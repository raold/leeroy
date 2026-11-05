// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_PORTS_H
#define MOD_LEEROY_REALM_PORTS_H

#include "RealmModule.h"
#include "LeeroyCommands.h"
#include "LeeroyIntegrations.h"
#include "LeeroyModule.h"
#include "LeeroyPinSet.h"
#include "BotCommands.h"
#include "OfflineRaidJob.h"
#include "TrainingCommands.h"
#include "BattlefieldMgr.h"
#include "CharacterCache.h"
#include "DBCStores.h"
#include "Group.h"
#include "Guild.h"
#include "GuildMgr.h"
#include "Item.h"
#include "LFGMgr.h"
#include "Map.h"
#include "MapMgr.h"
#include "ObjectAccessor.h"
#include "ObjectMgr.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "PlayerbotAIConfig.h"
#include "Playerbots.h"
#include "RandomPlayerbotMgr.h"
#include <chrono>
#include <utility>

#if LEEROY_HAS_DUNGEON_CLEAR
#include "TestRun/DcTestDriver.h"
#endif

namespace Leeroy::Realm
{
inline LeeroyConfig const& Config() { return LeeroyModule::Instance().Config(); }
inline std::uint32_t GuildId() { return LeeroyModule::Instance().GuildId(); }
inline std::uint32_t HumanGuid() { return LeeroyModule::Instance().HumanGuid(); }
inline bool IsCast(std::uint32_t guid) { return LeeroyModule::Instance().Cast().IsCast(guid); }
inline std::unordered_set<std::uint32_t> CastMembers() { return LeeroyModule::Instance().Cast().Guids(); }
inline ObjectGuid PlayerGuid(std::uint32_t guid) { return ObjectGuid::Create<HighGuid::Player>(guid); }
inline std::uint64_t NowMs()
{
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}
inline bool RegisterCommand(std::string const& type, LeeroyCommandHandler handler)
{
    return LeeroyRegisterCommand(type, std::move(handler));
}
inline void Publish(nlohmann::json const& body, bool droppable = false)
{
    LeeroyModule::Instance().Publish(EncodeEvent(NowUnixMs(), body), droppable);
}
inline Player* FindOnline(std::uint32_t guid)
{
    Player* player = ObjectAccessor::FindConnectedPlayer(PlayerGuid(guid));
    return player && player->IsInWorld() && !player->IsDuringRemoveFromWorld() && player->GetSession() ? player : nullptr;
}
inline bool IsOnRandomBotAccount(std::uint32_t guid)
{
    auto const* character = sCharacterCache->GetCharacterCacheByGuid(PlayerGuid(guid));
    return character && sPlayerbotAIConfig.IsInRandomAccountList(character->AccountId);
}
inline bool IsConfiguredGuildMember(std::uint32_t guid)
{
    auto const guildId = GuildId();
    if (!guildId || !guid) return false;
    auto const objectGuid = PlayerGuid(guid);
    auto const* guild = sGuildMgr->GetGuildById(guildId);
    if (guild && guild->GetMember(objectGuid)) return true;
    auto const* character = sCharacterCache->GetCharacterCacheByGuid(objectGuid);
    if (character && character->GuildId == guildId) return true;
    auto const* player = ObjectAccessor::FindConnectedPlayer(objectGuid);
    return player && player->GetGuildId() == guildId;
}
inline std::uint32_t SetRealmPins(std::unordered_set<std::uint32_t> const& guids)
{
    LeeroySetExtraPins("realm", guids, kPinMute | kPinWatch);
    return LeeroyApplyPins();
}
inline bool IsPinned(std::uint32_t guid) { return sRandomPlayerbotMgr.IsPinned(guid); }
inline bool ValidPlace(Place const& place)
{
    auto const* map = sMapStore.LookupEntry(place.map);
    return map && !map->Instanceable() && !sObjectMgr->IsTransportMap(place.map)
        && MapMgr::IsValidMapCoord(place.map, place.x, place.y, place.z, place.o)
        && sMapMgr->GetZoneId(PHASEMASK_NORMAL, place.map, place.x, place.y, place.z) == place.zone;
}
inline WeaponNow ReadWeapons(Player* player)
{
    WeaponNow result;
    if (!player)
        return result;
    auto* main = player->GetItemByPos(INVENTORY_SLOT_BAG_0, EQUIPMENT_SLOT_MAINHAND);
    auto* off = player->GetItemByPos(INVENTORY_SLOT_BAG_0, EQUIPMENT_SLOT_OFFHAND);
    auto* ranged = player->GetItemByPos(INVENTORY_SLOT_BAG_0, EQUIPMENT_SLOT_RANGED);
    result.mainHand = main ? main->GetGUID().GetCounter() : 0;
    result.offHand = off ? off->GetGUID().GetCounter() : 0;
    result.ranged = ranged ? ranged->GetGUID().GetCounter() : 0;
    result.mainIsPole = main && main->GetTemplate()->Class == ITEM_CLASS_WEAPON
        && main->GetTemplate()->SubClass == ITEM_SUBCLASS_WEAPON_FISHING_POLE;
    return result;
}
inline bool IsRealGuild(std::uint32_t guildId)
{
    if (!guildId)
        return false;
    ::Guild const* guild = sGuildMgr->GetGuildById(guildId);
    if (!guild)
        return true; // Unknown guild ownership cannot authorize leaving it.
    for (auto const& [guid, member] : guild->GetUnorderedMembers())
    {
        (void)member;
        if (guid == HumanGuid() || !IsOnRandomBotAccount(guid))
            return true;
    }
    return false;
}
inline Player* GmIssuer(std::string& why)
{
#if LEEROY_HAS_DUNGEON_CLEAR
    std::string detail;
    auto const ready = DcTestDriver::Ensure(&detail);
    if (ready == DcTestDriver::Readiness::Ready)
        if (auto* driver = DcTestDriver::Get())
            return driver;
    why = ready == DcTestDriver::Readiness::PendingLogin ? "driver_pending" : "driver_unavailable:" + detail;
#else
    why = "driver_unavailable:no_dungeon_clear";
#endif
    return nullptr;
}
inline BodyState Body(Player* player)
{
    BodyState state;
    if (!player || !player->IsInWorld() || player->IsDuringRemoveFromWorld() || !player->GetSession())
        return state;
    state.online = true;
    state.dead = player->isDead();
    state.combat = player->IsInCombat();
    state.flight = player->IsInFlight();
    Group* group = player->GetGroup();
    state.raidGroup = group && group->isRaidGroup();
    state.pvpOrLfgGroup = group && (group->isBGGroup() || group->isBFGroup() || group->isLFGGroup());
    state.humanGroup = group && group->IsMember(PlayerGuid(HumanGuid()));
    state.instance = !player->GetMap() || player->GetMap()->Instanceable();
    state.battleground = player->InBattleground();
    state.arena = player->InArena();
    state.lfg = sLFGMgr->GetState(player->GetGUID()) != lfg::LFG_STATE_NONE;
    return state;
}
inline HumanPvp Human()
{
    HumanPvp human;
    auto* player = FindOnline(HumanGuid());
    if (!player)
        return human;
    human.online = true;
    human.zone = player->GetZoneId();
    human.manualFlag = player->HasPlayerFlag(PLAYER_FLAGS_IN_PVP);
    human.battleground = player->InBattleground();
    human.arena = player->InArena();
    auto* field = sBattlefieldMgr->GetBattlefieldToZoneId(human.zone);
    human.wgWar = human.zone == 4197 && field && field->IsWarTime();
    return human;
}

// World-thread, read-only. Gameplay waivers never bypass this ownership fence.
// A coarse unknown cleanup blocks conservatively; it is never cleared here.
inline std::string NativeOwnershipReason(std::uint32_t guid)
{
    if (Leeroy::Guild::OfflineRaidOwns(guid) || Leeroy::Training::DummyTrialOwns(guid))
        return "busy:training_owner";
    if (Leeroy::Training::TrainingCleanupPending() || Leeroy::Guild::OfflineTrainingCleanupPending())
        return "busy:training_cleanup";
    if (Leeroy::Guild::HasPendingBotCommands({guid}) || Leeroy::Training::HasTrainingJournal(guid)
        || Leeroy::Training::TrainingDcOwns(guid))
        return "busy:training_state";
    return "";
}
inline std::string NativeMutationReason(std::uint32_t guid)
{
    if (auto reason = NativeOwnershipReason(guid); !reason.empty())
        return reason;
    if (!guid || guid == HumanGuid())
        return "not_bot";
    if (!IsOnRandomBotAccount(guid))
        return "not_bot";
    auto* player = ObjectAccessor::FindConnectedPlayer(PlayerGuid(guid));
    if (player)
    {
        if (FindOnline(guid) != player || !GET_PLAYERBOT_AI(player) || IsSelfBot(player))
            return "busy:body_unavailable";
        auto const state = Body(player);
        if (state.humanGroup || (state.raidGroup && !state.pvpOrLfgGroup))
            return "busy:raid_owner";
    }
    return "";
}

inline std::string AdmissionReason(std::uint32_t guid, Purpose purpose, std::string_view operation = {})
{
    auto& module = RealmModule::Instance();
    if (!module.On())
        return "realm_off";
    if (!IsCast(guid) && !module.IsActor(guid))
        return "not_actor";
    if (auto reason = module.MutationReason(guid, operation); !reason.empty())
        return reason;
    auto reason = BusyReason(Body(FindOnline(guid)), purpose);
    return reason.empty() ? "" : "busy:" + reason;
}

inline std::optional<BodyIdentity> Identity(std::uint32_t guid, bool allowTransit = false)
{
    auto const* character = sCharacterCache->GetCharacterCacheByGuid(PlayerGuid(guid));
    if (!character)
        return std::nullopt;
    BodyIdentity identity;
    identity.account = character->AccountId;
    identity.guild = character->GuildId;
    identity.classId = character->Class;
    identity.race = character->Race;
    identity.sex = character->Sex;
    identity.group = character->GroupGuid.GetRawValue();
    identity.player = ObjectAccessor::FindConnectedPlayer(PlayerGuid(guid));
    if (identity.player)
    {
        if (FindOnline(guid) != identity.player && !(allowTransit && identity.player->IsBeingTeleported()
            && identity.player->GetSession() && GET_PLAYERBOT_AI(identity.player)))
            return std::nullopt;
        identity.ai = GET_PLAYERBOT_AI(identity.player);
        identity.session = identity.player->GetSession();
        identity.guild = identity.player->GetGuildId();
        identity.map = identity.player->GetMapId();
        identity.instance = identity.player->GetInstanceId();
        auto* group = identity.player->GetGroup();
        identity.group = group ? group->GetGUID().GetRawValue() : 0;
    }
    return identity;
}
}
#endif
