// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyQueueFence.h"
#include "LeeroyQueueFencePolicy.h"
#include "LeeroyModule.h"
#include "RealmModule.h"
#include "RealmPvp.h"

#include "ArenaTeam.h"
#include "ArenaTeamMgr.h"
#include "Battlefield.h"
#include "BattlefieldMgr.h"
#include "Battleground.h"
#include "BattlegroundMgr.h"
#include "BattlegroundQueue.h"
#include "DBCStores.h"
#include "GlobalScript.h"
#include "Group.h"
#include "LFGMgr.h"
#include "Log.h"
#include "Map.h"
#include "ObjectAccessor.h"
#include "Opcodes.h"
#include "Player.h"
#include "PlayerScript.h"
#include "RandomPlayerbotMgr.h"
#include "ServerScript.h"
#include "WorldPacket.h"
#include "WorldSession.h"

#include <map>
#include <mutex>
#include <set>

namespace
{
using Leeroy::QueueKind;
struct Held
{
    std::uint32_t guid = 0;
    std::uintptr_t session = 0;
    std::string reason, key;
};
std::mutex mutex;
std::map<std::uint32_t, Held> held;
std::map<std::uintptr_t, std::uint32_t> sessions;
std::map<ObjectGuid, std::set<std::uint32_t>> groups;
Leeroy::QueueFenceLog logGate;

Player* Find(std::uint32_t guid)
{
    return ObjectAccessor::FindConnectedPlayer(ObjectGuid::Create<HighGuid::Player>(guid));
}

// World thread only. Hooks that can run on a map/session thread use the copied
// snapshot instead of consulting Realm's world-owned reservations or players.
Held Current(Player* player, QueueKind kind, bool ownerRequest = false)
{
    if (!player) return {};
    auto& module = LeeroyModule::Instance();
    auto guid = player->GetGUID().GetCounter();
    Leeroy::QueueFenceState state;
    state.enabled = module.Enabled() && module.Config().queueFence;
    state.cast = module.Cast().IsCast(guid);
    state.human = guid == module.HumanGuid();
    if (!state.enabled || !state.cast || state.human) return {};
    Group* group = player->GetGroup();
    state.ownerRequest = ownerRequest;
    state.ownerGroup = group && group->IsMember(ObjectGuid::Create<HighGuid::Player>(module.HumanGuid()));
    state.humanGroupInInstance = group && player->GetMap() && player->GetMap()->Instanceable()
        && group->IsMember(ObjectGuid::Create<HighGuid::Player>(module.HumanGuid()));
    auto& realmModule = Leeroy::Realm::RealmModule::Instance();
    state.reason = realmModule.MutationReason(guid);
    auto reason = Leeroy::QueueFenceReason(state, kind);
    if (reason.empty()) return {};
    std::string key(reason);
    if (auto owner = realmModule.Holder(guid); !owner.empty()) key += ":" + owner;
    else if (group) key += ":" + std::to_string(group->GetGUID().GetRawValue());
    return {guid, reinterpret_cast<std::uintptr_t>(player->GetSession()), std::string(reason), std::move(key)};
}

bool RefuseLocked(Held const& member)
{
    if (logGate.Refuse(member.guid, member.key))
        LOG_INFO("module.leeroy", "queue_fence_refused guid={} owner={}", member.guid, member.reason);
    return false;
}

bool NativeCall(Player* player, QueueKind kind)
{
    return player && Leeroy::Realm::RealmPvpQueueCallOwns(player->GetGUID().GetCounter(),
        reinterpret_cast<std::uintptr_t>(player->GetSession()), kind);
}

// Queue admissions are PROCESS_THREADUNSAFE handlers, hence world-thread.
// Check all members: an unheld leader must not enqueue somebody else's body.
bool AllowJoin(Player* player, QueueKind kind, bool asGroup)
{
    if (!player || NativeCall(player, kind)) return true;
    bool ownerRequest = player->GetGUID().GetCounter() == LeeroyModule::Instance().HumanGuid();
    auto check = [kind, ownerRequest](Player* member)
    {
        auto current = Current(member, kind, ownerRequest);
        if (!current.guid) return true;
        std::lock_guard lock(mutex);
        return RefuseLocked(current);
    };
    if (!asGroup || !player->GetGroup()) return check(player);
    bool allowed = true;
    for (auto const& slot : player->GetGroup()->GetMemberSlots())
        if (!check(Find(slot.guid.GetCounter()))) allowed = false;
    return allowed;
}

// World-thread withdrawal. Never interfere with a Realm lease, a group queue,
// rated-arena penalties, an active match, an LFG proposal or a dungeon group.
void Withdraw(Player* player, Held const& member)
{
    if (!Leeroy::Realm::RealmModule::Instance().Holder(member.guid).empty()) return;
    if (player->IsBeingTeleported()) return;
    bool removed = false;
    if (!player->InBattleground() && !player->IsBeingTeleported())
        for (uint8 slot = 0; slot < PLAYER_MAX_BATTLEGROUND_QUEUES; ++slot)
        {
            auto type = player->GetBattlegroundQueueTypeId(slot);
            if (type == BATTLEGROUND_QUEUE_NONE) continue;
            auto& queue = sBattlegroundMgr->GetBattlegroundQueue(type);
            GroupQueueInfo info;
            if (!queue.GetPlayerGroupInfoData(player->GetGUID(), &info)
                || info.IsRated || info.Players.size() != 1) continue;
            auto* bg = sBattlegroundMgr->GetBattlegroundTemplate(info.BgTypeId);
            if (!bg) continue;
            queue.RemovePlayer(player->GetGUID(), true);
            player->RemoveBattlegroundQueueId(type);
            WorldPacket packet;
            sBattlegroundMgr->BuildBattlegroundStatusPacket(&packet, bg, slot, STATUS_NONE, 0, 0, 0, TEAM_NEUTRAL);
            player->SendDirectMessage(&packet);
            sBattlegroundMgr->ScheduleQueueUpdate(info.ArenaMatchmakerRating, info.ArenaType,
                type, info.BgTypeId, BattlegroundBracketId(info.BracketId));
            removed = true;
        }
    auto state = sLFGMgr->GetState(player->GetGUID());
    if (state == lfg::LFG_STATE_RAIDBROWSER
        || (state == lfg::LFG_STATE_QUEUED && !sLFGMgr->GetGroup(player->GetGUID())))
    {
        sLFGMgr->LeaveLfg(player->GetGUID());
        removed = true;
    }
    if (auto* field = sBattlefieldMgr->GetBattlefieldByBattleId(BATTLEFIELD_BATTLEID_WG))
        if (!field->GetPlayersInWarSet(TEAM_ALLIANCE).count(player->GetGUID())
            && !field->GetPlayersInWarSet(TEAM_HORDE).count(player->GetGUID())
            && !field->GetInvitedPlayersMap(TEAM_ALLIANCE).count(player->GetGUID())
            && !field->GetInvitedPlayersMap(TEAM_HORDE).count(player->GetGUID())
            && field->GetPlayersQueueSet(player->GetTeamId()).count(player->GetGUID()))
        {
            field->AskToLeaveQueue(player);
            removed = true;
        }
    if (removed)
    {
        std::lock_guard lock(mutex);
        RefuseLocked(member);
    }
}

// World thread. Leaves through ArenaTeam::DelMember only under the core leave
// handler's own conditions (DecideCastArenaTeamExit), at most once per member and
// team in this process, and logs whether the member's slot actually cleared.
std::set<std::pair<std::uint32_t, std::uint32_t>> captainsLogged, leaveTried;
void LeaveCastArenaTeams()
{
    auto& module = LeeroyModule::Instance();
    bool const enabled = module.Enabled() && module.Config().castArenaTeamsLeave;
    if (!enabled) { captainsLogged.clear(); leaveTried.clear(); return; }
    auto const human = ObjectGuid::Create<HighGuid::Player>(module.HumanGuid());
    for (auto guid : module.Cast().Guids())
    {
        auto* player = Find(guid);
        if (!player || !player->IsInWorld() || player->IsBeingTeleported()) continue;
        for (uint8 slot = 0; slot < MAX_ARENA_SLOT; ++slot)
        {
            auto const id = player->GetArenaTeamId(slot);
            auto* team = id ? sArenaTeamMgr->GetArenaTeamById(id) : nullptr;
            if (!team || !team->IsMember(player->GetGUID())) continue;
            Leeroy::CastArenaTeamFacts facts;
            facts.enabled = enabled;
            facts.cast = module.Cast().IsCast(guid);
            facts.pinned = sRandomPlayerbotMgr.IsPinned(guid);
            facts.human = guid == module.HumanGuid();
            facts.ownerCaptain = module.HumanGuid() && team->GetCaptain() == human;
            facts.memberCaptain = team->GetCaptain() == player->GetGUID();
            facts.realmHeld = !Leeroy::Realm::RealmModule::Instance().Holder(guid).empty();
            facts.teamFighting = team->IsFighting();
            facts.inBattleground = player->InBattleground();
            facts.inQueue = player->InBattlegroundQueue();
            if (auto* group = player->GetGroup())
                for (auto const& member : group->GetMemberSlots())
                    if (auto* mate = Find(member.guid.GetCounter()); mate && mate != player && mate->InBattlegroundQueue())
                        facts.groupQueued = true;
            auto const exit = Leeroy::DecideCastArenaTeamExit(facts);
            if (exit == Leeroy::CastArenaTeamExit::Captain && captainsLogged.emplace(guid, id).second)
                LOG_WARN("module.leeroy", "leeroy: cast_arena_team_kept guid={} team={} reason=captain", guid, id);
            if (exit != Leeroy::CastArenaTeamExit::Leave || !leaveTried.emplace(guid, id).second) continue;
            auto const type = team->GetType();
            team->DelMember(player->GetGUID(), true);
            if (!player->GetArenaTeamId(slot) && !team->IsMember(player->GetGUID()))
                LOG_INFO("module.leeroy", "leeroy: cast_arena_team_left guid={} team={} type={}", guid, id, uint32(type));
            else
                LOG_WARN("module.leeroy", "leeroy: cast_arena_team_leave_unproved guid={} team={} type={}",
                    guid, id, uint32(type));
        }
    }
}

class QueuePlayerScript final : public PlayerScript
{
public:
    QueuePlayerScript() : PlayerScript("LeeroyQueueFencePlayer", {PLAYERHOOK_CAN_JOIN_IN_BATTLEGROUND_QUEUE,
        PLAYERHOOK_CAN_JOIN_IN_ARENA_QUEUE, PLAYERHOOK_CAN_JOIN_LFG, PLAYERHOOK_CAN_BATTLEFIELD_PORT,
        PLAYERHOOK_ON_LOGIN, PLAYERHOOK_ON_LOGOUT}) { }
    void ForgetSession(Player* player)
    {
        if (!player) return;
        std::lock_guard lock(mutex);
        // A recycled session address must never inherit another character's WG
        // fence. Admission is checked live; the next tick publishes this life.
        auto found = sessions.find(reinterpret_cast<std::uintptr_t>(player->GetSession()));
        if (found != sessions.end()) { held.at(found->second).session = 0; sessions.erase(found); }
    }
    void OnPlayerLogin(Player* player) override { ForgetSession(player); }
    void OnPlayerLogout(Player* player) override { ForgetSession(player); }
    bool OnPlayerCanJoinInBattlegroundQueue(Player* player, ObjectGuid, BattlegroundTypeId, uint8 asGroup,
        GroupJoinBattlegroundResult& error) override
    {
        if (AllowJoin(player, QueueKind::Battleground, asGroup)) return true;
        error = ERR_BATTLEGROUND_JOIN_FAILED; return false;
    }
    bool OnPlayerCanJoinInArenaQueue(Player* player, ObjectGuid, uint8, BattlegroundTypeId, uint8 asGroup,
        uint8, GroupJoinBattlegroundResult& error) override
    {
        if (AllowJoin(player, QueueKind::Arena, asGroup)) return true;
        error = ERR_BATTLEGROUND_JOIN_FAILED; return false;
    }
    bool OnPlayerCanJoinLfg(Player* player, uint8, std::set<uint32>& dungeons, std::string const&) override
    {
        bool raid = false;
        for (auto id : dungeons)
            if (auto* dungeon = sLFGDungeonStore.LookupEntry(id & 0x00FFFFFF))
                raid = raid || dungeon->TypeID == lfg::LFG_TYPE_RAID;
        return AllowJoin(player, raid ? QueueKind::RaidBrowser : QueueKind::DungeonFinder, true);
    }
    bool OnPlayerCanBattleFieldPort(Player* player, uint8 arena, BattlegroundTypeId, uint8 action) override
    {
        // Declines/cleanup remain possible. Playerbots also calls this handler
        // directly from AI updates, so read only our synchronized snapshot.
        if (!player || !action || NativeCall(player, arena ? QueueKind::Arena : QueueKind::Battleground)) return true;
        std::lock_guard lock(mutex);
        auto found = held.find(player->GetGUID().GetCounter());
        return found == held.end() || found->second.session != reinterpret_cast<std::uintptr_t>(player->GetSession())
            || RefuseLocked(found->second);
    }
};

class QueueServerScript final : public ServerScript
{
public:
    QueueServerScript() : ServerScript("LeeroyQueueFencePackets", {SERVERHOOK_CAN_PACKET_RECEIVE}) { }
    bool CanPacketReceive(WorldSession* session, WorldPacket const& packet) override
    {
        if (packet.GetOpcode() != CMSG_BATTLEFIELD_MGR_QUEUE_INVITE_RESPONSE
            && packet.GetOpcode() != CMSG_BATTLEFIELD_MGR_ENTRY_INVITE_RESPONSE) return true;
        // Declines must reach core; only WG accepts, not unrelated battlefields.
        if (packet.size() < 5 || packet.read<uint32>(0) != BATTLEFIELD_BATTLEID_WG
            || !packet.read<uint8>(4)) return true;
        std::lock_guard lock(mutex);
        auto found = sessions.find(reinterpret_cast<std::uintptr_t>(session));
        return found == sessions.end() || RefuseLocked(held.at(found->second));
    }
};

class QueueGlobalScript final : public GlobalScript
{
public:
    QueueGlobalScript() : GlobalScript("LeeroyQueueFenceLfg", {GLOBALHOOK_CAN_CREATE_LFG_PROPOSAL}) { }
    bool CanCreateLfgProposal(lfg::Lfg5Guids const& queues) override
    {
        // Compatibility search can run on a map thread. Group IDs and members
        // were copied together at the end of the world tick.
        std::lock_guard lock(mutex);
        bool allowed = true;
        for (auto guid : queues.guids)
        {
            if (guid.IsPlayer())
            {
                auto found = held.find(guid.GetCounter());
                if (found != held.end()) { RefuseLocked(found->second); allowed = false; }
            }
            else if (auto found = groups.find(guid); found != groups.end())
                for (auto member : found->second) { RefuseLocked(held.at(member)); allowed = false; }
        }
        return allowed;
    }
};
}

void LeeroyRefreshQueueFence()
{
    std::map<std::uint32_t, Held> next;
    std::map<std::uintptr_t, std::uint32_t> nextSessions;
    std::map<ObjectGuid, std::set<std::uint32_t>> nextGroups;
    auto& module = LeeroyModule::Instance();
    if (module.Enabled() && module.Config().queueFence)
        for (auto guid : module.Cast().Guids())
            if (auto* player = Find(guid))
            {
                auto member = Current(player, QueueKind::Battleground);
                if (!member.guid) continue;
                next[guid] = member;
                if (member.session) nextSessions[member.session] = guid;
                if (auto* group = player->GetGroup()) nextGroups[group->GetGUID()].insert(guid);
            }
    {
        std::lock_guard lock(mutex);
        for (auto const& [guid, member] : held)
            if (!next.count(guid)) logGate.Observe(guid, {});
        for (auto const& [guid, member] : next) logGate.Observe(guid, member.key);
        held = next;
        sessions = std::move(nextSessions);
        groups = std::move(nextGroups);
    }
    for (auto const& [guid, member] : next)
        if (auto* player = Find(guid)) Withdraw(player, member);
    LeaveCastArenaTeams();
}

void AddSC_leeroy_queue_fence()
{
    new QueuePlayerScript(); new QueueServerScript(); new QueueGlobalScript();
}
