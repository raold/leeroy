// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "RealmBodies.h"
#include "RealmCleanup.h"
#include "RealmPorts.h"
#include "LeeroyEvents.h"
#include "LeeroyRealmCleanup.h"
#include "LeeroyRealmFailure.h"
#include "LeeroyRealmRelease.h"
#include "LeeroyRealmTransmog.h"
#include "Bag.h"
#include "Battleground.h"
#include "BattlegroundMgr.h"
#include "BattlegroundQueue.h"
#include "DBCStores.h"
#include "Event.h"
#include "MapMgr.h"
#include "MotionMaster.h"
#include "ModelIgnoreFlags.h"
#include "PlayerScript.h"
#include "Spell.h"
#include "SpellInfo.h"
#include "SpellMgr.h"
#include <algorithm>
#include <cmath>
#include <map>
#include <mutex>
#include <set>

#if !defined(LEEROY_HAS_TRANSMOG)
#error "LEEROY_HAS_TRANSMOG comes from mod-leeroy.cmake; re-run cmake"
#endif
#if LEEROY_HAS_TRANSMOG
#include "Transmogrification.h"
#endif

namespace Leeroy::Realm
{
namespace
{
using Json = nlohmann::json;
constexpr float Pi = 3.14159265358979323846f;
RealmStep Refuse(std::string why) { return {StepState::Refused, std::move(why)}; }
RealmStep UnchangedRefusal(std::string why) { return {StepState::Refused, std::move(why), 0, {}, true}; }
// A member whose own step refused before it changed anything, for a reason of its own: Tick ends
// only its lease and the others go on (AcceptsMemberSkip). A reason about the job itself refuses the
// whole job, as before.
RealmStep MemberRefusal(std::string why)
{
    if (!MemberSkipReason(why)) return UnchangedRefusal(std::move(why));
    return {StepState::Skipped, std::move(why), 0, {}, true};
}
RealmStep Unknown(std::string why) { return {StepState::Unknown, std::move(why)}; }
std::uint64_t GroupId(Player* player)
{
    auto* group = player->GetGroup();
    return group ? group->GetGUID().GetRawValue() : 0;
}
std::vector<BodyScope> Scopes(Json const& state)
{
    std::vector<BodyScope> result;
    if (state.contains("scopes"))
        for (auto const& scope : state.at("scopes"))
            result.push_back({scope.at(0).get<std::uint32_t>(), scope.at(1).get<std::uint32_t>(),
                              scope.at(2).get<std::uint64_t>()});
    return result;
}
RealmStep Pending(Json const& state) { return {StepState::Pending, "", 0, Scopes(state)}; }
RealmStep Unproved(Json const& state, std::string why)
{
    return {StepState::Unknown, std::move(why), 0, Scopes(state)};
}
RealmStep Done(Queued& entry, std::uint64_t added = 0)
{
    auto total = entry.payload.value("total", std::uint64_t{0}) + added;
    entry.payload["total"] = total;
    auto scopes = Scopes(entry.payload.value("step", Json::object()));
    entry.payload.erase("step");
    return {StepState::Complete, "", total, std::move(scopes)};
}
std::string Eligible(Queued const& entry, std::uint32_t guid, Purpose purpose, bool castOnly = false)
{
    if (castOnly && !IsCast(guid)) return "not_cast";
    if (auto why = AdmissionReason(guid, purpose, entry.id); !why.empty()) return why;
    auto* player = FindOnline(guid);
    if (player->IsBeingTeleported()) return "busy:teleport";
    // TeleportTo can complete an unrelated duel and change the other body.
    if (purpose != Purpose::Duel && player->duel) return "busy:duel";
    return "";
}
bool Expired(Json const& state) { return NowMs() - state.at("at").get<std::uint64_t>() > OperationWaitMs; }
bool GroundMap(std::uint32_t map)
{
    auto const* info = sMapStore.LookupEntry(map);
    return info && !info->Instanceable();
}
struct Destination
{
    std::uint32_t map;
    float x, y, z, o;
};
Json SaveDestination(Destination const& destination)
{
    return {destination.map, destination.x, destination.y, destination.z, destination.o};
}
Destination LoadDestination(Json const& value)
{
    return {value.at(0).get<std::uint32_t>(), value.at(1).get<float>(), value.at(2).get<float>(),
            value.at(3).get<float>(), value.at(4).get<float>()};
}
Destination Home(Player* player)
{
    return {player->m_homebindMapId, player->m_homebindX, player->m_homebindY,
            player->m_homebindZ, player->GetOrientation()};
}
Destination EntryPoint(Player* player)
{
    auto const& point = player->GetEntryPoint();
    return point.GetMapId() == MAPID_INVALID ? Home(player)
        : Destination{point.GetMapId(), point.GetPositionX(), point.GetPositionY(), point.GetPositionZ(), point.GetOrientation()};
}
bool At(Player* player, Destination const& point, float tolerance = 3.0f)
{
    return player && !player->IsBeingTeleported() && player->GetMapId() == point.map
        && player->GetInstanceId() == 0 && player->GetDistance(point.x, point.y, point.z) <= tolerance;
}
bool TeleportMatches(Player* player, Destination const& point)
{
    auto const& target = player->GetTeleportDest();
    return target.GetMapId() == point.map && std::abs(target.GetPositionX() - point.x) < 0.1f
        && std::abs(target.GetPositionY() - point.y) < 0.1f && std::abs(target.GetPositionZ() - point.z) < 0.1f;
}
bool Behind(Player* human, float x, float y, float minimum, float maximum)
{
    auto dx = x - human->GetPositionX(), dy = y - human->GetPositionY();
    auto distance = std::sqrt(dx * dx + dy * dy);
    return distance >= minimum && distance <= maximum
        && dx * std::cos(human->GetOrientation()) + dy * std::sin(human->GetOrientation()) < 0;
}
void SetTransition(Json& state, Player* player, Destination const& point,
                   std::uint64_t resultingGroup)
{
    state["dest"] = SaveDestination(point);
    state["scopes"] = Json::array({Json::array({point.map, 0, resultingGroup}),
        Json::array({player->GetMapId(), player->GetInstanceId(), resultingGroup}),
        Json::array({point.map, 0, GroupId(player)})});
    // The original scope is independently retained by RealmModule.
}
RealmStep Move(Queued& entry, std::uint32_t guid)
{
    if (entry.payload.contains("step"))
    {
        auto& state = entry.payload["step"];
        if (auto why = RealmModule::Instance().MutationReason(guid, entry.id); !why.empty())
            return Unproved(state, why);
        auto* bot = ObjectAccessor::FindConnectedPlayer(PlayerGuid(guid));
        if (!bot) return Unproved(state, "move_identity");
        auto const destination = LoadDestination(state.at("dest"));
        if (bot->IsBeingTeleported())
            return !Expired(state) && TeleportMatches(bot, destination) ? Pending(state) : Unproved(state, "worldport_unproved");
        if (!At(FindOnline(guid), destination))
            return Expired(state) ? Unproved(state, "arrival_unproved") : Pending(state);
        if (state.value("walk", false))
        {
            auto* human = FindOnline(HumanGuid());
            if (!human || human->GetMapId() != destination.map || human->GetInstanceId() || human->InBattleground()
                || !Behind(human, bot->GetPositionX(), bot->GetPositionY(), 15.0f, 20.0f))
                return Unproved(state, "human_walk_scope_changed");
            return Done(entry);
        }
        if (entry.payload.at("place") != "near_human") return Done(entry);
        if (auto why = Eligible(entry, guid, Purpose::Move, true); !why.empty()) return Unproved(state, why);
        auto* human = FindOnline(HumanGuid());
        if (!human || human->GetMapId() != destination.map || human->GetInstanceId() || human->InBattleground())
            return Unproved(state, "human_moved");
        float const angle = human->GetOrientation() + Pi;
        float const x = human->GetPositionX() + std::cos(angle) * 18.0f;
        float const y = human->GetPositionY() + std::sin(angle) * 18.0f;
        float const z = human->GetMap()->GetHeight(human->GetPhaseMask(), x, y, human->GetPositionZ() + 5.0f);
        if (!std::isfinite(z) || z <= INVALID_HEIGHT) return Unproved(state, "walk_ground_unavailable");
        if (!MapMgr::IsValidMapCoord(destination.map, x, y, z)
            || sMapMgr->GetZoneId(PHASEMASK_NORMAL, destination.map, x, y, z) != human->GetZoneId())
            return Unproved(state, "walk_zone_mismatch");
        state["dest"] = SaveDestination({destination.map, x, y, z, human->GetOrientation()});
        state["walk"] = true;
        bot->GetMotionMaster()->MovePoint(0x4c5259, x, y, z);
        return Pending(state); // Completion requires arrival, not a void MovePoint return.
    }
    if (auto why = Eligible(entry, guid, Purpose::Move); !why.empty()) return Refuse(why);
    auto* bot = FindOnline(guid);
    if (entry.payload.value("social", false)
        && (!SocialReady(FindOnline(HumanGuid())) || !SocialReady(bot) || !IsCast(guid)))
        return UnchangedRefusal("social_unavailable");
    Destination destination{};
    std::uint32_t zone = 0;
    auto const placeName = entry.payload.at("place").get<std::string>();
    if (placeName == "near_human")
    {
        if (!IsCast(guid)) return Refuse("near_human");
        auto* human = FindOnline(HumanGuid());
        if (!human) return Refuse("human offline");
        if (human->GetMap()->Instanceable() || human->InBattleground()) return Refuse("near_human");
        zone = human->GetZoneId();
        bool found = false;
        for (unsigned i = 0; i < 8; ++i)
        {
            float distance = 60.0f + static_cast<float>(i) * (20.0f / 7.0f);
            float angle = human->GetOrientation() + Pi + (static_cast<float>(i % 3) - 1.0f) * 0.15f;
            // GetNearPoint searches a full circle for an in-LOS point and adds
            // combat reach. Exact rear XY preserves this operation's geometry.
            float x = human->GetPositionX() + std::cos(angle) * distance;
            float y = human->GetPositionY() + std::sin(angle) * distance;
            float z = human->GetMap()->GetHeight(human->GetPhaseMask(), x, y, human->GetPositionZ() + 10.0f);
            if (!std::isfinite(z) || z <= INVALID_HEIGHT) continue;
            if (!MapMgr::IsValidMapCoord(human->GetMapId(), x, y, z)
                || sMapMgr->GetZoneId(PHASEMASK_NORMAL, human->GetMapId(), x, y, z) != zone) continue;
            destination = {human->GetMapId(), x, y, z, human->GetOrientation()};
            found = true; // Increasing distance makes the fallback the farthest valid point.
            if (!human->GetMap()->isInLineOfSight(human->GetPositionX(), human->GetPositionY(), human->GetPositionZ() + 2,
                    x, y, z + 2, human->GetPhaseMask(), LINEOFSIGHT_ALL_CHECKS, VMAP::ModelIgnoreFlags::Nothing)) break;
        }
        if (!found) return Refuse("ground_unavailable");
    }
    else
    {
        auto const& places = RealmModule::Instance().Places();
        auto it = std::find_if(places.begin(), places.end(), [&](Place const& place) { return place.name == placeName; });
        if (it == places.end()) return Refuse("unknown_place");
        if (entry.payload.value("social", false) && it->kind != "inn") return UnchangedRefusal("unknown_place");
        zone = it->zone;
        destination = {it->map, it->x, it->y, it->z, it->o};
        auto slot = entry.payload.at("slot").get<std::uint32_t>();
        if (slot)
        {
            float const right = (static_cast<float>((slot - 1) % 8) - 3.5f) * 2.0f;
            float const back = static_cast<float>((slot - 1) / 8 + 1) * 2.0f;
            destination.x += std::sin(it->o) * right - std::cos(it->o) * back;
            destination.y -= std::cos(it->o) * right + std::sin(it->o) * back;
        }
    }
    if (!GroundMap(destination.map) || !MapMgr::IsValidMapCoord(destination.map, destination.x, destination.y,
            destination.z, destination.o)) return Refuse("invalid_place_map");
    if (sMapMgr->GetZoneId(PHASEMASK_NORMAL, destination.map, destination.x, destination.y, destination.z) != zone)
        return Refuse("place_zone_mismatch");
    if (RealmModule::Instance().Gankers().contains(guid) && !GankerMoveAllowed(Human(), zone)) return Refuse("unflagged");
    Json state{{"at", NowMs()}, {"walk", false}};
    SetTransition(state, bot, destination, GroupId(bot));
    entry.payload["step"] = state; // Save intended destination before any native mutation.
    if (!bot->TeleportTo(destination.map, destination.x, destination.y, destination.z, destination.o))
        return Unproved(state, "teleport_refused");
    return Pending(state);
}

RealmStep Rpg(Queued& entry, std::uint32_t guid)
{
    if (auto why = Eligible(entry, guid, Purpose::Status, true); !why.empty()) return Refuse(why);
    std::string why;
    Player* issuer = GmIssuer(why);
    if (!issuer) return Refuse(why);
    auto* bot = FindOnline(guid);
    auto* ai = GET_PLAYERBOT_AI(bot);
    auto status = entry.payload.at("status").get<std::string>();
    auto const expected = NewRpgInfo::StatusFromString(status);
    auto const quest = entry.payload.at("quest_id").get<std::uint32_t>();
    if (quest) status += " " + std::to_string(quest);
    bool accepted;
    {
        LeeroyQuietScope quiet;
        accepted = ai->DoSpecificAction("rpg status", Event("leeroy", status, issuer), true);
    }
    if (!accepted) return Refuse("refused");
    if (ai->rpgInfo.GetStatus() != expected) return Unknown("rpg_state_unproved");
    if (quest)
    {
        auto const* state = std::get_if<NewRpgInfo::DoQuest>(&ai->rpgInfo.data);
        if (!state || state->questId != quest) return Unknown("rpg_quest_unproved");
    }
    return Done(entry);
}

std::vector<Item*> BagItems(Player* player)
{
    std::vector<Item*> items;
    for (uint8 slot = INVENTORY_SLOT_ITEM_START; slot < INVENTORY_SLOT_ITEM_END; ++slot)
        if (auto* item = player->GetItemByPos(INVENTORY_SLOT_BAG_0, slot)) items.push_back(item);
    for (uint8 slot = INVENTORY_SLOT_BAG_START; slot < INVENTORY_SLOT_BAG_END; ++slot)
        if (auto* bag = player->GetBagByPos(slot))
            for (uint32 inside = 0; inside < bag->GetBagSize(); ++inside)
                if (auto* item = bag->GetItemByPos(static_cast<uint8>(inside))) items.push_back(item);
    return items;
}
RealmStep Rearm(Queued& entry, std::uint32_t guid)
{
    if (auto why = Eligible(entry, guid, Purpose::Rearm); !why.empty()) return MemberRefusal(why);
    auto* bot = FindOnline(guid);
    auto const* record = RealmModule::Instance().Weapons(guid);
    // Records live in memory: a worldserver start empties them until the 60 s poll finds the body
    // online and free. Record an armed body now, as that poll would; it then has nothing to put back.
    if (!record && RealmModule::Instance().RecordWeapons(guid, entry.id)) record = RealmModule::Instance().Weapons(guid);
    if (!record) return MemberRefusal("weapon_record_unavailable");
    auto const saved = *record;
    std::set<std::uint32_t> inventory;
    for (auto* item : BagItems(bot)) inventory.insert(item->GetGUID().GetCounter());
    auto const initial = ReadWeapons(bot);
    if (((!initial.mainHand || initial.mainIsPole) && saved.mainHand && !inventory.contains(saved.mainHand))
        || (!initial.offHand && saved.offHand && !inventory.contains(saved.offHand))
        || (!initial.ranged && saved.ranged && !inventory.contains(saved.ranged))) return MemberRefusal("recorded_weapon_unavailable");
    auto const steps = PlanRearm(saved, initial, inventory);
    std::uint64_t changed = 0;
    for (auto const& step : steps)
    {
        uint8 slot = static_cast<uint8>(step.slot == WeaponSlot::Main ? EQUIPMENT_SLOT_MAINHAND
            : step.slot == WeaponSlot::Off ? EQUIPMENT_SLOT_OFFHAND : EQUIPMENT_SLOT_RANGED);
        auto* item = bot->GetItemByGuid(ObjectGuid::Create<HighGuid::Item>(step.item));
        auto* equipped = bot->GetItemByPos(INVENTORY_SLOT_BAG_0, slot);
        if (!item || item->IsEquipped() || (item->GetTemplate()->Class == ITEM_CLASS_WEAPON
            && item->GetTemplate()->SubClass == ITEM_SUBCLASS_WEAPON_FISHING_POLE))
            return changed ? Unknown("recorded_weapon_changed") : MemberRefusal("recorded_weapon_unavailable");
        if (equipped && !(slot == EQUIPMENT_SLOT_MAINHAND && ReadWeapons(bot).mainIsPole))
            return changed ? Unknown("equipment_changed") : MemberRefusal("equipment_changed");
        uint16 destination = 0;
        if (bot->CanEquipItem(slot, destination, item, true) != EQUIP_ERR_OK
            || destination != static_cast<uint16>((INVENTORY_SLOT_BAG_0 << 8) | slot))
            return changed ? Unknown("cannot_equip_recorded_weapon") : MemberRefusal("cannot_equip_recorded_weapon");
        // A two-handed main must never evict a real offhand as a side effect.
        auto before = ReadWeapons(bot);
        if (slot == EQUIPMENT_SLOT_MAINHAND && before.offHand
            && item->GetTemplate()->InventoryType == INVTYPE_2HWEAPON)
            return changed ? Unknown("offhand_occupied") : MemberRefusal("offhand_occupied");
        bot->SwapItem(item->GetPos(), destination);
        auto after = ReadWeapons(bot);
        if (!bot->GetItemByPos(INVENTORY_SLOT_BAG_0, slot)
            || bot->GetItemByPos(INVENTORY_SLOT_BAG_0, slot)->GetGUID().GetCounter() != step.item
            || (slot != EQUIPMENT_SLOT_MAINHAND && before.mainHand != after.mainHand)
            || (slot != EQUIPMENT_SLOT_OFFHAND && before.offHand != after.offHand)
            || (slot != EQUIPMENT_SLOT_RANGED && before.ranged != after.ranged)) return Unknown("weapon_swap_unproved");
        ++changed;
    }
    auto now = ReadWeapons(bot);
    if ((!now.mainHand || now.mainIsPole) && saved.mainHand && !inventory.contains(saved.mainHand))
        return changed ? Unknown("recorded_weapon_unavailable") : MemberRefusal("recorded_weapon_unavailable");
    if (!RealmModule::Instance().RecordWeapons(guid, entry.id))
        return changed ? Unknown("weapon_record_unproved") : MemberRefusal("weapon_record_unavailable");
    auto result = Done(entry, changed);
    result.unchanged = changed == 0;
    return result;
}

RealmStep Equip(Queued& entry, std::uint32_t guid)
{
    if (auto why = Eligible(entry, guid, Purpose::Equip, true); !why.empty()) return Refuse(why);
    auto* bot = FindOnline(guid);
    auto* previous = bot->GetItemByPos(INVENTORY_SLOT_BAG_0, EQUIPMENT_SLOT_TABARD);
    std::uint32_t replaced = previous ? previous->GetEntry() : 0;
    if (replaced == kGuildTabard) return Done(entry);
    auto find = [&]() -> Item*
    {
        for (auto* item : BagItems(bot)) if (item->GetEntry() == kGuildTabard) return item;
        auto* equipped = bot->GetItemByPos(INVENTORY_SLOT_BAG_0, EQUIPMENT_SLOT_TABARD);
        return equipped && equipped->GetEntry() == kGuildTabard ? equipped : nullptr;
    };
    Item* item = find();
    bool created = false;
    if (!item)
    {
        if (bot->GetItemCount(kGuildTabard, true)) return Refuse("tabard_not_in_bags");
        // StoreNewItemInBestSlots may equip directly. Read back both locations.
        if (!bot->StoreNewItemInBestSlots(kGuildTabard, 1)) return Unknown("tabard_store_unproved");
        created = true;
        item = find();
    }
    if (!item) return Unknown("tabard_missing");
    if (!item->IsEquipped())
    {
        uint16 destination = 0;
        if (bot->CanEquipItem(EQUIPMENT_SLOT_TABARD, destination, item, true) != EQUIP_ERR_OK
            || destination != ((INVENTORY_SLOT_BAG_0 << 8) | EQUIPMENT_SLOT_TABARD))
            return created ? Unknown("cannot_equip") : Refuse("cannot_equip");
        bot->SwapItem(item->GetPos(), destination);
    }
    auto* actual = bot->GetItemByPos(INVENTORY_SLOT_BAG_0, EQUIPMENT_SLOT_TABARD);
    return actual && actual->GetGUID() == item->GetGUID() ? Done(entry, replaced) : Unknown("tabard_equip_unproved");
}

#if LEEROY_HAS_TRANSMOG
static_assert(EQUIPMENT_SLOT_START == 0 && EQUIPMENT_SLOT_END == kTransmogSlots
    && INVENTORY_SLOT_BAG_0 == kTransmogEquipmentBag && ITEM_END <= 128,
    "transmog snapshots require the supported native equipment/item layout");
std::optional<TransmogSnapshot> ReadTransmog(Player* player, Transmogrification const* transmog)
{
    if (!player || !transmog) return std::nullopt;
    TransmogSnapshot snapshot;
    auto const owner = player->GetGUID();
    snapshot.owner = owner.GetRawValue(); snapshot.money = player->GetMoney();
    auto const records = transmog->entryMap.find(owner);
    for (uint8 slot = EQUIPMENT_SLOT_START; slot < EQUIPMENT_SLOT_END; ++slot)
    {
        snapshot.visibleEntries[slot] = player->GetUInt32Value(PLAYER_VISIBLE_ITEM_1_ENTRYID + slot * 2);
        snapshot.visibleEnchants[slot] = player->GetUInt32Value(PLAYER_VISIBLE_ITEM_1_ENCHANTMENT + slot * 2);
        auto* item = player->GetItemByPos(INVENTORY_SLOT_BAG_0, slot);
        if (!item) continue;
        // Only ordinary equipped items, never a bag/container or an unknown
        // extension of the native update-field layout.
        if (item->GetValuesCount() != ITEM_END) return std::nullopt;
        auto& fact = snapshot.items[slot];
        fact.object = reinterpret_cast<std::uintptr_t>(item);
        fact.container = reinterpret_cast<std::uintptr_t>(item->GetContainer());
        fact.guid = item->GetGUID().GetRawValue(); fact.owner = item->GetOwnerGUID().GetRawValue();
        fact.entry = item->GetEntry(); fact.bag = item->GetBagSlot(); fact.slot = item->GetSlot();
        fact.equipped = item->IsEquipped(); fact.inTrade = item->IsInTrade();
        fact.updateState = item->GetState(); fact.queuePosition = item->GetQueuePos();
        fact.refundRecipient = item->GetRefundRecipient(); fact.paidMoney = item->GetPaidMoney();
        fact.paidExtendedCost = item->GetPaidExtendedCost(); fact.text = item->GetText();
        fact.fields.reserve(ITEM_END);
        for (uint16 field = 0; field < ITEM_END; ++field) fact.fields.push_back(item->GetUInt32Value(field));
        auto& appearance = snapshot.appearances[slot];
        auto const indexed = transmog->dataMap.find(item->GetGUID());
        if (indexed != transmog->dataMap.end())
        { appearance.indexed = true; appearance.owner = indexed->second.GetRawValue(); }
        if (records != transmog->entryMap.end())
        {
            auto const record = records->second.find(item->GetGUID());
            if (record != records->second.end())
            { appearance.recorded = true; appearance.entry = record->second; }
        }
        appearance.readback = transmog->GetFakeEntry(item->GetGUID());
    }
    return snapshot;
}
#endif

RealmStep Transmog(Queued& entry, std::uint32_t guid)
{
#if LEEROY_HAS_TRANSMOG
    if (auto why = Eligible(entry, guid, Purpose::Equip, true); !why.empty()) return Refuse(why);
    auto& module = RealmModule::Instance();
    auto* player = FindOnline(guid);
    auto* transmog = sTransmogrification;
    auto authority = [&]
    {
        return TransmogAuthority{module.On(), IsCast(guid), Eligible(entry, guid, Purpose::Equip, true).empty(),
            module.Epoch(), module.BodyGeneration(guid), GuildId(), HumanGuid(), module.Holder(guid), Identity(guid)};
    };
    auto const originalAuthority = authority();
    auto const slot = entry.payload.at("slot").get<std::uint32_t>();
    auto const appearance = entry.payload.at("entry").get<std::uint32_t>();
    if (slot >= EQUIPMENT_SLOT_END) return Refuse("bad_entry");
    auto* item = player->GetItemByPos(INVENTORY_SLOT_BAG_0, static_cast<uint8>(slot));
    auto const itemGuid = item ? item->GetGUID() : ObjectGuid::Empty;
    auto exact = [&]
    {
        return SameTransmogAuthority(originalAuthority, authority(), entry.id)
            && FindOnline(guid) == player && sTransmogrification == transmog
            && player->GetItemByPos(INVENTORY_SLOT_BAG_0, static_cast<uint8>(slot)) == item
            && (!item || (item->GetGUID() == itemGuid && item->GetOwnerGUID() == player->GetGUID()
                && item->GetBagSlot() == INVENTORY_SLOT_BAG_0 && item->GetSlot() == slot && item->IsEquipped()));
    };
    auto const result = MutateTransmog(transmog != nullptr, slot, appearance,
        [&] { return Eligible(entry, guid, Purpose::Equip, true); },
        [&]() -> std::optional<TransmogSnapshot>
        { return exact() ? ReadTransmog(player, transmog) : std::nullopt; },
        [&]
        {
            auto const* source = sObjectMgr->GetItemTemplate(appearance);
            auto const* target = item ? item->GetTemplate() : nullptr;
            return source && target && source->DisplayInfoID && target->DisplayInfoID
                && sItemDisplayInfoStore.LookupEntry(source->DisplayInfoID)
                && sItemDisplayInfoStore.LookupEntry(target->DisplayInfoID)
                && transmog->CanTransmogrifyItemWithItem(player, target, source);
        }, exact,
        [&] { transmog->SetFakeEntry(player, appearance, static_cast<uint8>(slot), item); },
        [&] { transmog->DeleteFakeEntry(player, static_cast<uint8>(slot), item); });
    if (result.state == TransmogState::Complete) return Done(entry);
    return result.state == TransmogState::Unknown ? Unknown(result.reason) : Refuse(result.reason);
#else
    (void)entry; (void)guid;
    return Refuse("no_transmog");
#endif
}

RealmStep Profession(Queued& entry, std::uint32_t guid)
{
    if (auto why = Eligible(entry, guid, Purpose::Craft, true); !why.empty()) return Refuse(why);
    auto* bot = FindOnline(guid);
    uint16 const skill = static_cast<uint16>(entry.payload.at("skill_id").get<std::uint32_t>());
    if (IsPrimaryProfession(skill) && !bot->HasSkill(skill))
    {
        auto known = std::count_if(kProfessionSkills.begin(), kProfessionSkills.end(),
            [&](auto id) { return IsPrimaryProfession(id) && bot->HasSkill(static_cast<uint16>(id)); });
        if (known >= 2 || !bot->GetFreePrimaryProfessionPoints()) return Refuse("skill_cap");
    }
    std::set<uint32> spells;
    // Mirrors GPL core cs_learn.cpp HandleLearnSkillRecipesHelper: rank chain,
    // terminal recipes, race/class restrictions and SpellMgr validity.
    for (uint32 rank : sSpellMgr->GetSkillRankSpells(skill)) spells.insert(rank);
    for (auto const* ability : GetSkillLineAbilitiesBySkillLine(skill))
    {
        if (ability->SupercededBySpell || ability->RaceMask || ability->MinSkillLineRank > 450
            || (ability->ClassMask && !(ability->ClassMask & bot->getClassMask()))) continue;
        auto const* info = sSpellMgr->GetSpellInfo(ability->Spell);
        if (info && SpellMgr::IsSpellValid(info)) spells.insert(ability->Spell);
    }
    bot->SetSkill(skill, bot->GetSkillStep(skill), 450, 450);
    for (auto spell : spells) bot->learnSpell(spell);
    if (bot->GetPureSkillValue(skill) != 450 || bot->GetPureMaxSkillValue(skill) != 450
        || std::any_of(spells.begin(), spells.end(), [&](auto spell) { return !bot->HasSpell(spell); }))
        return Unknown("profession_unproved");
    return Done(entry);
}

struct DuelReceipt
{
    std::string operation;
    Player* source = nullptr;
    Player* target = nullptr;
    WorldSession* sourceSession = nullptr;
    WorldSession* targetSession = nullptr;
    DuelInfo* sourceDuel = nullptr;
    DuelInfo* targetDuel = nullptr;
    std::uint64_t epoch = 0;
    bool ended = false;
};
std::mutex DuelMutex;
std::map<std::uint32_t, DuelReceipt> Duels;
thread_local DuelReceipt* StartingDuel = nullptr;
void EndedDuel(Player* winner, Player* loser)
{
    std::lock_guard lock(DuelMutex);
    for (auto source : {winner->GetGUID().GetCounter(), loser->GetGUID().GetCounter()})
    {
        auto it = Duels.find(source);
        if (it == Duels.end()) continue;
        auto& receipt = it->second;
        if (!((receipt.source == winner && receipt.target == loser) || (receipt.source == loser && receipt.target == winner))
            || receipt.epoch != RealmModule::Instance().Epoch()
            || receipt.sourceSession != receipt.source->GetSession() || receipt.targetSession != receipt.target->GetSession()
            || !receipt.source->duel || !receipt.target->duel
            || receipt.source->duel->Opponent != receipt.target || receipt.target->duel->Opponent != receipt.source
            || receipt.source->duel->Initiator != receipt.source || receipt.target->duel->Initiator != receipt.source) continue;
        if (StartingDuel != &receipt && (receipt.sourceDuel != receipt.source->duel.get()
            || receipt.targetDuel != receipt.target->duel.get())) continue;
        receipt.sourceDuel = receipt.source->duel.get();
        receipt.targetDuel = receipt.target->duel.get();
        receipt.ended = true; // Core clears duel pointers/team/arbiter after this hook.
    }
}
RealmStep Duel(Queued& entry, std::uint32_t guid)
{
    auto& module = RealmModule::Instance();
    auto const source = entry.payload.at("guid").get<std::uint32_t>();
    auto const target = entry.payload.at("target_guid").get<std::uint32_t>();
    if (guid != source) return Done(entry); // Exact end + native cleanup was proved for both in the first step.
    if (entry.payload.contains("step"))
    {
        auto const& state = entry.payload.at("step");
        for (auto member : entry.guids)
            if (auto why = module.MutationReason(member, entry.id); !why.empty()) return Unproved(state, why);
        auto* bot = FindOnline(source);
        auto* other = FindOnline(target);
        if (!bot || !other) return Unproved(state, "duel_body_unavailable");
        std::lock_guard lock(DuelMutex);
        auto it = Duels.find(source);
        if (it == Duels.end() || it->second.operation != entry.id || it->second.source != bot || it->second.target != other
            || it->second.sourceSession != bot->GetSession() || it->second.targetSession != other->GetSession()
            || it->second.epoch != module.Epoch()) return Unproved(state, "duel_receipt_missing");
        auto const& receipt = it->second;
        if (receipt.ended && !bot->duel && !other->duel && bot->GetGuidValue(PLAYER_DUEL_ARBITER).IsEmpty()
            && other->GetGuidValue(PLAYER_DUEL_ARBITER).IsEmpty() && !bot->GetUInt32Value(PLAYER_DUEL_TEAM)
            && !other->GetUInt32Value(PLAYER_DUEL_TEAM))
        {
            Duels.erase(it);
            return Done(entry);
        }
        if (!receipt.ended && (!bot->duel || !other->duel || bot->duel.get() != receipt.sourceDuel
            || other->duel.get() != receipt.targetDuel)) return Unproved(state, "duel_changed_without_end");
        // A long duel remains owned; elapsed time is not cancellation proof.
        return Pending(state);
    }
    for (auto member : entry.guids)
        if (auto why = Eligible(entry, member, Purpose::Duel); !why.empty()) return Refuse(why);
    auto* bot = FindOnline(source);
    auto* other = FindOnline(target);
    if (bot->duel || other->duel) return Refuse("busy:duel");
    if (!bot->IsWithinDistInMap(other, 30.0f)) return Refuse("too_far");
    auto budget = module.ChargeAffectedBodies(entry.id, {source, target});
    if (budget == "busy:tick_budget") return {StepState::Pending};
    if (!budget.empty()) return Refuse(budget);
    {
        std::lock_guard lock(DuelMutex);
        if (Duels.contains(source) || Duels.size() >= ActionQueue::Capacity) return Refuse("busy:duel_receipt");
        auto [it, inserted] = Duels.emplace(source, DuelReceipt{entry.id, bot, other, bot->GetSession(), other->GetSession(),
            nullptr, nullptr, module.Epoch(), false});
        (void)inserted;
        StartingDuel = &it->second;
    }
    struct StartingScope { ~StartingScope() { StartingDuel = nullptr; } } startingScope;
    entry.payload["step"] = Json{{"at", NowMs()}};
    auto result = bot->CastSpell(other, 7266, false);
    StartingDuel = nullptr;
    bool const challenge = bot->duel && other->duel && bot->duel->Opponent == other && other->duel->Opponent == bot
        && bot->duel->Initiator == bot && other->duel->Initiator == bot;
    std::lock_guard lock(DuelMutex);
    auto& receipt = Duels.at(source);
    if (challenge)
    {
        receipt.sourceDuel = bot->duel.get(); receipt.targetDuel = other->duel.get();
        return Pending(entry.payload.at("step"));
    }
    if (receipt.ended) return Pending(entry.payload.at("step"));
    if (result == SPELL_CAST_OK || bot->duel || other->duel) return Unknown("duel_unproved");
    Duels.erase(source); entry.payload.erase("step");
    return Refuse("duel_refused");
}

LfgPhase Phase(lfg::LfgState state)
{
    switch (state)
    {
        case lfg::LFG_STATE_NONE: return LfgPhase::None;
        case lfg::LFG_STATE_ROLECHECK: return LfgPhase::RoleCheck;
        case lfg::LFG_STATE_QUEUED: return LfgPhase::Queued;
        case lfg::LFG_STATE_PROPOSAL: return LfgPhase::Proposal;
        case lfg::LFG_STATE_BOOT: return LfgPhase::Boot;
        case lfg::LFG_STATE_DUNGEON: return LfgPhase::Dungeon;
        case lfg::LFG_STATE_FINISHED_DUNGEON: return LfgPhase::FinishedDungeon;
        case lfg::LFG_STATE_RAIDBROWSER: return LfgPhase::RaidBrowser;
    }
    return LfgPhase::Boot; // Unknown state is never treated as no ownership.
}
bool WgMember(Battlefield* field, ObjectGuid guid)
{
    if (!field) return false;
    for (auto team : {TEAM_ALLIANCE, TEAM_HORDE})
        if (field->GetPlayersQueueSet(team).contains(guid) || field->GetInvitedPlayersMap(team).contains(guid)
            || field->GetPlayersInWarSet(team).contains(guid)) return true;
    return false;
}
bool QueuedForPvp(Player* bot)
{
    for (uint32 slot = 0; slot < PLAYER_MAX_BATTLEGROUND_QUEUES; ++slot)
        if (bot->GetBattlegroundQueueTypeId(slot) != BATTLEGROUND_QUEUE_NONE) return true;
    return false;
}
RealmStep LeavePvp(Queued& entry, std::uint32_t guid)
{
    auto& module = RealmModule::Instance();
    bool const started = entry.payload.contains("step");
    if (started)
    {
        if (auto why = module.MutationReason(guid, entry.id); !why.empty()) return Unproved(entry.payload.at("step"), why);
    }
    else if (auto why = Eligible(entry, guid, Purpose::LeavePvp); !why.empty())
    {
        if (auto skip = SkipLeaveMember(entry, guid, why)) return *skip;
        return entry.payload.value("cleanup_mutated", false) ? Unknown(why) : UnchangedRefusal(why);
    }
    auto* bot = ObjectAccessor::FindConnectedPlayer(PlayerGuid(guid));
    if (!bot) return started ? Unproved(entry.payload.at("step"), "leave_identity")
        : (entry.payload.value("cleanup_mutated", false) ? Unknown("leave_identity") : UnchangedRefusal("offline"));
    if (!started) entry.payload["step"] = Json{{"at", NowMs()}, {"changed", false}, {"stage", "queues"}};
    auto& state = entry.payload["step"];
    auto refuse = [&](std::string why)
    {
        return state.at("changed").get<bool>() || entry.payload.value("cleanup_mutated", false)
            ? Unproved(state, std::move(why)) : UnchangedRefusal(std::move(why));
    };
    // A member the leave cannot take out for a reason of its own (its battleground or original raid
    // group, its entry point, its LFG or Wintergrasp state, a step that never finished): before its
    // own step or a shared cleanup in this job changed it, it is left as it is and the others go on
    // (SkipLeaveMember ends this step, the state with it); after, the old rule stands.
    auto fail = [&](std::string why)
    {
        if (auto skip = SkipLeaveMember(entry, guid, why)) return *skip;
        return refuse(std::move(why));
    };
    // A fault in another member already rechecked (one this job took out or touched) is the job's own.
    if (auto fault = OrdinaryCleanupFault(entry); !fault.reason.empty())
    {
        auto const check = LeaveReadbackCheck(entry, fault.guid);
        if (fault.guid != guid && !Expired(state) && (check == "teleport" || check == "exit")) return Pending(state);
        auto why = LeaveReadbackFault(entry, fault.guid,
            check.empty() ? std::string(CleanupFaultCheck(fault.reason)) : check, fault.reason);
        return fault.guid == guid ? fail(std::move(why)) : refuse(std::move(why));
    }
    if (Expired(state))
    {
        auto check = LeaveReadbackCheck(entry, guid);
        return fail(check.empty() ? "leave_cleanup_unproved"
            : LeaveReadbackFault(entry, guid, check, "leave_cleanup_unproved"));
    }
    auto stage = state.at("stage").get<std::string>();
    if (stage == "shared_lfg") return LeaveOnlineLfgGroup(entry, guid);
    auto* field = sBattlefieldMgr->GetBattlefieldToZoneId(4197);
    if (stage == "worldport")
    {
        auto const destination = LoadDestination(state.at("dest"));
        if (bot->IsBeingTeleported())
            return TeleportMatches(bot, destination) ? Pending(state)
                : Unproved(state, LeaveReadbackFault(entry, guid, "teleport", "leave_worldport_changed"));
        if (!At(FindOnline(guid), destination)) return Pending(state);
        if (bot->GetDelayedOperations() & (DELAYED_BG_MOUNT_RESTORE | DELAYED_BG_TAXI_RESTORE | DELAYED_BG_GROUP_RESTORE))
            return Pending(state);
        auto group = GroupId(bot);
        if (group != state.at("result_group").get<std::uint64_t>())
            return Unproved(state, LeaveReadbackFault(entry, guid, "group", "leave_group_cleanup_unproved"));
        auto source = state.at("source").get<std::string>();
        if ((source == "bg" && bot->InBattleground())
            || (source == "wg" && WgMember(field, bot->GetGUID()))
            || (source == "lfg" && (sLFGMgr->GetState(bot->GetGUID()) != lfg::LFG_STATE_NONE
                || (bot->GetGroup() && bot->GetGroup()->isLFGGroup())))) return Pending(state);
        state["stage"] = "queues";
        stage = "queues";
    }
    if (FindOnline(guid) != bot) return fail("body_unavailable");
    if (auto why = Eligible(entry, guid, Purpose::LeavePvp); !why.empty()) return fail(why);
    auto* currentGroup = bot->GetGroup();
    // Leaving would change the rest of its group.
    if (auto why = BattlegroundGroupDeparture(bot); !why.empty()) return fail(std::move(why));
    if (bot->InBattleground())
    {
        auto* bg = bot->GetBattleground();
        if (!bg) return fail("battleground_receipt_unavailable");
        auto* actualGroup = bg->GetBgRaid(bot->GetBgTeamId());
        if (actualGroup && actualGroup->IsMember(bot->GetGUID()) && actualGroup != currentGroup)
            return fail("battleground_group_changed");
    }

    // Every iteration performs at most one native queue/teleport mutation.
    // The core's ordinary packet leave handler removes the entire queued
    // group, so use the per-player queue primitive and prove its readback.
    for (uint32 slot = 0; slot < PLAYER_MAX_BATTLEGROUND_QUEUES; ++slot)
    {
        auto type = bot->GetBattlegroundQueueTypeId(slot);
        if (type == BATTLEGROUND_QUEUE_NONE) continue;
        if (bot->InBattleground() && type == BattlegroundMgr::BGQueueTypeId(bot->GetBattleground()->GetBgTypeID(),
            bot->GetBattleground()->GetArenaType())) continue; // Active BG slot is cleared by native map leave.
        return LeaveNativeQueue(entry, guid, slot);
    }
    auto transition = [&](std::string source, Destination const& destination, std::uint64_t group)
    {
        state["stage"] = "worldport"; state["source"] = std::move(source);
        state["result_group"] = group; state["changed"] = true;
        SetTransition(state, bot, destination, group);
    };
    if (bot->InBattleground())
    {
        auto const destination = EntryPoint(bot);
        if (!GroundMap(destination.map)) return fail("entry_point_unavailable");
        auto* original = bot->GetOriginalGroup();
        if (original && (original->isRaidGroup() || original->IsMember(PlayerGuid(HumanGuid()))))
            return fail("busy:raid_owner");
        auto targetGroup = original ? original->GetGUID().GetRawValue() : 0;
        transition("bg", destination, targetGroup);
        if (auto why = PrepareOrdinaryCleanupMutation(entry, guid, Scopes(state)); !why.empty()) return fail(why);
        bot->LeaveBattleground();
        return Pending(state);
    }
    auto* group = bot->GetGroup();
    auto lfgState = sLFGMgr->GetState(bot->GetGUID());
    auto exit = DecideLfgExit(Phase(lfgState), group && group->isLFGGroup());
    if (exit != LfgExit::None)
    {
        auto lfgGroup = sLFGMgr->GetGroup(bot->GetGUID());
        // Shared applicants need the exact native producer receipt. Proposal
        // coverage remains unavailable without its complete participant proof.
        if (lfgState == lfg::LFG_STATE_PROPOSAL) return fail("shared_lfg_proposal");
        if (lfgGroup)
        {
            auto groupState = sLFGMgr->GetState(lfgGroup);
            if (groupState == lfg::LFG_STATE_PROPOSAL) return fail("shared_lfg_proposal");
            if (groupState == lfg::LFG_STATE_QUEUED || groupState == lfg::LFG_STATE_ROLECHECK)
                return LeaveObservedLfgApplicants(entry, guid);
        }
        // A reverse group identity alone cannot prove the affected closure.
        if (lfgGroup && (lfgState == lfg::LFG_STATE_QUEUED || lfgState == lfg::LFG_STATE_ROLECHECK))
            return LeaveObservedLfgApplicants(entry, guid);
        if (exit == LfgExit::LeaveGroup)
            return LeaveOnlineLfgGroup(entry, guid);
        if (auto why = PrepareOrdinaryCleanupMutation(entry, guid,
            {{bot->GetMapId(), bot->GetInstanceId(), GroupId(bot)}}); !why.empty()) return fail(why);
        state["changed"] = true;
        sLFGMgr->LeaveLfg(bot->GetGUID());
        if (sLFGMgr->GetState(bot->GetGUID()) != lfg::LFG_STATE_NONE)
            return Unproved(state, LeaveReadbackFault(entry, guid, "lfg", "lfg_queue_leave_unproved"));
        return Pending(state);
    }
    if (field && WgMember(field, bot->GetGUID()))
        return fail("wg_private_state_unavailable");
    if (QueuedForPvp(bot) || bot->InBattleground() || sLFGMgr->GetState(bot->GetGUID()) != lfg::LFG_STATE_NONE
        || (bot->GetGroup() && (bot->GetGroup()->isLFGGroup() || bot->GetGroup()->isBGGroup() || bot->GetGroup()->isBFGroup())))
        return fail(LeaveReadbackFault(entry, guid, LeaveReadbackCheck(entry, guid), "leave_cleanup_unproved"));
    if (entry.cursor + 1 < entry.guids.size())
        if (auto why = VerifyCleanupReceipts(entry); !why.empty()) return Unproved(state, why);
    bool const bodyChanged = state.at("changed").get<bool>() || TouchedByCleanup(entry, guid);
    if (state.at("changed").get<bool>()) RecordOwnStepChange(entry, guid);
    auto result = Done(entry, bodyChanged ? 1 : 0);
    result.unchanged = !bodyChanged && !entry.payload.value("cleanup_mutated", false);
    return result;
}

template<class Parse, class Payload>
void QueueHandler(char const* kind, Parse parse, Payload payload, RealmModule::Executor executor,
    RealmModule::Admission admission = {})
{
    RealmModule::Instance().RegisterExecutor(kind, std::move(executor), std::move(admission));
    RegisterCommand(kind, [kind, parse, payload](Json const& message) -> LeeroyCommandResult
    {
        std::string error;
        auto command = parse(message, error);
        if (!command) return {false, error};
        Queued entry;
        entry.id = message.at("id").get<std::string>(); entry.kind = kind;
        payload(*command, entry);
        if (!RealmModule::Instance().Accept(std::move(entry), error)) return {false, error};
        return {true, ""};
    });
}
class BodyScripts final : public PlayerScript
{
public:
    BodyScripts() : PlayerScript("LeeroyRealmBodyScripts", {
        PLAYERHOOK_ON_PVP_KILL, PLAYERHOOK_ON_DUEL_END, PLAYERHOOK_ON_PLAYER_PVP_FLAG_CHANGE}) { }
    void OnPlayerPVPKill(Player* killer, Player* victim) override
    {
        auto& module = RealmModule::Instance();
        if (!module.On() || !killer || !victim) return;
        auto k = killer->GetGUID().GetCounter(), v = victim->GetGUID().GetCounter();
        if (module.Watched(k) || module.Watched(v)) Publish(MakePvpKill(k, v, killer->GetMapId(), killer->GetZoneId()));
    }
    void OnPlayerDuelEnd(Player* winner, Player* loser, DuelCompleteType type) override
    {
        auto& module = RealmModule::Instance();
        if (!module.On() || !winner || !loser) return;
        EndedDuel(winner, loser);
        auto w = winner->GetGUID().GetCounter(), l = loser->GetGUID().GetCounter();
        // The core's own city rule (its capital zone flag), for either duelist.
        auto const inCity = [](Player const* player)
        {
            auto const* zone = sAreaTableStore.LookupEntry(player->GetZoneId());
            return zone && IsCityZone(zone->flags);
        };
        if (module.Watched(w) || module.Watched(l))
            Publish(MakeDuelEnd(w, l, type == DUEL_WON ? "won" : type == DUEL_FLED ? "fled" : "interrupted",
                winner->GetMapId(), winner->GetZoneId(), inCity(winner) || inCity(loser)));
    }
    void OnPlayerPVPFlagChange(Player* player, bool /*state*/) override
    {
        if (!RealmModule::Instance().On() || !player || player->GetGUID().GetCounter() != HumanGuid()) return;
        bool flagged = player->HasPlayerFlag(PLAYER_FLAGS_IN_PVP) || player->InBattleground() || player->InArena();
        auto* field = sBattlefieldMgr->GetBattlefieldToZoneId(player->GetZoneId());
        flagged = flagged || (player->GetZoneId() == 4197 && field && field->IsWarTime());
        Publish(MakePvpFlag(HumanGuid(), flagged)); // RealmModule also polls the same narrow flag every second.
    }
};
}

void RegisterRealmBodyHandlers()
{
    QueueHandler("realm_move", ParseMove, [](MoveCmd const& command, Queued& entry)
        { entry.guids = {command.guid}; entry.payload = {{"place", command.place}, {"slot", command.slot}, {"social", command.social}}; }, Move);
    RegisterCommand("realm_visit", [](Json const& message) -> LeeroyCommandResult
    {
        std::string error;
        auto command = ParseVisit(message, error);
        if (!command) return {false, error};
        if (!RealmModule::Instance().On()) return {false, "realm_off"};
        auto const& places = RealmModule::Instance().Places();
        auto it = std::find_if(places.begin(), places.end(), [&](Place const& p) { return p.name == command->place; });
        if (it == places.end() || it->kind != "inn") return {false, "unknown_place"};
        auto* human = FindOnline(HumanGuid());
        bool const ready = SocialReady(human);
        bool const nearby = ready && human->GetMapId() == it->map
            && human->GetDistance(it->x, it->y, it->z) <= 20.0f;
        Publish(MakeVisitState(message.at("id").get<std::string>(), command->place, GuildId(), HumanGuid(), ready, nearby));
        return {true, ""};
    });
    QueueHandler("rpg_status", ParseRpgStatus, [](RpgStatusCmd const& command, Queued& entry)
        { entry.guids = {command.guid}; entry.payload = {{"status", command.status}, {"quest_id", command.questId}}; }, Rpg);
    QueueHandler("leave_pvp", ParseLeavePvp, [](LeavePvpCmd const& command, Queued& entry)
        { entry.guids = command.guids; entry.payload = {{"pvp_owner", command.pvpOwner}}; }, LeavePvp);
    QueueHandler("rearm", ParseRearm, [](RearmCmd const& command, Queued& entry)
        { entry.guids = command.guids; entry.payload = Json::object(); }, Rearm);
    QueueHandler("duel", ParseDuel, [](DuelCmd const& command, Queued& entry)
        { entry.guids = {command.guid, command.targetGuid}; entry.payload = {{"guid", command.guid}, {"target_guid", command.targetGuid}}; }, Duel);
    QueueHandler("equip", ParseEquip, [](EquipCmd const& command, Queued& entry)
        { entry.guids = {command.guid}; entry.payload = Json::object(); }, Equip);
    QueueHandler("set_profession", ParseSetProfession, [](SetProfessionCmd const& command, Queued& entry)
        { entry.guids = {command.guid}; entry.payload = {{"skill_id", command.skillId}}; }, Profession);
    QueueHandler("transmog_set", ParseTransmogSet, [](TransmogSetCmd const& command, Queued& entry)
        { entry.guids = {command.guid}; entry.payload = {{"slot", command.slot}, {"entry", command.entry}}; }, Transmog,
        [](Queued const&, std::uint32_t guid) -> std::string { return IsCast(guid) ? "" : "not_cast"; });
    RegisterCommand("item_count", [](Json const& message) -> LeeroyCommandResult
    {
        std::string error;
        auto command = ParseItemCount(message, error);
        if (!command) return {false, error};
        if (!RealmModule::Instance().On()) return {false, "realm_off"};
        if (!IsCast(command->guid) && command->guid != HumanGuid()) return {false, "not_cast"};
        auto* player = FindOnline(command->guid);
        if (!player) return {false, "offline"};
        std::uint64_t total = 0;
        for (auto item : command->itemIds) total += player->GetItemCount(item, true);
        RealmModule::Instance().PublishJob({message.at("id").get<std::string>(), "item_count", true, "", command->guid, total});
        return {true, ""};
    });
}
void AddRealmBodyScripts() { new BodyScripts(); }
}
