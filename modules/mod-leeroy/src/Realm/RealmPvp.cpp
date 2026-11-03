// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "RealmPvp.h"
#include "RealmPorts.h"
#include "LeeroyRealmPvpProof.h"
#include "ArenaTeam.h"
#include "ArenaTeamMgr.h"
#include "ArenaScript.h"
#include "Battlefield.h"
#include "BattlefieldScript.h"
#include "Battleground.h"
#include "BattlegroundMgr.h"
#include "BattlegroundQueue.h"
#include "AllBattlegroundScript.h"
#include "Creature.h"
#include "GameTime.h"
#include "GroupMgr.h"
#include "GroupScript.h"
#include "Opcodes.h"
#include "PlayerScript.h"
#include "ServerScript.h"
#include "UpdateTime.h"
#include "WorldPacket.h"
#include "WorldSession.h"
#include <algorithm>
#include <array>
#include <cmath>
#include <deque>
#include <limits>
#include <map>
#include <mutex>
#include <set>

namespace Leeroy::Realm
{
namespace
{
using Json = nlohmann::json;
constexpr std::size_t MaxOwners = ActionQueue::Capacity, MaxInstances = 1024, MaxEnds = 128;
constexpr std::uint32_t WgZone = 4197;
struct NativeInstance
{
    std::uint64_t generation = 0;
    std::uintptr_t object = 0;
    std::uint32_t instance = 0, map = 0, type = 0;
    std::uint64_t nativeOwnerEpoch = 0;
    bool nativeOwned = false, endCopied = false;
};
struct MemberFacts
{
    ArenaQueueIdentity arenaQueue;
    ArenaMatchIdentity arenaMatch;
    std::uint64_t arenaEpoch = 0;
    std::uint64_t add = 0, remove = 0, group = 0, wgInvite = 0, wgJoin = 0, wgLeave = 0, wgZone = 0;
    std::uint64_t wgTravel = 0, wgArrival = 0, wgLeaveComplete = 0;
    PvpDestination wgDestination;
    std::uint32_t map = 0, instance = 0, wgExpiry = 0;
    std::uint8_t wgTravelKind = 0; // 1: normal war teleport, 2: normal homebind departure
    bool entering = false, wgFar = false;
};
struct OwnerFacts
{
    std::uint64_t revision = 0, insertion = 0, createdGroup = 0, groupGeneration = 0, disband = 0;
    std::string poison;
    std::map<std::uint32_t, MemberFacts> members;
    std::uintptr_t battlefield = 0;
};
struct Watch
{
    std::string token;
    std::uintptr_t session = 0;
    std::array<std::uint32_t, MAX_ARENA_SLOT> teams{};
    bool wintergrasp = false;
};
struct EndFact
{
    std::uint64_t epoch = 0, generation = 0;
    std::uint32_t instance = 0, type = 0, winner = 0;
    std::array<std::uint32_t, 2> teams{};
    std::vector<std::uint32_t> members;
    bool nativeOwned = false;
};
// Hooks retain bounded values only, never core pointers. The session/object
// numbers are opaque correlation keys and are never dereferenced from storage.
struct Inbox
{
    std::mutex mutex;
    std::uint64_t serial = 0, overflow = 0, wgCycle = 1, wgEnds = 0, wgEndEpoch = 0;
    std::uint32_t wgDefender = 0;
    std::map<std::uint32_t, Watch> watches;
    std::map<std::uintptr_t, std::uint32_t> sessions;
    std::map<std::uint32_t, std::size_t> teams;
    std::map<std::string, OwnerFacts> owners;
    std::map<std::uint32_t, NativeInstance> instances;
    std::deque<EndFact> ends;
    WintergraspHistory wgHistory;
} inbox;
bool ForeignWgBaseline(Player const* player)
{
    std::lock_guard lock(inbox.mutex);
    return !inbox.wgHistory.Allows(player->GetGUID().GetCounter(), reinterpret_cast<std::uintptr_t>(player->GetSession()));
}
std::uint64_t SerialLocked()
{
    if (inbox.serial == std::numeric_limits<std::uint64_t>::max()) { ++inbox.overflow; return 0; }
    return ++inbox.serial;
}
struct Capability
{
    std::string token;
    std::uint32_t guid = 0;
    bool queueInvite = false, wgQueue = false;
    std::uint64_t insertion = 0, group = 0, groupGeneration = 0;
    std::uint8_t wgTravelKind = 0;
    std::uint32_t wgDefender = 0;
    PvpDestination wgDestination;
};
thread_local Capability* capability = nullptr;
struct NativeCall
{
    Capability value;
    Capability* previous;
    NativeCall(std::string token, std::uint32_t guid = 0) : value{std::move(token), guid}, previous(capability)
        { capability = &value; }
    ~NativeCall() { capability = previous; }
};
struct ArenaAttempt
{
    ArenaLineage proof;
    std::uint32_t team = 0, captain = 0, type = 0;
    std::vector<std::uint32_t> roster, players;
    std::uint32_t rating = 0;
    std::uint64_t group = 0, groupGeneration = 0, fallbackAt = 0;
    std::map<std::uint32_t, BodyScope> origins, exits;
    std::optional<Place> fallbackTarget;
    bool grouped = false, queued = false, result = false, fallback = false, queueAttempted = false;
};
struct WgMember
{
    bool queued = false, queueRemoved = false, everExposed = false, accepted = false, joined = false, leaving = false;
    std::uint64_t inviteReceipt = 0, leaveReceipt = 0, travelReceipt = 0, group = 0;
    std::uint32_t expiry = 0, team = 0;
    BodyScope origin, exit;
};
struct Attempt
{
    std::uint64_t overflow = 0, cycle = 0;
    std::uintptr_t battlefield = 0;
    bool enrollmentReported = false;
    std::string failure;
    ArenaAttempt arena;
    std::map<std::uint32_t, WgMember> wg;
};
std::map<std::string, Attempt> attempts; // world-thread, one per existing reservation

std::uint64_t GroupId(Player* player)
{
    return player && player->GetGroup() ? player->GetGroup()->GetGUID().GetRawValue() : 0;
}
std::vector<std::uint32_t> Roster(ArenaTeam* team)
{
    std::vector<std::uint32_t> result;
    if (team) for (auto const& member : team->GetMembers()) result.push_back(member.Guid.GetCounter());
    std::sort(result.begin(), result.end()); return result;
}
std::vector<std::uint32_t> GroupMembers(Group const* group)
{
    std::vector<std::uint32_t> result;
    if (group) for (auto const& member : group->GetMemberSlots()) result.push_back(member.guid.GetCounter());
    std::sort(result.begin(), result.end()); return result;
}
Group* ExactGroup(std::uint64_t raw)
{
    if (!raw) return nullptr;
    auto* group = sGroupMgr->GetGroupByGUID(ObjectGuid(raw).GetCounter());
    return group && group->GetGUID().GetRawValue() == raw ? group : nullptr;
}
bool Included(std::vector<std::uint32_t> const& all, std::vector<std::uint32_t> const& subset)
{
    return std::includes(all.begin(), all.end(), subset.begin(), subset.end());
}
PvpDestination Destination(WorldLocation const& location)
{
    return {location.GetMapId(), location.GetPositionX(), location.GetPositionY(), location.GetPositionZ(), location.GetOrientation()};
}
bool AtDestination(Player const* player, PvpDestination const& destination, bool isFarTeleport)
{
    return player && player->GetMapId() == destination.map && !player->GetInstanceId()
        && std::abs(player->GetPositionX() - destination.x) <= 0.1f
        && std::abs(player->GetPositionY() - destination.y) <= 0.1f
        && std::abs(player->GetPositionZ() - destination.z - (isFarTeleport ? player->GetHoverHeight() : 0.0f)) <= 0.1f
        && std::abs(player->GetOrientation() - destination.o) <= 0.01f;
}
Battlefield* Wintergrasp()
{
    auto* field = sBattlefieldMgr->GetBattlefieldByBattleId(BATTLEFIELD_BATTLEID_WG);
    return field && field->IsEnabled() ? field : nullptr;
}
bool WgSources(Battlefield* field, std::uint32_t guid)
{
    if (!field) return false;
    for (auto side : {TEAM_ALLIANCE, TEAM_HORDE})
        if (field->GetPlayersQueueSet(side).count(PlayerGuid(guid))
            || field->GetInvitedPlayersMap(side).count(PlayerGuid(guid))
            || field->GetPlayersInWarSet(side).count(PlayerGuid(guid))) return true;
    return false;
}
bool AnyQueue(std::uint32_t guid)
{
    for (std::uint32_t type = 1; type < MAX_BATTLEGROUND_QUEUE_TYPES; ++type)
    {
        GroupQueueInfo row;
        if (sBattlegroundMgr->GetBattlegroundQueue(static_cast<BattlegroundQueueTypeId>(type))
            .GetPlayerGroupInfoData(PlayerGuid(guid), &row)) return true;
    }
    return false;
}
std::string Eligible(std::uint32_t guid, std::string_view operation)
{
    auto& module = RealmModule::Instance();
    auto const* character = sCharacterCache->GetCharacterCacheByGuid(PlayerGuid(guid));
    if (!IsCast(guid) && !module.IsActor(guid)
        && (!character || !module.RealmGuilds().count(character->GuildId))) return "not_actor";
    if (auto why = module.MutationReason(guid, operation); !why.empty()) return why;
    auto* player = FindOnline(guid);
    if (!player) return "busy:offline";
    auto busy = BusyReason(Body(player), Purpose::Move);
    if (!busy.empty()) return "busy:" + busy;
    if (player->GetGroup() || player->GetOriginalGroup() || player->GetGroupInvite()) return "busy:group";
    if (player->duel || player->IsBeingTeleported()) return "busy:transition";
    if (player->InBattlegroundQueue() || AnyQueue(guid) || WgSources(Wintergrasp(), guid) || player->GetZoneId() == WgZone
        || sLFGMgr->GetState(player->GetGUID()) != lfg::LFG_STATE_NONE)
        return "busy:pvp_source";
    return "";
}
bool TeamFields(ArenaTeam* team, std::vector<std::uint32_t> const& members, bool allowTransit = false)
{
    if (!team || Roster(team) != members || !team->IsMember(team->GetCaptain())) return false;
    for (auto guid : members)
    {
        auto* player = allowTransit ? ObjectAccessor::FindConnectedPlayer(PlayerGuid(guid)) : FindOnline(guid);
        auto const* character = sCharacterCache->GetCharacterCacheByGuid(PlayerGuid(guid));
        if (!player || !player->GetSession() || (FindOnline(guid) != player && !player->IsBeingTeleported())
            || !character || player->GetArenaTeamId(team->GetSlot()) != team->GetId()
            || character->ArenaTeamId[team->GetSlot()] != team->GetId()
            || player->GetUInt32Value(PLAYER_FIELD_ARENA_TEAM_INFO_1_1 + team->GetSlot() * ARENA_TEAM_END
                + ARENA_TEAM_MEMBER) != (PlayerGuid(guid) == team->GetCaptain() ? 0u : 1u)) return false;
    }
    return true;
}
RealmStep TeamEdit(Queued& entry, std::uint32_t)
{
    if (entry.payload.value("done", false)) return {StepState::Complete, "", entry.payload.value("value", 0u)};
    auto& module = RealmModule::Instance();
    for (auto guid : entry.guids)
        if (auto why = Eligible(guid, entry.id); !why.empty()) return {StepState::Refused, why};
    auto const action = entry.payload.at("action").get<std::string>();
    auto const id = entry.payload.value("team_id", 0u), target = entry.payload.value("guid", 0u);
    auto* team = sArenaTeamMgr->GetArenaTeamById(id);
    auto before = Roster(team);
    auto expected = before;
    auto const slot = ArenaTeam::GetSlotByType(entry.payload.value("arena_type", team ? team->GetType() : 0u));
    if (action != "create")
    {
        if (!team || !TeamFields(team, before) || team->IsFighting()) return {StepState::Refused, "no_team"};
        auto closure = before;
        if (action == "add") closure.push_back(target);
        std::sort(closure.begin(), closure.end()); closure.erase(std::unique(closure.begin(), closure.end()), closure.end());
        if (closure != entry.guids) return {StepState::Refused, "arena_roster_changed"};
    }
    else
    {
        auto const name = entry.payload.at("name").get<std::string>();
        if (sArenaTeamMgr->GetArenaTeamByName(name)) return {StepState::Refused, "name_taken"};
        if (FindOnline(entry.guids.front())->GetArenaTeamId(slot)) return {StepState::Refused, "already_in_team"};
    }
    if (auto why = module.ChargeAffectedBodies(entry.id, entry.guids); !why.empty())
        return {why == "busy:tick_budget" ? StepState::Pending : StepState::Refused, why};
    std::uint32_t result = id;
    if (action == "create")
    {
        auto created = std::make_unique<ArenaTeam>();
        if (!created->Create(PlayerGuid(entry.guids.front()), entry.payload.at("arena_type").get<uint8>(),
            entry.payload.at("name").get<std::string>(), 0, 0, 0, 0, 0)) return {StepState::Refused, "arena_create_refused"};
        result = created->GetId(); team = created.release(); sArenaTeamMgr->AddArenaTeam(team);
        if (!TeamFields(team, entry.guids) || team->GetCaptain() != PlayerGuid(entry.guids.front())
            || team->GetName() != entry.payload.at("name").get<std::string>())
            return {StepState::Unknown, "arena_create_unproved"};
    }
    else if (action == "add")
    {
        if (team->IsMember(PlayerGuid(target))) return {StepState::Refused, "already_in_team"};
        expected.push_back(target); std::sort(expected.begin(), expected.end());
        bool added = team->AddMember(PlayerGuid(target));
        if (!added && TeamFields(team, before)) return {StepState::Refused, "arena_add_refused"};
        if (!added || !TeamFields(team, expected)) return {StepState::Unknown, "arena_add_unproved"};
    }
    else
    {
        if (!team->IsMember(PlayerGuid(target))) return {StepState::Refused, "not_member"};
        expected.erase(std::remove(expected.begin(), expected.end(), target), expected.end());
        if (team->GetCaptain() == PlayerGuid(target) && !expected.empty())
        {
            team->SetCaptain(PlayerGuid(expected.front()));
            if (team->GetCaptain() != PlayerGuid(expected.front()) || !TeamFields(team, before))
                return {StepState::Unknown, "arena_captain_unproved"};
        }
        team->DelMember(PlayerGuid(target), true);
        auto const* character = sCharacterCache->GetCharacterCacheByGuid(PlayerGuid(target));
        if (!character || character->ArenaTeamId[slot] || FindOnline(target)->GetArenaTeamId(slot)
            || Roster(team) != expected) return {StepState::Unknown, "arena_drop_unproved"};
        if (expected.empty())
        {
            team->Disband();
            if (sArenaTeamMgr->GetArenaTeamById(id)) return {StepState::Unknown, "arena_disband_unproved"};
            delete team;
        }
        else if (!TeamFields(team, expected)) return {StepState::Unknown, "arena_drop_unproved"};
    }
    entry.payload["done"] = true; entry.payload["value"] = result;
    return {StepState::Complete, "", result};
}

bool WatchOwner(PvpOwner const& owner, std::uintptr_t battlefield)
{
    std::lock_guard lock(inbox.mutex);
    if (inbox.owners.size() >= MaxOwners || inbox.owners.count(owner.token)) return false;
    OwnerFacts facts;
    facts.battlefield = battlefield;
    for (auto guid : owner.members)
    {
        auto* player = FindOnline(guid);
        if (!player || inbox.watches.count(guid)) return false;
        facts.members.emplace(guid, MemberFacts{});
    }
    inbox.owners.emplace(owner.token, std::move(facts));
    for (auto guid : owner.members)
    {
        auto* player = FindOnline(guid);
        auto session = reinterpret_cast<std::uintptr_t>(player->GetSession());
        Watch watch{owner.token, session};
        watch.wintergrasp = owner.kind == PvpKind::Wintergrasp;
        for (std::uint8_t slot = 0; slot < MAX_ARENA_SLOT; ++slot)
        {
            watch.teams[slot] = player->GetArenaTeamId(slot);
            if (watch.teams[slot]) ++inbox.teams[watch.teams[slot]];
        }
        inbox.watches.emplace(guid, std::move(watch)); inbox.sessions.emplace(session, guid);
    }
    return true;
}
OwnerFacts Facts(std::string const& token)
{
    std::lock_guard lock(inbox.mutex);
    auto found = inbox.owners.find(token);
    return found == inbox.owners.end() ? OwnerFacts{0, 0, 0, 0, 0, "pvp_receipts_missing"} : found->second;
}
std::optional<NativeInstance> Instance(std::uint32_t id)
{
    std::lock_guard lock(inbox.mutex);
    auto found = inbox.instances.find(id);
    return found == inbox.instances.end() ? std::nullopt : std::optional<NativeInstance>(found->second);
}
void SetEntering(PvpOwner const& owner, std::uint32_t guid, ArenaAttempt const& arena)
{
    std::lock_guard lock(inbox.mutex);
    auto& member = inbox.owners.at(owner.token).members.at(guid);
    member.entering = true; member.arenaQueue = arena.proof.Queue(); member.arenaMatch = arena.proof.Match();
    member.arenaEpoch = owner.originEpoch;
}
bool Declare(PvpOwner const& owner, std::uint32_t guid, std::vector<BodyScope> scopes)
{
    return RealmModule::Instance().DeclareContinuationScopes(owner.token, owner.revision, guid, std::move(scopes));
}
PvpContinuationStep Unknown(std::string why) { return {PvpStage::Unknown, std::move(why)}; }
PvpContinuationStep Waiting(PvpOwner const& owner, std::string why = "")
{
    return {owner.cleanupId.empty() ? owner.stage : PvpStage::Cleaning, std::move(why)};
}
PvpContinuationStep FailedEnrollment(PvpOwner const& owner, Attempt& attempt, std::string reason)
{
    attempt.failure = std::move(reason);
    if (!attempt.enrollmentReported)
    {
        RealmModule::Instance().PublishJob({owner.originId,
            owner.kind == PvpKind::Wintergrasp ? "wg_join" : "arena_queue", false, attempt.failure, 0, 0, owner.token});
        attempt.enrollmentReported = true;
    }
    return {PvpStage::Ending, attempt.failure};
}
std::string Validate(PvpOwner const& owner)
{
    for (auto guid : owner.members)
        if (auto why = RealmModule::Instance().ValidateContinuation(owner.token, guid,
            owner.cleanupId.empty() ? ContinuationMode::Application : ContinuationMode::Cleanup); !why.empty()) return why;
    return "";
}
void Enrollment(PvpOwner const& owner, Attempt& attempt, std::uint64_t value)
{
    if (attempt.enrollmentReported) return;
    RealmJob job{owner.originId, owner.kind == PvpKind::Wintergrasp ? "wg_join" : "arena_queue", true, "", 0, value};
    job.pvpOwner = owner.token;
    RealmModule::Instance().PublishJob(job); attempt.enrollmentReported = true;
}
struct CleanGuard final : CleanupQuiescence
{
    std::string token;
    std::unique_lock<std::mutex> lock{inbox.mutex};
    explicit CleanGuard(std::string value) : token(std::move(value)) { }
    ~CleanGuard() override
    {
        // The module either committed under this guard or retained the owner.
        // Only the former permits retiring native watches and proof storage.
        auto owner = RealmModule::Instance().GetPvpOwner(token);
        if (!owner || owner->stage != PvpStage::Clean) return;
        for (auto guid : owner->members)
        {
            auto found = inbox.watches.find(guid);
            if (found == inbox.watches.end() || found->second.token != token) continue;
            for (auto team : found->second.teams)
                if (team)
                {
                    auto affected = inbox.teams.find(team);
                    if (affected != inbox.teams.end() && --affected->second == 0) inbox.teams.erase(affected);
                }
            inbox.sessions.erase(found->second.session); inbox.watches.erase(found);
        }
        inbox.owners.erase(token); attempts.erase(token);
    }
};
PvpContinuationStep Clean(PvpOwner const& owner, std::uint64_t revision)
{
    auto guard = std::make_shared<CleanGuard>(owner.token);
    auto found = inbox.owners.find(owner.token);
    if (found == inbox.owners.end() || found->second.revision != revision || !found->second.poison.empty())
        return Unknown("pvp_cleanup_receipt_changed");
    PvpCleanProof proof;
    proof.token = owner.token; proof.cleanupId = owner.cleanupId; proof.revision = owner.revision;
    proof.members = owner.members; proof.quiescence = std::move(guard);
    return {PvpStage::Clean, "", std::move(proof)};
}
RealmStep Begin(Queued& entry, PvpKind kind)
{
    auto& module = RealmModule::Instance();
    for (auto guid : entry.guids)
        if (auto why = Eligible(guid, entry.id); !why.empty()) return {StepState::Refused, why};
    Attempt attempt;
    if (kind == PvpKind::Wintergrasp)
    {
        auto* field = Wintergrasp();
        if (!field) return {StepState::Refused, "no_wintergrasp"};
        // The native zone invitation uses this same 15-minute window. An
        // already-running war is a distinct source, never adopted here.
        if (field->IsWarTime() || field->GetTimer() > 900000) return {StepState::Refused, "queue_closed"};
        attempt.battlefield = reinterpret_cast<std::uintptr_t>(field);
        for (auto guid : entry.guids)
        {
            auto* player = FindOnline(guid);
            if (ForeignWgBaseline(player)) return {StepState::Refused, "wg_private_state_unavailable"};
            WgMember member; member.origin = {player->GetMapId(), player->GetInstanceId(), 0};
            member.team = player->GetTeamId(); attempt.wg.emplace(guid, member);
        }
    }
    else
    {
        auto& arena = attempt.arena;
        arena.team = entry.payload.at("team_id"); arena.captain = entry.payload.at("captain_guid");
        auto* team = sArenaTeamMgr->GetArenaTeamById(arena.team);
        if (!team || team->GetCaptain() != PlayerGuid(arena.captain)) return {StepState::Refused, "not_captain"};
        arena.type = team->GetType(); arena.roster = Roster(team);
        if (arena.roster != entry.guids || arena.roster.size() < arena.type || !TeamFields(team, arena.roster))
            return {StepState::Refused, "arena_roster_changed"};
        arena.players = {arena.captain};
        for (auto guid : arena.roster)
            if (guid != arena.captain && arena.players.size() < arena.type) arena.players.push_back(guid);
        std::sort(arena.players.begin(), arena.players.end());
        for (auto guid : entry.guids)
        {
            auto* player = FindOnline(guid);
            arena.origins.emplace(guid, BodyScope{player->GetMapId(), player->GetInstanceId(), 0});
        }
    }
    std::string error;
    auto owner = module.PreparePvpContinuation(entry.id, kind, entry.guids, error);
    if (!owner) return {StepState::Refused, error};
    if (!WatchOwner(*owner, attempt.battlefield)) return {StepState::Unknown, "pvp_watch_capacity"};
    { std::lock_guard lock(inbox.mutex); attempt.overflow = inbox.overflow; attempt.cycle = inbox.wgCycle; }
    attempts.emplace(owner->token, std::move(attempt));
    return {StepState::Detached};
}

ArenaQueueIdentity QueueIdentity(GroupQueueInfo const& row, ArenaAttempt const& arena, std::uint64_t insertion)
{
    return {insertion, arena.group, arena.groupGeneration,
        static_cast<std::uint32_t>(BattlegroundMgr::BGQueueTypeId(BATTLEGROUND_AA, static_cast<uint8>(arena.type))),
        row.ArenaTeamId, row.ArenaType, row.BracketId, row.JoinTime};
}
std::vector<std::uint32_t> QueueMembers(GroupQueueInfo const& row)
{
    std::vector<std::uint32_t> result;
    for (auto guid : row.Players) result.push_back(guid.GetCounter());
    std::sort(result.begin(), result.end()); return result;
}
bool OnlyQueue(Player* player, BattlegroundQueueTypeId expected)
{
    for (std::uint8_t i = 0; i < PLAYER_MAX_BATTLEGROUND_QUEUES; ++i)
    {
        auto queue = player->GetBattlegroundQueueTypeId(i);
        if (queue != BATTLEGROUND_QUEUE_NONE && queue != expected) return false;
    }
    return sLFGMgr->GetState(player->GetGUID()) == lfg::LFG_STATE_NONE
        && (!player->GetGroup() || sLFGMgr->GetState(player->GetGroup()->GetGUID()) == lfg::LFG_STATE_NONE);
}
std::string ReconcileArena(PvpOwner const& owner, ArenaAttempt& arena, OwnerFacts const& facts)
{
    if (!facts.poison.empty()) return facts.poison;
    if (!arena.proof.Bound()) return "";
    auto const& match = arena.proof.Match();
    auto queueType = static_cast<BattlegroundQueueTypeId>(arena.proof.Queue().queue);
    auto& queue = sBattlegroundMgr->GetBattlegroundQueue(queueType);
    for (auto guid : arena.players)
    {
        auto const& fact = facts.members.at(guid);
        auto* player = ObjectAccessor::FindConnectedPlayer(PlayerGuid(guid));
        if (!player) return "pvp_body_unavailable";
        if (fact.add && arena.proof.Stage(guid) == ArenaMemberStage::Entering)
        {
            auto native = Instance(match.instance);
            auto* bg = player->GetBattleground();
            bool exact = fact.add == match.generation && fact.instance == match.instance && fact.map == match.map
                && native && native->generation == match.generation && bg && bg->GetInstanceID() == match.instance
                && reinterpret_cast<std::uintptr_t>(bg) == native->object && bg->GetPlayers().count(PlayerGuid(guid))
                && GroupId(player) == fact.group && player->GetGroup() && player->GetGroup()->isBGGroup()
                && Included(arena.players, GroupMembers(player->GetGroup()))
                && player->GetOriginalGroup() && player->GetOriginalGroup()->GetGUID().GetRawValue() == arena.group;
            if (!arena.proof.Entered(guid, match, exact)) return arena.proof.Reason();
            auto origin = arena.origins.at(guid); origin.group = arena.group;
            if (!Declare(owner, guid, {origin, {match.map, match.instance, arena.group},
                {match.map, match.instance, fact.group}})) return "arena_scope_receipt";
        }
        if (fact.remove && arena.proof.Stage(guid) != ArenaMemberStage::Removed)
        {
            GroupQueueInfo row;
            if (fact.remove != match.generation || !fact.add || player->InBattleground()
                || queue.GetPlayerGroupInfoData(PlayerGuid(guid), &row) || player->InBattlegroundQueue())
                return "arena_remove_lineage";
            if (!arena.proof.Removed(guid, true, true)) return arena.proof.Reason();
            auto exit = arena.exits.count(guid) ? arena.exits.at(guid) : arena.origins.at(guid);
            exit.group = arena.group;
            if (!Declare(owner, guid, {exit, {match.map, match.instance, arena.group},
                {match.map, match.instance, facts.members.at(guid).group}})) return "arena_exit_scope";
        }
    }
    return "";
}
PvpContinuationStep ArenaClean(PvpOwner const& owner, Attempt& attempt, OwnerFacts const& facts)
{
    auto& arena = attempt.arena;
    auto& module = RealmModule::Instance();
    if (arena.proof.Bound())
    {
        auto queueType = static_cast<BattlegroundQueueTypeId>(arena.proof.Queue().queue);
        auto& queue = sBattlegroundMgr->GetBattlegroundQueue(queueType);
        std::vector<std::uint32_t> queued;
        for (auto guid : arena.players)
        {
            auto* player = FindOnline(guid);
            if (!player || player->IsBeingTeleported()) return Waiting(owner, "arena_transition_pending");
            if (!OnlyQueue(player, queueType)) return Unknown("arena_foreign_queue");
            GroupQueueInfo row;
            if (queue.GetPlayerGroupInfoData(PlayerGuid(guid), &row))
            {
                if (!row.IsRated || !arena.proof.ObserveQueue(QueueIdentity(row, arena, facts.insertion), QueueMembers(row)))
                    return Unknown("arena_cleanup_queue_lineage");
                queued.push_back(guid);
            }
            else if (arena.proof.Stage(guid) == ArenaMemberStage::Queued) return Unknown("arena_queue_disappeared");
        }
        if (!queued.empty())
        {
            if (auto why = module.ChargeContinuationBodies(owner.token, arena.players); !why.empty()) return Waiting(owner, why);
            auto* player = FindOnline(queued.front());
            if (player->IsInCombat() || player->GetCharm()) return Waiting(owner, "arena_queue_leave_busy");
            WorldPacket packet(CMSG_BATTLEFIELD_PORT, 9);
            packet << uint8(arena.type) << uint8(0) << uint32(BATTLEGROUND_AA) << uint16(0x1F90) << uint8(0);
            NativeCall call(owner.token, queued.front()); player->GetSession()->HandleBattleFieldPortOpcode(packet);
            for (auto guid : queued)
            {
                GroupQueueInfo after;
                auto* member = FindOnline(guid);
                if (!member || member->InBattlegroundQueue() || queue.GetPlayerGroupInfoData(PlayerGuid(guid), &after)
                    || !arena.proof.Removed(guid, true, true)) return Unknown("arena_queue_leave_unproved");
            }
            return Waiting(owner);
        }
        for (auto guid : arena.players)
        {
            auto* player = FindOnline(guid);
            if (arena.proof.Stage(guid) == ArenaMemberStage::Entering) return Waiting(owner, "arena_entry_pending");
            if (arena.proof.Stage(guid) != ArenaMemberStage::Active) continue;
            auto const& match = arena.proof.Match();
            auto native = Instance(match.instance);
            auto* bg = player->GetBattleground();
            if (!native || native->generation != match.generation || !bg
                || reinterpret_cast<std::uintptr_t>(bg) != native->object) return Unknown("arena_leave_instance");
            for (auto side : {TEAM_ALLIANCE, TEAM_HORDE})
                if (auto* group = bg->GetBgRaid(side); group && group->IsMember(PlayerGuid(guid))
                    && !Included(arena.players, GroupMembers(group))) return Unknown("arena_shared_group");
            auto const& point = player->GetEntryPoint();
            if (point.GetMapId() == MAPID_INVALID || !sMapStore.LookupEntry(point.GetMapId())
                || sMapStore.LookupEntry(point.GetMapId())->Instanceable()) return Unknown("arena_exit_unavailable");
            auto exit = BodyScope{point.GetMapId(), 0, arena.group}; arena.exits[guid] = exit;
            if (!Declare(owner, guid, {exit, {match.map, match.instance, GroupId(player)},
                {match.map, match.instance, arena.group}})) return Unknown("arena_exit_scope");
            if (auto why = module.ChargeContinuationBodies(owner.token, arena.players); !why.empty()) return Waiting(owner, why);
            NativeCall call(owner.token, guid); player->LeaveBattleground();
            return Waiting(owner);
        }
    }
    // A never-enqueued private party is also a continuing mutation and has to
    // be accounted for before cleanup can settle a partially refused attempt.
    if (arena.group)
    {
        auto* group = ExactGroup(arena.group);
        if (group)
        {
            if (GroupMembers(group) != arena.players || group->isBGGroup() || group->isBFGroup())
                return Unknown("arena_private_group_changed");
            for (auto guid : arena.players)
            {
                auto* player = FindOnline(guid);
                if (!player || player->InBattleground() || player->InBattlegroundQueue() || AnyQueue(guid)
                    || player->IsBeingTeleported() || player->GetOriginalGroup() || GroupId(player) != arena.group)
                    return Waiting(owner, "arena_body_return_pending");
                if (sLFGMgr->GetState(player->GetGUID()) != lfg::LFG_STATE_NONE
                    || sLFGMgr->GetState(group->GetGUID()) != lfg::LFG_STATE_NONE) return Unknown("arena_foreign_lfg");
                auto scope = BodyScope{player->GetMapId(), player->GetInstanceId(), 0};
                if (!Declare(owner, guid, {scope, {scope.map, scope.instance, arena.group}})) return Unknown("arena_disband_scope");
            }
            if (auto why = module.ChargeContinuationBodies(owner.token, arena.players); !why.empty()) return Waiting(owner, why);
            NativeCall call(owner.token); group->Disband();
            // Disband can delete this; only resolve its full GUID afterward.
            if (ExactGroup(arena.group)) return Unknown("arena_disband_unproved");
            return Waiting(owner);
        }
        if (facts.disband != arena.groupGeneration) return Unknown("arena_disband_receipt_missing");
    }
    for (auto guid : owner.members)
    {
        auto* player = FindOnline(guid);
        if (!player || player->InBattleground() || player->InBattlegroundQueue() || AnyQueue(guid) || player->IsBeingTeleported()
            || player->GetGroup() || player->GetOriginalGroup() || player->GetGroupInvite() || player->duel
            || sLFGMgr->GetState(player->GetGUID()) != lfg::LFG_STATE_NONE
            || WgSources(Wintergrasp(), guid)) return Unknown("arena_cleanup_sources_remain");
    }
    if (arena.proof.Bound() && !arena.proof.CanClean(true, true, facts.revision, Facts(owner.token).revision))
        return Unknown("arena_cleanup_lineage");
    return Clean(owner, facts.revision);
}
PvpContinuationStep ArenaProgress(ArenaAttempt const& arena, bool invited)
{
    switch (arena.proof.Progress(invited))
    {
        case ArenaProgressStage::Queued: return {PvpStage::Queued};
        case ArenaProgressStage::Invited: return {PvpStage::Invited};
        case ArenaProgressStage::Entering: return {PvpStage::Entering};
        case ArenaProgressStage::Active: return {PvpStage::Active};
        case ArenaProgressStage::Ending: return {PvpStage::Ending};
    }
    return Unknown("arena_progress_unavailable");
}
PvpContinuationStep ArenaContinue(PvpOwner const& owner, Attempt& attempt, OwnerFacts const& facts)
{
    auto& module = RealmModule::Instance();
    auto& arena = attempt.arena;
    if (auto why = ReconcileArena(owner, arena, facts); !why.empty()) return Unknown(why);
    if (auto why = Validate(owner); !why.empty()) return Unknown(why);
    if (!owner.cleanupId.empty()) return ArenaClean(owner, attempt, facts);
    if (!attempt.failure.empty()) return {PvpStage::Ending, attempt.failure};
    auto* team = sArenaTeamMgr->GetArenaTeamById(arena.team);
    // Validate(owner) above has already proved this immutable body's lifetime,
    // session and declared scopes; read-only roster checks also cover transit.
    if (!TeamFields(team, arena.roster, true) || team->GetCaptain() != PlayerGuid(arena.captain))
        return Unknown("arena_roster_changed");
    if (!arena.grouped)
    {
        auto* captain = ObjectAccessor::FindConnectedPlayer(PlayerGuid(arena.captain));
        if (!captain) return Unknown("arena_captain_unavailable");
        if (arena.fallbackTarget)
        {
            auto const& target = *arena.fallbackTarget;
            if (captain->IsBeingTeleported())
            {
                auto const& destination = captain->GetTeleportDest();
                if (destination.GetMapId() != target.map
                    || std::abs(destination.GetPositionX() - target.x) > 0.1f
                    || std::abs(destination.GetPositionY() - target.y) > 0.1f
                    || std::abs(destination.GetPositionZ() - target.z) > 0.1f)
                    return Unknown("arena_battlemaster_teleport_changed");
                return NowMs() - arena.fallbackAt >= 10000 ? Unknown("arena_battlemaster_transit_unproved")
                    : Waiting(owner, "arena_battlemaster_transit");
            }
            if (captain->GetMapId() != target.map || captain->GetInstanceId()
                || captain->GetZoneId() != target.zone || captain->GetDistance(target.x, target.y, target.z) > 3.0f)
                return Unknown("arena_battlemaster_arrival_unproved");
        }
        else if (captain->IsBeingTeleported()) return Unknown("arena_unscoped_teleport");
        if (FindOnline(arena.captain) != captain) return Unknown("arena_captain_unavailable");
        auto master = sRandomPlayerbotMgr.GetBattleMasterGUID(captain, BATTLEGROUND_AA);
        auto* creature = master.IsEmpty() ? nullptr : ObjectAccessor::GetCreature(*captain, master);
        if (!creature || !creature->IsBattleMaster())
        {
            if (arena.fallback) return NowMs() - arena.fallbackAt >= 10000
                ? FailedEnrollment(owner, attempt, "no_battlemaster") : Waiting(owner);
            auto place = std::find_if(module.Places().begin(), module.Places().end(), [](Place const& value)
                { return value.name == "org_arena_master" && value.kind == "arenamaster"; });
            if (place == module.Places().end() || !ValidPlace(*place)) return FailedEnrollment(owner, attempt, "no_battlemaster");
            if (!Declare(owner, arena.captain, {{place->map, 0, 0}})) return Unknown("arena_battlemaster_scope");
            if (auto why = module.ChargeContinuationBodies(owner.token, {arena.captain}); !why.empty()) return Waiting(owner, why);
            arena.fallback = true; arena.fallbackAt = NowMs(); arena.fallbackTarget = *place;
            NativeCall call(owner.token, arena.captain);
            if (!captain->TeleportTo(place->map, place->x, place->y, place->z, place->o))
                return FailedEnrollment(owner, attempt, "no_battlemaster");
            arena.origins[arena.captain] = {place->map, 0, 0};
            return Waiting(owner);
        }
        for (auto guid : owner.members)
        {
            auto* player = FindOnline(guid);
            if (!player || player->GetGroup() || player->GetOriginalGroup() || player->GetGroupInvite()
                || player->InBattlegroundQueue() || player->InBattleground() || player->IsBeingTeleported())
                return Unknown("arena_group_precondition");
        }
        if (auto why = module.ChargeContinuationBodies(owner.token, arena.players); !why.empty()) return Waiting(owner, why);
        NativeCall call(owner.token, arena.captain);
        auto* group = new Group;
        if (!group->Create(captain)) { delete group; return Unknown("arena_group_create_unproved"); }
        sGroupMgr->AddGroup(group); arena.group = group->GetGUID().GetRawValue();
        arena.groupGeneration = call.value.groupGeneration;
        if (!arena.groupGeneration || call.value.group != arena.group) return Unknown("arena_group_create_receipt");
        for (auto guid : arena.players)
        {
            if (guid != arena.captain && !group->AddMember(FindOnline(guid))) return Unknown("arena_group_add_unproved");
            auto scope = arena.origins.at(guid); scope.group = arena.group;
            if (!Declare(owner, guid, {scope})) return Unknown("arena_group_scope");
        }
        if (GroupMembers(group) != arena.players) return Unknown("arena_group_manifest");
        arena.grouped = true;
        return Waiting(owner);
    }
    if (!arena.queued)
    {
        if (arena.queueAttempted) return Unknown("arena_queue_refused");
        auto* group = ExactGroup(arena.group);
        auto* captain = FindOnline(arena.captain);
        if (!group || GroupMembers(group) != arena.players || group->GetLeaderGUID() != PlayerGuid(arena.captain))
            return Unknown("arena_private_group_changed");
        auto master = sRandomPlayerbotMgr.GetBattleMasterGUID(captain, BATTLEGROUND_AA);
        auto* creature = master.IsEmpty() ? nullptr : ObjectAccessor::GetCreature(*captain, master);
        if (!creature || !creature->IsBattleMaster()) return FailedEnrollment(owner, attempt, "no_battlemaster");
        for (auto guid : arena.players)
        {
            auto* player = FindOnline(guid);
            if (!player || player->InBattlegroundQueue() || player->InBattleground() || player->GetOriginalGroup()
                || player->GetGroupInvite() || WgSources(Wintergrasp(), guid)) return Unknown("arena_foreign_source");
        }
        if (auto why = module.ChargeContinuationBodies(owner.token, arena.players); !why.empty()) return Waiting(owner, why);
        arena.queueAttempted = true; arena.rating = team->GetRating();
        WorldPacket packet(CMSG_BATTLEMASTER_JOIN_ARENA, 11);
        packet << master << uint8(team->GetSlot()) << uint8(1) << uint8(1);
        NativeCall call(owner.token, arena.captain); captain->GetSession()->HandleBattlemasterJoinArena(packet);
        auto queueType = BattlegroundMgr::BGQueueTypeId(BATTLEGROUND_AA, static_cast<uint8>(arena.type));
        auto& queue = sBattlegroundMgr->GetBattlegroundQueue(queueType);
        GroupQueueInfo first;
        if (!call.value.insertion)
        {
            for (auto guid : arena.players)
                if (AnyQueue(guid) || FindOnline(guid)->InBattlegroundQueue()) return Unknown("arena_queue_acceptance_unproved");
            return FailedEnrollment(owner, attempt, "arena_queue_refused");
        }
        if (!call.value.insertion || !queue.GetPlayerGroupInfoData(PlayerGuid(arena.captain), &first)
            || !first.IsRated || first.ArenaTeamId != arena.team || QueueMembers(first) != arena.players
            || !arena.proof.Bind(QueueIdentity(first, arena, call.value.insertion), arena.players))
            return Unknown("arena_queue_acceptance_unproved");
        for (auto guid : arena.players)
        {
            GroupQueueInfo row;
            auto* player = FindOnline(guid);
            if (!player || player->GetBattlegroundQueueIndex(queueType) >= PLAYER_MAX_BATTLEGROUND_QUEUES
                || !queue.GetPlayerGroupInfoData(PlayerGuid(guid), &row) || !row.IsRated
                || !arena.proof.ObserveQueue(QueueIdentity(row, arena, call.value.insertion), QueueMembers(row)))
                return Unknown("arena_queue_manifest_unproved");
        }
        arena.queued = true; Enrollment(owner, attempt, arena.players.size());
        Publish(MakeArenaQueue(arena.team, arena.type, true));
        return {PvpStage::Queued};
    }
    auto queueType = static_cast<BattlegroundQueueTypeId>(arena.proof.Queue().queue);
    auto& queue = sBattlegroundMgr->GetBattlegroundQueue(queueType);
    bool invited = false;
    for (auto guid : arena.players)
    {
        auto stage = arena.proof.Stage(guid);
        if (stage == ArenaMemberStage::Removed) return {PvpStage::Ending};
        if (stage == ArenaMemberStage::Active) continue;
        if (stage == ArenaMemberStage::Entering) continue;
        GroupQueueInfo row;
        if (!queue.GetPlayerGroupInfoData(PlayerGuid(guid), &row) || !row.IsRated
            || !arena.proof.ObserveQueue(QueueIdentity(row, arena, facts.insertion), QueueMembers(row)))
            return Unknown("arena_queue_lineage");
        if (!row.IsInvitedToBGInstanceGUID) continue;
        invited = true;
        auto native = Instance(row.IsInvitedToBGInstanceGUID);
        auto* bg = sBattlegroundMgr->GetBattleground(row.IsInvitedToBGInstanceGUID, BATTLEGROUND_AA);
        if (!native || !bg || reinterpret_cast<std::uintptr_t>(bg) != native->object
            || !bg->isRated() || bg->GetArenaType() != arena.type) return Unknown("arena_invite_instance");
        ArenaMatchIdentity match{native->generation, native->instance, native->map, row.RemoveInviteTime};
        if (!arena.proof.Invite(match) || !queue.IsPlayerInvited(PlayerGuid(guid), match.instance, match.inviteExpiry))
            return Unknown("arena_invite_lineage");
        auto* player = FindOnline(guid);
        if (!player || !OnlyQueue(player, queueType) || player->InBattleground() || player->GetOriginalGroup()
            || player->GetGroupInvite() || GroupId(player) != arena.group || WgSources(Wintergrasp(), guid))
            return Unknown("arena_port_foreign_source");
        if (player->IsInCombat() || player->GetCharm() || player->IsBeingTeleported()) return ArenaProgress(arena, true);
        auto* raid = bg->GetBgRaid(row.teamId);
        if (raid && !Included(arena.players, GroupMembers(raid))) return Unknown("arena_shared_match_group");
        auto origin = arena.origins.at(guid); origin.group = arena.group;
        if (!Declare(owner, guid, {origin, {match.map, match.instance, arena.group}})) return Unknown("arena_entry_scope");
        if (auto why = module.ChargeContinuationBodies(owner.token, arena.players); !why.empty()) return Waiting(owner, why);
        if (!arena.proof.BeginEnter(guid, match)) return Unknown(arena.proof.Reason());
        SetEntering(owner, guid, arena);
        WorldPacket packet(CMSG_BATTLEFIELD_PORT, 9);
        packet << uint8(arena.type) << uint8(0) << uint32(BATTLEGROUND_AA) << uint16(0x1F90) << uint8(1);
        NativeCall call(owner.token, guid); player->GetSession()->HandleBattleFieldPortOpcode(packet);
        GroupQueueInfo after;
        if (queue.GetPlayerGroupInfoData(PlayerGuid(guid), &after) || player->GetBattlegroundId() != match.instance)
            return Unknown("arena_port_unproved");
        return ArenaProgress(arena, true);
    }
    return ArenaProgress(arena, invited);
}

PvpContinuationStep WintergraspContinue(PvpOwner const& owner, Attempt& attempt, OwnerFacts const& facts)
{
    auto& module = RealmModule::Instance();
    auto* field = Wintergrasp();
    if (!field || reinterpret_cast<std::uintptr_t>(field) != attempt.battlefield)
        return Unknown("wg_battlefield_identity");
    if (!facts.poison.empty()) return Unknown(facts.poison);
    if (auto why = Validate(owner); !why.empty()) return Unknown(why);
    std::uint64_t cycle;
    { std::lock_guard lock(inbox.mutex); cycle = inbox.wgCycle; }
    bool allQueued = true, allActive = true;
    for (auto guid : owner.members)
    {
        auto& member = attempt.wg.at(guid);
        auto const& fact = facts.members.at(guid);
        auto* player = ObjectAccessor::FindConnectedPlayer(PlayerGuid(guid));
        if (!player) return Unknown("wg_body_unavailable");
        if (fact.wgInvite)
        {
            if (!member.queued || (member.inviteReceipt && member.inviteReceipt != fact.wgInvite))
                return Unknown("wg_invite_lineage");
            member.inviteReceipt = fact.wgInvite; member.everExposed = true;
        }
        // A rejected low-level invitation can write a private kick task before
        // any invite hook. Crossing a war/cycle boundary is exposure even when
        // there was no public invitation receipt to observe.
        if (fact.wgZone || (member.queued && !member.queueRemoved && (field->IsWarTime() || cycle != attempt.cycle)))
            member.everExposed = true;
        if (fact.wgLeaveComplete) member.leaveReceipt = fact.wgLeaveComplete;
        if (member.accepted && !member.joined && owner.cleanupId.empty() && cycle == attempt.cycle)
        {
            if (fact.wgTravel != member.travelReceipt || fact.wgTravelKind != 1)
                return Unknown("wg_entry_teleport_lineage");
            if (player->IsBeingTeleported())
            {
                if (!(Destination(player->GetTeleportDest()) == fact.wgDestination))
                    return Unknown("wg_entry_teleport_changed");
            }
            else
            {
                auto* group = player->GetGroup();
                bool exactGroup = group && group->isBFGroup() && GroupId(player) == member.group
                    && group->IsMember(PlayerGuid(guid)) && Included(owner.members, GroupMembers(group));
                WintergraspEntryEvidence evidence{true, true, fact.wgTravel != 0,
                    player->GetMapId() == fact.wgDestination.map && !player->GetInstanceId() && player->GetZoneId() == WgZone,
                    fact.wgZone > fact.wgTravel,
                    fact.wgArrival > fact.wgZone, field->GetPlayersInWarSet(player->GetTeamId()).count(PlayerGuid(guid)) != 0,
                    exactGroup, true};
                if (!ProvesWintergraspEntry(evidence)) return Unknown("wg_arrival_unproved");
                member.joined = true;
            }
        }
        allQueued = allQueued && member.queued;
        allActive = allActive && member.joined;
    }
    if (!owner.cleanupId.empty())
    {
        for (auto guid : owner.members)
        {
            auto& member = attempt.wg.at(guid);
            auto* player = ObjectAccessor::FindConnectedPlayer(PlayerGuid(guid));
            if (player->IsBeingTeleported()) return Waiting(owner, "wg_departure_pending");
            if (player->InBattleground() || player->InBattlegroundQueue() || AnyQueue(guid) || player->GetOriginalGroup()
                || player->GetGroupInvite() || player->duel || sLFGMgr->GetState(player->GetGUID()) != lfg::LFG_STATE_NONE
                || (player->GetGroup() && sLFGMgr->GetState(player->GetGroup()->GetGUID()) != lfg::LFG_STATE_NONE))
                return Unknown("wg_foreign_source");
            if (player->GetGroup() && (!member.group || GroupId(player) != member.group
                || !player->GetGroup()->isBFGroup() || !Included(owner.members, GroupMembers(player->GetGroup()))))
                return Unknown("wg_shared_group");
            if (!member.queueRemoved)
            {
                if (auto why = module.ChargeContinuationBodies(owner.token, {guid}); !why.empty()) return Waiting(owner, why);
                NativeCall call(owner.token, guid); field->AskToLeaveQueue(player);
                for (auto side : {TEAM_ALLIANCE, TEAM_HORDE})
                    if (field->GetPlayersQueueSet(side).count(PlayerGuid(guid))) return Unknown("wg_queue_leave_unproved");
                member.queueRemoved = true;
                return Waiting(owner);
            }
            if (member.everExposed && !member.leaveReceipt)
            {
                // The installed core keeps private kick and logout-grace
                // state. An outside-zone invitation has no exact public cancel.
                if (player->GetZoneId() != WgZone) return Waiting(owner, "wg_cleanup_receipt_unavailable");
                if (member.leaving) return Waiting(owner, "wg_zone_departure_receipt_pending");
                auto* home = sMapStore.LookupEntry(player->m_homebindMapId);
                if (!home || home->Instanceable() || sMapMgr->GetZoneId(player->GetPhaseMask(), player->m_homebindMapId,
                    player->m_homebindX, player->m_homebindY, player->m_homebindZ) == WgZone)
                    return Waiting(owner, "wg_cleanup_destination_unavailable");
                member.exit = {player->m_homebindMapId, 0, 0};
                if (!Declare(owner, guid, {member.origin, member.exit,
                    {player->GetMapId(), player->GetInstanceId(), member.group},
                    {player->GetMapId(), player->GetInstanceId(), 0}})) return Unknown("wg_exit_scope");
                auto affected = player->GetGroup() ? GroupMembers(player->GetGroup()) : std::vector<std::uint32_t>{guid};
                if (auto why = module.ChargeContinuationBodies(owner.token, affected); !why.empty()) return Waiting(owner, why);
                member.leaving = true;
                NativeCall call(owner.token, guid); call.value.wgTravelKind = 2;
                call.value.wgDestination = {player->m_homebindMapId, player->m_homebindX, player->m_homebindY,
                    player->m_homebindZ, player->GetOrientation()};
                field->PlayerAskToLeave(player);
                auto after = Facts(owner.token);
                auto const& departure = after.members.at(guid);
                if (!departure.wgTravel || departure.wgTravelKind != 2 || !after.poison.empty()
                    || !(departure.wgDestination == call.value.wgDestination)
                    || (player->IsBeingTeleported() && !(Destination(player->GetTeleportDest()) == departure.wgDestination)))
                    return Unknown("wg_departure_teleport_unproved");
                return Waiting(owner);
            }
            WintergraspCleanupEvidence evidence{true, member.queueRemoved, !WgSources(field, guid), member.everExposed,
                member.leaveReceipt != 0, player->GetZoneId() != WgZone, GroupId(player) == 0,
                !player->IsBeingTeleported(), false, facts.revision, facts.revision};
            if (!ProvesWintergraspClean(evidence)) return Waiting(owner, "wg_cleanup_receipt_unavailable");
        }
        return Clean(owner, facts.revision);
    }
    if (!attempt.failure.empty()) return {PvpStage::Ending, attempt.failure};
    if (cycle != attempt.cycle) return {PvpStage::Ending, "wg_cycle_ended"};
    if (!allQueued)
    {
        if (field->IsWarTime()) return FailedEnrollment(owner, attempt, "wg_partial_queue_closed");
        for (auto guid : owner.members)
        {
            auto& member = attempt.wg.at(guid);
            if (member.queued) continue;
            auto* player = FindOnline(guid);
            if (WgSources(field, guid) || player->GetGroup() || player->GetOriginalGroup() || player->GetGroupInvite()
                || player->InBattlegroundQueue() || player->InBattleground() || player->IsBeingTeleported()
                || sLFGMgr->GetState(player->GetGUID()) != lfg::LFG_STATE_NONE)
                return Unknown("wg_foreign_source");
            if (auto why = module.ChargeContinuationBodies(owner.token, {guid}); !why.empty()) return Waiting(owner, why);
            NativeCall call(owner.token, guid); call.value.wgQueue = true; field->InvitePlayerToQueue(player);
            if (!call.value.queueInvite) return FailedEnrollment(owner, attempt, "wg_queue_invitation_refused");
            field->PlayerAcceptInviteToQueue(player);
            if (!field->GetPlayersQueueSet(player->GetTeamId()).count(PlayerGuid(guid)))
                return Unknown("wg_queue_acceptance_unproved");
            member.team = player->GetTeamId(); member.queued = true;
            bool complete = std::all_of(attempt.wg.begin(), attempt.wg.end(), [](auto const& value) { return value.second.queued; });
            if (complete) Enrollment(owner, attempt, owner.members.size());
            return {complete ? PvpStage::Queued : PvpStage::Acquiring};
        }
    }
    if (!field->IsWarTime())
    {
        for (auto guid : owner.members)
            if (!field->GetPlayersQueueSet(static_cast<TeamId>(attempt.wg.at(guid).team)).count(PlayerGuid(guid)))
                return Unknown("wg_queue_disappeared");
        return {PvpStage::Queued};
    }
    if (allActive) return {PvpStage::Active};
    for (auto guid : owner.members)
    {
        auto& member = attempt.wg.at(guid);
        if (member.accepted) continue;
        auto const& fact = facts.members.at(guid);
        auto* player = FindOnline(guid);
        if (!member.inviteReceipt || !fact.wgExpiry) return Waiting(owner, "wg_invitation_pending");
        auto const& invited = field->GetInvitedPlayersMap(player->GetTeamId());
        auto invitation = invited.find(PlayerGuid(guid));
        if (invitation == invited.end() || invitation->second != fact.wgExpiry
            || invitation->second <= GameTime::GetGameTime().count()) return Unknown("wg_invitation_unproved");
        if (player->GetGroup() || player->GetOriginalGroup() || player->GetGroupInvite() || player->InBattleground()
            || player->InBattlegroundQueue() || player->IsBeingTeleported() || player->duel
            || sLFGMgr->GetState(player->GetGUID()) != lfg::LFG_STATE_NONE) return Unknown("wg_entry_foreign_source");
        // JoinWar hooks may change effective team. Fence either possible native
        // group choice before dispatch; never adopt its members afterward.
        std::vector<std::uint32_t> affected{guid};
        std::array<std::uint64_t, 2> candidates{};
        for (auto side : {TEAM_ALLIANCE, TEAM_HORDE})
            if (auto* group = field->GetFreeBfRaid(side))
            {
                auto members = GroupMembers(group);
                if (!Included(owner.members, members)) return Waiting(owner, "wg_shared_raid_unavailable");
                candidates[side] = group->GetGUID().GetRawValue();
                affected.insert(affected.end(), members.begin(), members.end());
            }
        std::sort(affected.begin(), affected.end()); affected.erase(std::unique(affected.begin(), affected.end()), affected.end());
        if (auto why = module.ChargeContinuationBodies(owner.token, affected); !why.empty()) return Waiting(owner, why);
        NativeCall call(owner.token, guid); call.value.wgTravelKind = 1; call.value.wgDefender = field->GetDefenderTeam();
        field->PlayerAcceptInviteToWar(player);
        auto afterFacts = Facts(owner.token);
        auto const& arrival = afterFacts.members.at(guid);
        auto* group = player->GetGroup();
        auto expected = WintergraspWarDestination(player->GetTeamId(), call.value.wgDefender);
        if (!arrival.wgJoin || !arrival.wgTravel || arrival.wgTravelKind != 1 || !afterFacts.poison.empty()
            || !(arrival.wgDestination == expected)
            || (!player->IsBeingTeleported() && !arrival.wgArrival)
            || (player->IsBeingTeleported() && !(Destination(player->GetTeleportDest()) == expected))
            || !field->GetPlayersInWarSet(player->GetTeamId()).count(PlayerGuid(guid))
            || !group || !group->isBFGroup() || !group->IsMember(PlayerGuid(guid))
            || !Included(owner.members, GroupMembers(group))
            || (candidates[player->GetTeamId()] && candidates[player->GetTeamId()] != group->GetGUID().GetRawValue()))
            return Unknown("wg_war_entry_unproved");
        member.group = group->GetGUID().GetRawValue(); member.accepted = true; member.everExposed = true;
        member.travelReceipt = arrival.wgTravel;
        if (!Declare(owner, guid, {member.origin, {member.origin.map, member.origin.instance, member.group},
            {expected.map, 0, member.group}, {expected.map, 0, 0}}))
            return Unknown("wg_group_scope");
        return {PvpStage::Entering};
    }
    return {PvpStage::Entering};
}

PvpContinuationStep Continue(PvpOwner const& owner)
{
    auto found = attempts.find(owner.token);
    if (found == attempts.end()) return Unknown("pvp_attempt_missing");
    {
        std::lock_guard lock(inbox.mutex);
        if (inbox.overflow != found->second.overflow) return Unknown("pvp_receipt_overflow");
    }
    auto facts = Facts(owner.token);
    if (!facts.poison.empty()) return Unknown(facts.poison);
    return owner.kind == PvpKind::Wintergrasp ? WintergraspContinue(owner, found->second, facts)
        : ArenaContinue(owner, found->second, facts);
}

bool Allowed(Player* player)
{
    if (!player) return false;
    std::lock_guard lock(inbox.mutex);
    auto found = inbox.watches.find(player->GetGUID().GetCounter());
    return found == inbox.watches.end() || (capability && capability->token == found->second.token);
}
class PvpPlayerScript final : public PlayerScript
{
public:
    PvpPlayerScript() : PlayerScript("LeeroyRealmPvpPlayer", {PLAYERHOOK_CAN_JOIN_IN_BATTLEGROUND_QUEUE,
        PLAYERHOOK_CAN_JOIN_IN_ARENA_QUEUE, PLAYERHOOK_CAN_BATTLEFIELD_PORT,
        PLAYERHOOK_ON_LOGIN, PLAYERHOOK_ON_LOGOUT, PLAYERHOOK_ON_BEFORE_TELEPORT, PLAYERHOOK_ON_UPDATE_ZONE,
        PLAYERHOOK_CAN_JOIN_LFG, PLAYERHOOK_ON_DUEL_REQUEST, PLAYERHOOK_ON_DUEL_START}) { }
    void Duel(Player* first, Player* second)
    {
        std::lock_guard lock(inbox.mutex);
        for (auto* player : {first, second})
            if (player)
                if (auto watched = inbox.watches.find(player->GetGUID().GetCounter()); watched != inbox.watches.end())
                {
                    auto& owner = inbox.owners.at(watched->second.token);
                    owner.poison = "pvp_foreign_duel"; ++owner.revision;
                }
    }
    void OnPlayerDuelRequest(Player* target, Player* challenger) override { Duel(target, challenger); }
    void OnPlayerDuelStart(Player* first, Player* second) override { Duel(first, second); }
    bool OnPlayerCanJoinLfg(Player* player, uint8, std::set<uint32>&, std::string const&) override
    {
        if (!player) return false;
        auto members = player->GetGroup() ? GroupMembers(player->GetGroup())
            : std::vector<std::uint32_t>{player->GetGUID().GetCounter()};
        std::lock_guard lock(inbox.mutex);
        return std::none_of(members.begin(), members.end(), [](auto guid) { return inbox.watches.count(guid); });
    }
    bool OnPlayerBeforeTeleport(Player* player, uint32 map, float x, float y, float z, float orientation,
        uint32, Unit*) override
    {
        if (!player) return true;
        std::lock_guard lock(inbox.mutex);
        auto watched = inbox.watches.find(player->GetGUID().GetCounter());
        if (watched == inbox.watches.end() || !watched->second.wintergrasp) return true;
        auto& owner = inbox.owners.at(watched->second.token);
        auto& member = owner.members.at(watched->first);
        if (reinterpret_cast<std::uintptr_t>(player->GetSession()) != watched->second.session)
            owner.poison = "wg_body_lifecycle_changed";
        PvpDestination actual{map, x, y, z, orientation};
        bool scoped = capability && capability->token == watched->second.token && capability->guid == watched->first
            && capability->wgTravelKind;
        if (scoped)
        {
            auto expected = capability->wgTravelKind == 1
                ? WintergraspWarDestination(player->GetTeamId(), capability->wgDefender) : capability->wgDestination;
            if (!(actual == expected)) owner.poison = "wg_native_teleport_destination_changed";
            else
            {
                member.wgTravel = SerialLocked(); member.wgDestination = expected;
                member.wgTravelKind = capability->wgTravelKind;
                member.wgArrival = member.wgLeaveComplete = 0;
                member.wgFar = player->GetMapId() != map;
            }
        }
        else owner.poison = "wg_unscoped_teleport";
        ++owner.revision;
        return true;
    }
    void OnPlayerUpdateZone(Player* player, uint32 zone, uint32) override
    {
        if (!player) return;
        std::lock_guard lock(inbox.mutex);
        auto watched = inbox.watches.find(player->GetGUID().GetCounter());
        if (watched == inbox.watches.end() || !watched->second.wintergrasp) return;
        auto& owner = inbox.owners.at(watched->second.token);
        auto& member = owner.members.at(watched->first);
        // Player::UpdateZone calls this only after BattlefieldMgr's leave/enter
        // paths return and the new zone is assigned. A leave prehook alone is
        // never a completion receipt, including RemoveFromWorld/logout paths.
        if (reinterpret_cast<std::uintptr_t>(player->GetSession()) != watched->second.session
            || !player->GetSession() || player->GetSession()->PlayerLogout())
            owner.poison = "wg_body_lifecycle_changed";
        else if (member.wgTravel && !player->IsBeingTeleported() && AtDestination(player, member.wgDestination, member.wgFar))
        {
            if (zone == WgZone && member.wgTravelKind == 1 && member.wgZone > member.wgTravel)
                member.wgArrival = SerialLocked();
            if (zone != WgZone && member.wgTravelKind == 2 && member.wgLeave > member.wgTravel)
                member.wgLeaveComplete = SerialLocked();
        }
        ++owner.revision;
    }
    void Lifecycle(Player* player, bool login)
    {
        if (!player) return;
        auto guid = player->GetGUID().GetCounter();
        auto session = reinterpret_cast<std::uintptr_t>(player->GetSession());
        std::lock_guard lock(inbox.mutex);
        if (!login) inbox.wgHistory.Logout(guid, session);
        auto watched = inbox.watches.find(guid);
        if (watched == inbox.watches.end()) return;
        auto& owner = inbox.owners.at(watched->second.token);
        owner.poison = "pvp_body_lifecycle_changed"; ++owner.revision;
        if (login)
        {
            // Gate replacement sessions too, while retaining the original
            // immutable body identity in the reservation. This is no rebind.
            inbox.sessions.erase(watched->second.session);
            watched->second.session = session; inbox.sessions[session] = guid;
        }
    }
    void OnPlayerLogin(Player* player) override { Lifecycle(player, true); }
    void OnPlayerLogout(Player* player) override { Lifecycle(player, false); }
    bool OnPlayerCanJoinInBattlegroundQueue(Player* player, ObjectGuid, BattlegroundTypeId, uint8,
        GroupJoinBattlegroundResult& error) override
    {
        if (Allowed(player)) return true;
        error = ERR_BATTLEGROUND_JOIN_FAILED; return false;
    }
    bool OnPlayerCanJoinInArenaQueue(Player* player, ObjectGuid, uint8, BattlegroundTypeId, uint8, uint8,
        GroupJoinBattlegroundResult& error) override
    {
        if (Allowed(player)) return true;
        error = ERR_BATTLEGROUND_JOIN_FAILED; return false;
    }
    bool OnPlayerCanBattleFieldPort(Player* player, uint8, BattlegroundTypeId, uint8) override
        { return Allowed(player); }
};
class PvpServerScript final : public ServerScript
{
public:
    PvpServerScript() : ServerScript("LeeroyRealmPvpPackets", {SERVERHOOK_CAN_PACKET_RECEIVE, SERVERHOOK_ON_PACKET_SENT}) { }
    bool CanPacketReceive(WorldSession* session, WorldPacket const& packet) override
    {
        bool teamMutation = false;
        switch (packet.GetOpcode())
        {
            case CMSG_BATTLEFIELD_MGR_QUEUE_INVITE_RESPONSE:
            case CMSG_BATTLEFIELD_MGR_ENTRY_INVITE_RESPONSE:
            case CMSG_BATTLEFIELD_MGR_EXIT_REQUEST:
            case CMSG_BATTLEMASTER_JOIN:
            case CMSG_BATTLEMASTER_JOIN_ARENA:
            case CMSG_BATTLEFIELD_PORT:
            case CMSG_LEAVE_BATTLEFIELD:
            case CMSG_LFG_JOIN:
            case CMSG_SEARCH_LFG_JOIN:
            case CMSG_LFG_SET_ROLES:
            case CMSG_GROUP_INVITE:
            case CMSG_GROUP_ACCEPT:
            case CMSG_GROUP_UNINVITE_GUID:
            case CMSG_GROUP_DISBAND:
                break;
            case CMSG_ARENA_TEAM_INVITE:
            case CMSG_ARENA_TEAM_ACCEPT:
            case CMSG_ARENA_TEAM_LEAVE:
            case CMSG_ARENA_TEAM_REMOVE:
            case CMSG_ARENA_TEAM_DISBAND:
            case CMSG_ARENA_TEAM_LEADER:
                teamMutation = true; break;
            default: return true;
        }
        std::lock_guard lock(inbox.mutex);
        // No Player or world-owned map reads on the session dispatch path.
        if (inbox.sessions.count(reinterpret_cast<std::uintptr_t>(session))) return false;
        // The initiating captain can belong to another arena slot's team.
        // Fence every persistent team containing a held body, using copied IDs.
        return !teamMutation || packet.size() < 4 || !inbox.teams.count(packet.read<uint32>(0));
    }
    void OnPacketSent(WorldSession* session, WorldPacket const& packet) override
    {
        if (packet.GetOpcode() == SMSG_BATTLEFIELD_MGR_QUEUE_REQUEST_RESPONSE && packet.size() >= 9
            && packet.read<uint32>(0) == BATTLEFIELD_BATTLEID_WG && packet.read<uint8>(8))
        {
            std::lock_guard lock(inbox.mutex);
            auto key = reinterpret_cast<std::uintptr_t>(session);
            auto watched = inbox.sessions.find(key);
            bool scoped = watched != inbox.sessions.end() && capability && capability->wgQueue
                && capability->token == inbox.watches.at(watched->second).token && capability->guid == watched->second;
            if (!scoped)
            {
                // The low-level kick path runs before the invite hook and can
                // survive war end. Retain this opaque session until logout can
                // convert it to a GUID taint; public absence is no baseline.
                inbox.wgHistory.TaintSession(key);
                if (watched != inbox.sessions.end())
                {
                    auto& owner = inbox.owners.at(inbox.watches.at(watched->second).token);
                    owner.poison = "wg_unscoped_queue_acceptance"; ++owner.revision;
                }
            }
            return;
        }
        if (packet.GetOpcode() != SMSG_BATTLEFIELD_MGR_QUEUE_INVITE
            && packet.GetOpcode() != SMSG_BATTLEFIELD_MGR_ENTRY_INVITE) return;
        if (packet.size() < 4 || packet.read<uint32>(0) != BATTLEFIELD_BATTLEID_WG) return;
        std::lock_guard lock(inbox.mutex);
        auto found = inbox.sessions.find(reinterpret_cast<std::uintptr_t>(session));
        if (found == inbox.sessions.end()) return;
        auto const& watch = inbox.watches.at(found->second);
        auto& owner = inbox.owners.at(watch.token);
        if (packet.GetOpcode() == SMSG_BATTLEFIELD_MGR_QUEUE_INVITE)
        {
            if (capability && capability->wgQueue && capability->token == watch.token && capability->guid == found->second)
                capability->queueInvite = true;
        }
        else if (packet.size() >= 12)
        {
            auto& member = owner.members.at(found->second);
            if (!member.wgInvite) owner.poison = "wg_invite_without_candidate";
            member.wgExpiry = packet.read<uint32>(8); ++owner.revision;
        }
    }
};
class PvpArenaScript final : public ArenaScript
{
public:
    PvpArenaScript() : ArenaScript("LeeroyRealmPvpTeams", {ARENAHOOK_CAN_ADD_MEMBER}) { }
    bool CanAddMember(ArenaTeam* team, ObjectGuid guid) override
    {
        if (!team) return false;
        auto id = team->GetId();
        std::lock_guard lock(inbox.mutex);
        // Includes an invitation accepted after the owner froze the roster;
        // CMSG_ARENA_TEAM_ACCEPT itself has no team ID to inspect.
        return !inbox.teams.count(id) && !inbox.watches.count(guid.GetCounter());
    }
};
class PvpGroupScript final : public GroupScript
{
public:
    PvpGroupScript() : GroupScript("LeeroyRealmPvpGroups", {GROUPHOOK_ON_CREATE,
        GROUPHOOK_ON_ADD_MEMBER, GROUPHOOK_ON_INVITE_MEMBER, GROUPHOOK_ON_DISBAND,
        GROUPHOOK_CAN_GROUP_JOIN_BATTLEGROUND_QUEUE}) { }
    void OnCreate(Group* group, Player*) override
    {
        if (!group || !capability) return;
        std::lock_guard lock(inbox.mutex);
        auto found = inbox.owners.find(capability->token);
        if (found == inbox.owners.end()) return;
        auto& facts = found->second;
        capability->group = facts.createdGroup = group->GetGUID().GetRawValue();
        capability->groupGeneration = facts.groupGeneration = SerialLocked(); ++facts.revision;
    }
    void OnAddMember(Group* group, ObjectGuid guid) override
    {
        if (!group) return;
        auto members = GroupMembers(group);
        std::lock_guard lock(inbox.mutex);
        std::set<std::string> affected;
        for (auto member : members)
            if (auto found = inbox.watches.find(member); found != inbox.watches.end()) affected.insert(found->second.token);
        for (auto const& token : affected)
        {
            auto& owner = inbox.owners.at(token); ++owner.revision;
            bool expected = capability && capability->token == token;
            if (!expected && group->isBGGroup())
            {
                auto found = owner.members.find(guid.GetCounter());
                expected = found != owner.members.end() && found->second.entering;
            }
            for (auto member : members) if (!owner.members.count(member)) expected = false;
            if (!expected) owner.poison = "pvp_foreign_group_change";
        }
    }
    void OnInviteMember(Group*, ObjectGuid guid) override
    {
        std::lock_guard lock(inbox.mutex);
        auto found = inbox.watches.find(guid.GetCounter());
        if (found != inbox.watches.end())
        {
            auto& owner = inbox.owners.at(found->second.token);
            owner.poison = "pvp_group_invitation"; ++owner.revision;
        }
    }
    void OnDisband(Group* group) override
    {
        if (!group) return;
        auto raw = group->GetGUID().GetRawValue();
        auto members = GroupMembers(group);
        std::lock_guard lock(inbox.mutex);
        for (auto guid : members)
            if (auto found = inbox.watches.find(guid); found != inbox.watches.end())
            {
                auto& owner = inbox.owners.at(found->second.token);
                if (owner.createdGroup == raw) owner.disband = owner.groupGeneration;
                ++owner.revision;
            }
    }
    bool CanGroupJoinBattlegroundQueue(Group const*, Player* member, Battleground const*, uint32, bool, uint32) override
        { return Allowed(member); }
};
class PvpBattlegroundScript final : public AllBattlegroundScript
{
public:
    PvpBattlegroundScript() : AllBattlegroundScript("LeeroyRealmPvpBattlegrounds", {
        ALLBATTLEGROUNDHOOK_ON_ADD_GROUP, ALLBATTLEGROUNDHOOK_ON_BATTLEGROUND_CREATE,
        ALLBATTLEGROUNDHOOK_ON_BATTLEGROUND_DESTROY, ALLBATTLEGROUNDHOOK_ON_BATTLEGROUND_ADD_PLAYER,
        ALLBATTLEGROUNDHOOK_ON_BATTLEGROUND_REMOVE_PLAYER_AT_LEAVE, ALLBATTLEGROUNDHOOK_ON_BATTLEGROUND_END,
        ALLBATTLEGROUNDHOOK_ON_BEFORE_SEND_JOIN_MESSAGE_ARENA_QUEUE}) { }
    bool OnBeforeSendJoinMessageArenaQueue(BattlegroundQueue*, Player*, GroupQueueInfo*,
        PvPDifficultyEntry const*, bool) override { return true; }
    void OnAddGroup(BattlegroundQueue*, GroupQueueInfo*, uint32&, Player* leader, Group* group,
        BattlegroundTypeId, PvPDifficultyEntry const*, uint8, bool, bool, uint32, uint32, uint32, uint32) override
    {
        if (!leader) return;
        auto members = group ? GroupMembers(group) : std::vector<std::uint32_t>{leader->GetGUID().GetCounter()};
        std::lock_guard lock(inbox.mutex);
        auto serial = SerialLocked();
        std::set<std::string> affected;
        for (auto guid : members)
            if (auto found = inbox.watches.find(guid); found != inbox.watches.end()) affected.insert(found->second.token);
        for (auto const& token : affected)
        {
            auto& owner = inbox.owners.at(token); ++owner.revision;
            if (!capability || capability->token != token || owner.insertion)
                owner.poison = "arena_unscoped_reinsertion";
            else { owner.insertion = serial; capability->insertion = serial; }
        }
    }
    void OnBattlegroundCreate(Battleground* bg) override
    {
        if (!bg || !bg->GetInstanceID() || !bg->isArena()) return;
        std::lock_guard lock(inbox.mutex);
        if (inbox.instances.size() >= MaxInstances || inbox.instances.count(bg->GetInstanceID()))
            { ++inbox.overflow; return; }
        inbox.instances.emplace(bg->GetInstanceID(), NativeInstance{SerialLocked(),
            reinterpret_cast<std::uintptr_t>(bg), bg->GetInstanceID(), bg->GetMapId(), bg->GetArenaType()});
    }
    void OnBattlegroundDestroy(Battleground* bg) override
    {
        if (!bg || !bg->GetInstanceID() || !bg->isArena()) return;
        std::lock_guard lock(inbox.mutex);
        auto found = inbox.instances.find(bg->GetInstanceID());
        if (found != inbox.instances.end() && found->second.object == reinterpret_cast<std::uintptr_t>(bg))
            inbox.instances.erase(found);
        else ++inbox.overflow;
    }
    void Member(Battleground* bg, Player* player, bool adding)
    {
        if (!bg || !player) return;
        auto guid = player->GetGUID().GetCounter();
        auto group = GroupId(player);
        std::lock_guard lock(inbox.mutex);
        auto watched = inbox.watches.find(guid);
        if (watched == inbox.watches.end()) return;
        auto& owner = inbox.owners.at(watched->second.token); ++owner.revision;
        auto instance = inbox.instances.find(bg->GetInstanceID());
        if (instance == inbox.instances.end() || instance->second.object != reinterpret_cast<std::uintptr_t>(bg))
            { owner.poison = "arena_instance_receipt_missing"; return; }
        auto& fact = owner.members.at(guid);
        if (adding)
        {
            if (!fact.entering || fact.add) owner.poison = "arena_unscoped_entry";
            fact.add = instance->second.generation; fact.group = group;
            fact.map = bg->GetMapId(); fact.instance = bg->GetInstanceID();
            // Freeze an exact native-owner event claim on the instance itself.
            // Cleanup may retire every owner/watch before the map ends or the
            // ten-second publisher runs; that cannot erase result provenance.
            if (owner.poison.empty() && !watched->second.wintergrasp
                && watched->second.session == reinterpret_cast<std::uintptr_t>(player->GetSession())
                && fact.arenaQueue.insertion && fact.arenaQueue.insertion == owner.insertion
                && fact.arenaMatch.generation == instance->second.generation
                && fact.arenaMatch.instance == bg->GetInstanceID() && fact.arenaMatch.map == bg->GetMapId()
                && fact.arenaQueue.type == bg->GetArenaType()
                && player->GetBgTeamId() < TEAM_NEUTRAL
                && fact.arenaQueue.team == bg->GetArenaTeamIdForTeam(player->GetBgTeamId()))
            {
                instance->second.nativeOwned = true; instance->second.nativeOwnerEpoch = fact.arenaEpoch;
            }
        }
        else fact.remove = instance->second.generation;
    }
    void OnBattlegroundAddPlayer(Battleground* bg, Player* player) override { Member(bg, player, true); }
    void OnBattlegroundRemovePlayerAtLeave(Battleground* bg, Player* player) override { Member(bg, player, false); }
    void OnBattlegroundEnd(Battleground* bg, TeamId winner) override
    {
        if (!bg || !bg->isArena() || !bg->isRated()) return;
        EndFact fact; fact.epoch = RealmModule::Instance().Epoch(); fact.instance = bg->GetInstanceID();
        fact.type = bg->GetArenaType(); fact.winner = winner;
        for (auto side : {TEAM_ALLIANCE, TEAM_HORDE}) fact.teams[side] = bg->GetArenaTeamIdForTeam(side);
        for (auto const& [guid, player] : bg->GetPlayers())
        {
            (void)player;
            if (fact.members.size() == 10) return;
            fact.members.push_back(guid.GetCounter());
        }
        std::sort(fact.members.begin(), fact.members.end());
        std::lock_guard lock(inbox.mutex);
        auto instance = inbox.instances.find(fact.instance);
        if (instance == inbox.instances.end() || instance->second.object != reinterpret_cast<std::uintptr_t>(bg))
            { ++inbox.overflow; return; }
        fact.generation = instance->second.generation;
        if (instance->second.endCopied) return;
        instance->second.endCopied = true;
        fact.nativeOwned = instance->second.nativeOwned && instance->second.nativeOwnerEpoch == fact.epoch;
        if (inbox.ends.size() == MaxEnds) { ++inbox.overflow; return; }
        inbox.ends.push_back(std::move(fact));
    }
};
class PvpBattlefieldScript final : public BattlefieldScript
{
public:
    PvpBattlefieldScript() : BattlefieldScript("LeeroyRealmPvpWintergrasp", {
        BATTLEFIELDHOOK_BEFORE_INVITE_PLAYER_TO_WAR, BATTLEFIELDHOOK_ON_PLAYER_JOIN_WAR,
        BATTLEFIELDHOOK_ON_PLAYER_LEAVE_ZONE, BATTLEFIELDHOOK_ON_PLAYER_ENTER_ZONE,
        BATTLEFIELDHOOK_ON_WAR_END}) { }
    void Fact(Battlefield* field, Player* player, int kind)
    {
        if (!field || field->GetBattleId() != BATTLEFIELD_BATTLEID_WG || !player) return;
        std::lock_guard lock(inbox.mutex);
        if (kind == 2 && player->GetSession() && player->GetSession()->PlayerLogout())
            inbox.wgHistory.TaintBody(player->GetGUID().GetCounter());
        auto found = inbox.watches.find(player->GetGUID().GetCounter());
        if (found == inbox.watches.end())
        {
            // Scripts are registered before battlefield gameplay at boot.
            // Observe foreign exposure even before a body first asks Realm to
            // own it; private grace/kick state cannot be reconstructed later.
            if (kind != 2 || (player->GetSession() && player->GetSession()->PlayerLogout()))
                inbox.wgHistory.TaintBody(player->GetGUID().GetCounter());
            return;
        }
        auto& owner = inbox.owners.at(found->second.token); ++owner.revision;
        if (!found->second.wintergrasp || owner.battlefield != reinterpret_cast<std::uintptr_t>(field))
            owner.poison = "pvp_foreign_wg_source";
        if (reinterpret_cast<std::uintptr_t>(player->GetSession()) != found->second.session)
            owner.poison = "wg_body_lifecycle_changed";
        auto& member = owner.members.at(found->first);
        auto serial = SerialLocked();
        if (kind == 0) member.wgInvite = serial;
        if (kind == 1)
        {
            member.wgJoin = serial;
            if (!capability || capability->wgTravelKind != 1 || capability->guid != found->first
                || capability->token != found->second.token) owner.poison = "wg_unscoped_join";
        }
        if (kind == 2)
        {
            member.wgLeave = serial;
            if (!player->GetSession() || player->GetSession()->PlayerLogout()
                || reinterpret_cast<std::uintptr_t>(player->GetSession()) != found->second.session)
                owner.poison = "wg_body_lifecycle_changed";
        }
        if (kind == 3)
        {
            member.wgZone = serial;
            if (member.wgTravelKind != 1 || !member.wgTravel
                || !AtDestination(player, member.wgDestination, member.wgFar)) owner.poison = "wg_unscoped_zone_entry";
        }
    }
    void OnBattlefieldBeforeInvitePlayerToWar(Battlefield* field, Player* player) override { Fact(field, player, 0); }
    void OnBattlefieldPlayerJoinWar(Battlefield* field, Player* player) override { Fact(field, player, 1); }
    void OnBattlefieldPlayerLeaveZone(Battlefield* field, Player* player) override { Fact(field, player, 2); }
    void OnBattlefieldPlayerEnterZone(Battlefield* field, Player* player) override { Fact(field, player, 3); }
    void OnBattlefieldWarEnd(Battlefield* field, bool) override
    {
        if (!field || field->GetBattleId() != BATTLEFIELD_BATTLEID_WG) return;
        std::lock_guard lock(inbox.mutex); ++inbox.wgCycle; ++inbox.wgEnds;
        inbox.wgDefender = field->GetDefenderTeam();
        inbox.wgEndEpoch = RealmModule::Instance().Epoch();
    }
};

void Poll()
{
    auto& module = RealmModule::Instance();
    static std::uint64_t observedEpoch = 0, observedEnds = 0;
    static bool soon = false, wasWar = false;
    auto epoch = module.Epoch();
    std::uint64_t ends, endEpoch;
    std::uint32_t defender;
    std::deque<EndFact> results;
    {
        std::lock_guard lock(inbox.mutex);
        ends = inbox.wgEnds; endEpoch = inbox.wgEndEpoch; defender = inbox.wgDefender;
        results.swap(inbox.ends);
    }
    bool reset = epoch != observedEpoch;
    if (reset) { observedEpoch = epoch; soon = false; wasWar = false; }
    if (module.On())
    {
        if (auto* field = Wintergrasp())
        {
            bool war = field->IsWarTime();
            auto side = field->GetDefenderTeam() == TEAM_ALLIANCE ? "alliance" : "horde";
            if (ends != observedEnds && endEpoch == epoch)
            {
                Publish(MakeWg("end", defender == TEAM_ALLIANCE ? "alliance" : "horde", 0,
                    sWorldUpdateTime.GetAverageUpdateTime())); soon = false;
            }
            if (!reset && war && !wasWar)
                Publish(MakeWg("start", side, field->GetTimer() / 1000, sWorldUpdateTime.GetAverageUpdateTime()));
            if (!war && !soon && field->GetTimer() <= 900000)
            {
                soon = true;
                Publish(MakeWg("soon", side, field->GetTimer() / 1000, sWorldUpdateTime.GetAverageUpdateTime()));
            }
            wasWar = war;
        }
        for (auto const& result : results)
        {
            if (result.epoch != epoch || result.winner >= TEAM_NEUTRAL) continue; // Draw has no winner/loser wire form.
            bool watched = false;
            for (auto guid : result.members) watched = watched || module.Watched(guid);
            if (!watched) continue;
            std::array<std::int32_t, 2> deltas{};
            bool owned = result.nativeOwned;
            if (!owned) continue; // Immutable instance claim survives exact native Clean.
            for (auto& [token, attempt] : attempts)
            {
                (void)token;
                auto& arena = attempt.arena;
                if (!arena.proof.Bound() || arena.result || arena.proof.Match().instance != result.instance
                    || arena.proof.Match().generation != result.generation) continue;
                arena.result = true;
                for (auto side : {TEAM_ALLIANCE, TEAM_HORDE})
                    if (arena.team == result.teams[side])
                        if (auto* team = sArenaTeamMgr->GetArenaTeamById(arena.team))
                        {
                            auto difference = std::int64_t(team->GetRating()) - arena.rating;
                            deltas[side] = static_cast<std::int32_t>(std::clamp(difference,
                                std::int64_t(std::numeric_limits<std::int32_t>::min()),
                                std::int64_t(std::numeric_limits<std::int32_t>::max())));
                        }
            }
            if (!owned) continue; // Passive watched matches have their own bounded observer.
            auto winner = result.winner;
            std::uint32_t loser = winner == TEAM_ALLIANCE ? TEAM_HORDE : TEAM_ALLIANCE;
            if (result.teams[winner] && result.teams[loser])
                Publish(MakeArenaEnd(result.type, result.teams[winner], result.teams[loser],
                    deltas[winner], deltas[loser], result.members));
        }
    }
    observedEnds = ends;
}
}

bool RealmPvpArenaObserverOwns(std::uint32_t guid, std::uintptr_t session)
{
    std::lock_guard lock(inbox.mutex);
    auto found = inbox.watches.find(guid);
    return found != inbox.watches.end() && !found->second.wintergrasp && found->second.session == session;
}

void RegisterRealmPvpHandlers()
{
    auto& module = RealmModule::Instance();
    auto admission = [](Queued const& entry, std::uint32_t guid) { return Eligible(guid, entry.id); };
    module.RegisterExecutor("arena_team", TeamEdit, admission);
    module.RegisterExecutor("wg_join", [](Queued& entry, std::uint32_t) { return Begin(entry, PvpKind::Wintergrasp); }, admission);
    module.RegisterExecutor("arena_queue", [](Queued& entry, std::uint32_t) { return Begin(entry, PvpKind::RatedArena); }, admission);
    module.RegisterPvpContinuation(PvpKind::Wintergrasp, Continue);
    module.RegisterPvpContinuation(PvpKind::RatedArena, Continue);
    module.RegisterPoll("realm_pvp", 10000, Poll);
    RegisterCommand("wg_join", [](Json const& message) -> LeeroyCommandResult
    {
        std::string error;
        auto command = ParseWgJoin(message, error);
        if (!command) return {false, error};
        if (!RealmModule::Instance().On()) return {false, "realm_off"};
        if (!Wintergrasp()) return {false, "no_wintergrasp"};
        Queued entry{message.at("id").get<std::string>(), "wg_join", {}, 0, Json::object()};
        for (auto guid : command->guids) if (FindOnline(guid)) entry.guids.push_back(guid);
        std::sort(entry.guids.begin(), entry.guids.end());
        if (entry.guids.empty())
            return {false, "no_online_members"};
        return RealmModule::Instance().Accept(std::move(entry), error) ? LeeroyCommandResult{true, ""}
            : LeeroyCommandResult{false, error};
    });
    RegisterCommand("arena_team", [](Json const& message) -> LeeroyCommandResult
    {
        std::string error;
        auto command = ParseArenaTeam(message, error);
        if (!command) return {false, error};
        Queued entry{message.at("id").get<std::string>(), "arena_team", {}, 0, Json::object()};
        entry.payload = {{"action", command->action}, {"team_id", command->teamId}, {"guid", command->guid}};
        if (command->action == "create")
        {
            entry.guids = {command->captainGuid};
            entry.payload["arena_type"] = command->arenaType; entry.payload["name"] = command->name;
        }
        else
        {
            auto* team = sArenaTeamMgr->GetArenaTeamById(command->teamId);
            if (!team) return {false, "no_team"};
            entry.guids = Roster(team);
            if (command->action == "add") entry.guids.push_back(command->guid);
            std::sort(entry.guids.begin(), entry.guids.end());
            entry.guids.erase(std::unique(entry.guids.begin(), entry.guids.end()), entry.guids.end());
        }
        return RealmModule::Instance().Accept(std::move(entry), error) ? LeeroyCommandResult{true, ""}
            : LeeroyCommandResult{false, error};
    });
    RegisterCommand("arena_queue", [](Json const& message) -> LeeroyCommandResult
    {
        std::string error;
        auto command = ParseArenaQueue(message, error);
        if (!command) return {false, error};
        auto* team = sArenaTeamMgr->GetArenaTeamById(command->teamId);
        if (!team) return {false, "no_team"};
        if (team->GetCaptain() != PlayerGuid(command->captainGuid)) return {false, "not_captain"};
        auto members = Roster(team);
        if (members.size() < team->GetType()) return {false, "too_few"};
        Queued entry{message.at("id").get<std::string>(), "arena_queue", std::move(members), 0,
            {{"team_id", command->teamId}, {"captain_guid", command->captainGuid}}};
        return RealmModule::Instance().Accept(std::move(entry), error) ? LeeroyCommandResult{true, ""}
            : LeeroyCommandResult{false, error};
    });
}
void AddRealmPvpScripts()
{
    new PvpPlayerScript(); new PvpServerScript(); new PvpGroupScript();
    new PvpBattlegroundScript(); new PvpBattlefieldScript(); new PvpArenaScript();
}
}
