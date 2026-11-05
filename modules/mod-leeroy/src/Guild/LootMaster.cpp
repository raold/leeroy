// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LootMaster.h"
#include "GuildPorts.h"

#include "Creature.h"
#include "GameObject.h"
#include "Group.h"
#include "Item.h"
#include "LootMgr.h"
#include "Map.h"
#include "MapMgr.h"
#include "ObjectMgr.h"
#include "ScriptMgr.h"

#include <algorithm>
#include <string>
#include <utility>

namespace Leeroy::Guild
{
namespace
{
bool Owned(uint32 low) { return low && (low == HumanGuid() || IsCast(low)); }
LeeroyCommandResult Fail(std::string error) { return {false, std::move(error)}; }

bool OwnedGroup(Group const* group)
{
    return group && group->isRaidGroup() && !group->isLFGGroup() && !group->isBGGroup() && !group->isBFGroup()
        && std::all_of(group->GetMemberSlots().begin(), group->GetMemberSlots().end(), [](auto const& member)
        {
            return Owned(member.guid.GetCounter());
        });
}

bool HasEpic(Loot const& loot)
{
    return std::any_of(loot.items.begin(), loot.items.end(), [](::LootItem const& item)
    {
        ItemTemplate const* info = sObjectMgr->GetItemTemplate(item.itemid);
        return info && info->Quality >= ITEM_QUALITY_EPIC;
    });
}

ObjectGuid GroupGuid(Group const* group) { return group ? group->GetGUID() : ObjectGuid::Empty; }

uint64 Generation(Creature const* creature)
{
    auto const time = creature->GetRespawnTime();
    return time > 0 ? static_cast<uint64>(time) : 0;
}

bool InMatchingRoll(Group const* group, Loot const& loot, uint32 index)
{
    for (Roll const* roll : group->GetRolls())
        if (roll && roll->isValid() && roll->getTarget() == &loot && roll->itemSlot == index)
            return true;
    return false;
}
}

LootSourceLedger& LootSourceLedger::Instance()
{
    static LootSourceLedger ledger;
    return ledger;
}

void LootSourceLedger::Note(uint32 map, uint32 instanceId, uint32 entry, ObjectGuid source)
{
    Note(map, instanceId, entry, source, ObjectGuid::Empty, 0);
}

void LootSourceLedger::Note(uint32 map, uint32 instanceId, uint32 entry, ObjectGuid source,
    ObjectGuid group, uint64 generation)
{
    _state.Note({map, instanceId, entry}, {source.GetRawValue(), group.GetRawValue(), generation});
}

std::optional<ObjectGuid> LootSourceLedger::Find(uint32 map, uint32 instanceId, uint32 entry) const
{
    auto const source = Observe(map, instanceId, entry);
    return source ? std::optional<ObjectGuid>(ObjectGuid(source->source)) : std::nullopt;
}

std::optional<LootSourceObservation> LootSourceLedger::Observe(uint32 map, uint32 instanceId, uint32 entry) const
{
    return _state.Find({map, instanceId, entry});
}

void LootSourceLedger::Invalidate(uint32 map, uint32 instanceId, uint32 entry)
{
    _state.Invalidate({map, instanceId, entry});
}

void LootSourceLedger::Expire() { _state.Expire(); }

void NoteCreatureLoot(Creature* creature)
{
    Map const* map = creature ? creature->GetMap() : nullptr;
    if (!map || !map->IsRaid() || !HasEpic(creature->loot))
        return;
    LootSourceLedger::Instance().Note(map->GetId(), map->GetInstanceId(), creature->GetEntry(),
        creature->GetGUID(), GroupGuid(creature->GetLootRecipientGroup()), Generation(creature));
}

void NoteGameObjectLoot(Player* player, ObjectGuid guid, Loot* loot)
{
    Map* map = player ? player->GetMap() : nullptr;
    if (!map || !map->IsRaid() || !guid.IsGameObject() || !loot || !HasEpic(*loot))
        return;
    GameObject* object = map->GetGameObject(guid);
    if (!object || &object->loot != loot)
        return;
    LootSourceLedger::Instance().Note(map->GetId(), map->GetInstanceId(), object->GetEntry(), guid,
        GroupGuid(object->GetLootRecipientGroup()), object->GetLootGenerationTime());
}

LeeroyCommandResult HandleGiveLoot(nlohmann::json const& message)
{
    std::string error;
    auto const cmd = ParseGiveLoot(message, error);
    if (!cmd)
        return Fail(error);
    if (!Owned(cmd->toGuid))
        return Fail("not_eligible");

    auto& ledger = LootSourceLedger::Instance();
    auto const observed = ledger.Observe(cmd->map, cmd->instanceId, cmd->sourceEntry);
    if (!observed)
        return Fail("no_source");
    Map* map = sMapMgr->FindMap(cmd->map, cmd->instanceId);
    if (!map || !map->IsRaid())
        return Fail("no_source");

    ObjectGuid const sourceGuid(observed->source);
    Creature* creature = sourceGuid.IsCreatureOrVehicle() ? map->GetCreature(sourceGuid) : nullptr;
    GameObject* object = sourceGuid.IsGameObject() ? map->GetGameObject(sourceGuid) : nullptr;
    if ((!creature && !object) || (creature && creature->GetEntry() != cmd->sourceEntry)
        || (object && object->GetEntry() != cmd->sourceEntry))
    {
        ledger.Invalidate(cmd->map, cmd->instanceId, cmd->sourceEntry);
        return Fail("no_source");
    }

    Group* sourceGroup = creature ? creature->GetLootRecipientGroup() : object->GetLootRecipientGroup();
    uint64 const generation = creature ? Generation(creature) : object->GetLootGenerationTime();
    if (!observed->generation || !generation || observed->generation != generation
        || !observed->group || observed->group != GroupGuid(sourceGroup).GetRawValue())
    {
        ledger.Invalidate(cmd->map, cmd->instanceId, cmd->sourceEntry);
        return Fail("no_source");
    }
    if (creature && (!creature->IsInWorld() || creature->IsAlive()
        || !creature->HasDynamicFlag(UNIT_DYNFLAG_LOOTABLE)))
        return Fail("no_source");
    if (object && (!object->IsInWorld() || object->GetGoType() != GAMEOBJECT_TYPE_CHEST
        || !object->GetGOInfo()->chest.groupLootRules || object->getLootState() != GO_ACTIVATED))
        return Fail("no_source");

    Loot& loot = creature ? creature->loot : object->loot;
    if (loot.sourceWorldObjectGUID != sourceGuid || (loot.loot_type != LOOT_NONE && loot.loot_type != LOOT_CORPSE))
        return Fail("no_source");
    // P1a publishes positions in loot.items only. Quest/FFA loot has per-player slots, counters and notifications;
    // it cannot be transferred using the shared master-loot bookkeeping below.
    if (cmd->index >= loot.items.size())
        return Fail("no_item");
    ::LootItem& item = loot.items[cmd->index];
    ItemTemplate const* info = sObjectMgr->GetItemTemplate(item.itemid);
    if (item.itemid != cmd->itemId || item.is_looted || !item.count || !loot.unlootedCount
        || item.freeforall || item.needs_quest || !info || info->Quality < ITEM_QUALITY_EPIC)
        return Fail("no_item");

    Player* target = FindOnline(cmd->toGuid);
    if (Realm::RealmOwns(cmd->toGuid)) return Fail("busy:realm_owner");
    Group* group = target ? target->GetGroup() : nullptr;
    if (!target || target->GetMap() != map || target->IsBeingTeleported() || target->HasPendingBind()
        || target->HasPlayerFlag(PLAYER_FLAGS_NO_PLAY_TIME) || !OwnedGroup(group)
        || group != sourceGroup || group->GetLootMethod() != MASTER_LOOT)
        return Fail("not_eligible");
    ObjectGuid const masterGuid = group->GetMasterLooterGuid();
    Player* master = Owned(masterGuid.GetCounter()) ? FindOnline(masterGuid.GetCounter()) : nullptr;
    if (!master || master->GetGUID() != masterGuid || master->GetGroup() != group || master->GetMap() != map
        || master->IsBeingTeleported() || master->HasPendingBind())
        return Fail("not_eligible");
    if (object && (!object->IsLootAllowedFor(master) || !object->IsLootAllowedFor(target)))
        return Fail("not_eligible");
    if (item.GetAllowedLooters().find(target->GetGUID()) == item.GetAllowedLooters().end()
        || !item.AllowedForPlayer(target, loot.sourceWorldObjectGUID)
        || !sScriptMgr->OnAllowedToLootContainerCheck(master, sourceGuid)
        || item.rollWinnerGUID || InMatchingRoll(group, loot, cmd->index))
        return Fail("not_eligible");

    ItemPosCountVec dest;
    InventoryResult const result = target->CanStoreNewItem(NULL_BAG, NULL_SLOT, dest, item.itemid, item.count);
    if (result != EQUIP_ERR_OK)
    {
        if (result == EQUIP_ERR_CANT_CARRY_MORE_OF_THIS)
            return Fail("unique");
        if (result == EQUIP_ERR_INVENTORY_FULL)
            return Fail("bags_full");
        return Fail("not_eligible");
    }

    AllowedLooterSet allowed = item.GetAllowedLooters();
    uint32 const count = item.count;
    if (Realm::RealmOwns(cmd->toGuid)) return Fail("busy:realm_owner");
    Item* awarded = target->StoreNewItem(dest, item.itemid, true, item.randomPropertyId, allowed);
    if (!awarded)
        return Fail("not_eligible");
    target->SendNewItem(awarded, count, false, false, true);
    target->UpdateLootAchievements(&item, &loot);
    item.count = 0;
    item.is_looted = true;
    loot.NotifyItemRemoved(static_cast<uint8>(cmd->index));
    --loot.unlootedCount;
    PublishMasterLoot(cmd->toGuid, cmd->itemId, info->Name1);
    return {true, {}};
}

void RegisterLootMasterHandler() { RegisterCommand("give_loot", HandleGiveLoot); }
}
