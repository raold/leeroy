// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "RealmArenaObserver.h"
#include "RealmPorts.h"
#include "RealmOwnership.h"
#include "RealmPvp.h"
#include "LeeroyRealmArenaObservation.h"
#include "AllBattlegroundScript.h"
#include "ArenaTeam.h"
#include "ArenaTeamMgr.h"
#include "Battleground.h"
#include "BattlegroundMgr.h"
#include "BattlegroundQueue.h"
#include "GroupScript.h"
#include "PlayerScript.h"
#include "WorldScript.h"
#include "WorldSession.h"
#include <deque>
#include <limits>
#include <mutex>

namespace Leeroy::Realm
{
namespace
{
struct ObservationInbox
{
    std::mutex mutex;
    ArenaObservationBook book;
    std::deque<ObservedArenaEnd> ends;
    std::uint64_t epoch = 0, serial = 0, cursor = 0;
} observations;

// Called under the inbox mutex, including on disabled world ticks. Epochs
// fence facts copied concurrently with configuration/lifecycle changes.
bool Enabled()
{
    auto& module = RealmModule::Instance();
    auto epoch = module.Epoch();
    bool enabled = module.On();
    if (!enabled || observations.epoch != epoch)
    {
        observations.book.Fence(enabled, epoch);
        observations.ends.clear(); observations.cursor = 0; observations.epoch = epoch;
    }
    return enabled;
}
std::uint64_t Serial()
{
    if (observations.serial == std::numeric_limits<std::uint64_t>::max())
    {
        observations.book.Clear(); observations.ends.clear(); return 0;
    }
    return ++observations.serial;
}
ObservedArenaLife Life(Player const* player)
{
    return player ? ObservedArenaLife{player->GetGUID().GetCounter(),
        reinterpret_cast<std::uintptr_t>(player), reinterpret_cast<std::uintptr_t>(player->GetSession())}
        : ObservedArenaLife{};
}
std::uint64_t GroupId(Group const* group) { return group ? group->GetGUID().GetRawValue() : 0; }
std::vector<std::uint32_t> GroupRoster(Group const* group)
{
    std::vector<std::uint32_t> result;
    if (group && group->GetMembersCount() <= 5)
        for (auto const& member : group->GetMemberSlots()) result.push_back(member.guid.GetCounter());
    std::sort(result.begin(), result.end()); return result;
}
std::vector<std::uint32_t> TeamRoster(ArenaTeam* team)
{
    std::vector<std::uint32_t> result;
    if (team && team->GetMembers().size() <= 10)
        for (auto const& member : team->GetMembers()) result.push_back(member.Guid.GetCounter());
    std::sort(result.begin(), result.end()); return result;
}
std::vector<std::uint32_t> Members(ObservedArenaQueue const& source)
{
    std::vector<std::uint32_t> result;
    for (auto const& life : source.members) result.push_back(life.guid);
    std::sort(result.begin(), result.end()); return result;
}
bool SameLife(ObservedArenaLife const& life)
{
    auto* player = ObjectAccessor::FindConnectedPlayer(PlayerGuid(life.guid));
    return player && player->GetSession() && !player->GetSession()->PlayerLogout()
        && Life(player) == life && !RealmOwns(life.guid);
}
ObservedArenaDestination Destination(WorldLocation const& location)
{
    return {location.GetMapId(), location.GetPositionX(), location.GetPositionY(), location.GetPositionZ(), location.GetOrientation()};
}
bool ExactTeam(ArenaObservation const& record, ArenaTeam* team)
{
    return team && reinterpret_cast<std::uintptr_t>(team) == record.teamObject
        && team->GetType() == record.source.type && team->GetCaptain().GetCounter() == record.teamCaptain
        && TeamRoster(team) == record.teamRoster;
}
ObservedArenaInstance const* Instance(Battleground* bg)
{
    auto found = observations.book.instances.find(bg->GetInstanceID());
    return found != observations.book.instances.end()
        && found->second.object == reinterpret_cast<std::uintptr_t>(bg) ? &found->second : nullptr;
}

class ArenaObservationBattlegrounds final : public AllBattlegroundScript
{
public:
    ArenaObservationBattlegrounds() : AllBattlegroundScript("LeeroyRealmArenaObservationBattlegrounds", {
        ALLBATTLEGROUNDHOOK_ON_ADD_GROUP, ALLBATTLEGROUNDHOOK_ON_BATTLEGROUND_CREATE,
        ALLBATTLEGROUNDHOOK_ON_BATTLEGROUND_DESTROY, ALLBATTLEGROUNDHOOK_ON_BATTLEGROUND_ADD_PLAYER,
        ALLBATTLEGROUNDHOOK_ON_BATTLEGROUND_REMOVE_PLAYER_AT_LEAVE, ALLBATTLEGROUNDHOOK_ON_BATTLEGROUND_END}) { }
    void OnAddGroup(BattlegroundQueue* queue, GroupQueueInfo* row, uint32&, Player* leader, Group* group,
        BattlegroundTypeId bgType, PvPDifficultyEntry const* bracket, uint8 type, bool rated, bool,
        uint32, uint32, uint32 team, uint32) override
    {
        if (!leader) return;
        auto guids = group ? GroupRoster(group) : std::vector<std::uint32_t>{leader->GetGUID().GetCounter()};
        std::lock_guard lock(observations.mutex);
        if (!Enabled()) return;
        // This callback is unconditional and precedes Players insertion. Never
        // treat its invocation as enrollment; Poll verifies the later row.
        observations.book.Reinsert(guids);
        if (!rated || !group || !queue || !row || !bracket || guids.size() != type || !team) return;
        ObservedArenaQueue source;
        source.insertion = Serial(); source.epoch = observations.epoch;
        source.queue = reinterpret_cast<std::uintptr_t>(queue); source.row = reinterpret_cast<std::uintptr_t>(row);
        source.group = GroupId(group); source.groupObject = reinterpret_cast<std::uintptr_t>(group);
        source.captain = leader->GetGUID().GetCounter(); source.team = team; source.type = type;
        source.bgType = bgType; source.bracket = bracket->GetBracketId(); source.joined = row->JoinTime;
        for (auto guid : guids)
        {
            auto* member = FindOnline(guid);
            if (!member) return;
            source.members.push_back(Life(member));
        }
        observations.book.Add(std::move(source));
    }
    void OnBattlegroundCreate(Battleground* bg) override
    {
        if (!bg || !bg->isArena() || !bg->GetInstanceID()) return;
        std::lock_guard lock(observations.mutex);
        if (!Enabled()) return;
        observations.book.Create({Serial(), observations.epoch, reinterpret_cast<std::uintptr_t>(bg),
            bg->GetInstanceID(), bg->GetMapId(), bg->GetArenaType(), false});
    }
    void OnBattlegroundDestroy(Battleground* bg) override
    {
        if (!bg || !bg->isArena()) return;
        std::lock_guard lock(observations.mutex);
        if (!Enabled()) return;
        if (auto* instance = Instance(bg))
        {
            auto copied = *instance; observations.book.Destroy(copied);
        }
    }
    void OnBattlegroundAddPlayer(Battleground* bg, Player* player) override
    {
        if (!bg || !player || !bg->isArena() || !bg->isRated()) return;
        ObservedArenaEntry entry{Life(player), GroupId(player->GetGroup()), player->GetBgTeamId()};
        bool ownerSourced = RealmPvpArenaObserverOwns(entry.life.guid, entry.life.session);
        std::lock_guard lock(observations.mutex);
        if (!Enabled()) return;
        if (auto* match = Instance(bg))
        {
            // Retain provenance on this exact create generation, including
            // after an owned entrant leaves and its cleanup retires the watch.
            if (ownerSourced) observations.book.MarkOwnerSource(*match);
            observations.book.Enter(*match, entry);
        }
    }
    void OnBattlegroundRemovePlayerAtLeave(Battleground*, Player* player) override
    {
        if (!player) return;
        std::lock_guard lock(observations.mutex);
        Enabled(); observations.book.Leave(player->GetGUID().GetCounter());
    }
    void OnBattlegroundEnd(Battleground* bg, TeamId winner) override
    {
        if (!bg || !bg->isArena() || !bg->isRated()) return;
        // Map-thread values only. The global ArenaTeam manager/ratings are
        // intentionally read only by the world-thread reconciliation below.
        ObservedArenaEnd end;
        end.winner = winner;
        for (auto side : {TEAM_ALLIANCE, TEAM_HORDE}) end.teams[side] = bg->GetArenaTeamIdForTeam(side);
        for (auto const& [guid, player] : bg->GetPlayers())
        {
            (void)guid;
            if (!player || end.members.size() >= 10) return;
            // Freeze owner-source exclusion at the map-thread result. A later
            // world pass may occur after native cleanup released its holder.
            // Query the native immutable watch snapshot before our inbox lock.
            end.ownerSourced = end.ownerSourced || RealmPvpArenaObserverOwns(player->GetGUID().GetCounter(),
                reinterpret_cast<std::uintptr_t>(player->GetSession()));
            end.members.push_back({Life(player), GroupId(player->GetGroup()), player->GetBgTeamId()});
        }
        std::lock_guard lock(observations.mutex);
        if (!Enabled()) return;
        auto* match = Instance(bg);
        if (!match) return;
        end.match = *match;
        end.ownerSourced = end.ownerSourced || match->ownerSourced;
        if (!observations.book.End(end)) return;
        for (auto& [id, record] : observations.book.records)
        {
            (void)id;
            if ((record.source.team == end.teams[0] || record.source.team == end.teams[1])
                && (!record.match || record.match->generation != end.match.generation)) record.baselineAvailable = false;
        }
        if (observations.ends.size() >= ArenaObservationBook::FactCapacity)
        {
            // Exhaustion loses observation coverage only; no body owner or
            // native gameplay state is modified or optimistically completed.
            observations.book.Clear(); observations.ends.clear(); return;
        }
        observations.ends.push_back(std::move(end));
    }
};
class ArenaObservationLifecycle final : public PlayerScript
{
public:
    ArenaObservationLifecycle() : PlayerScript("LeeroyRealmArenaObservationLifecycle",
        {PLAYERHOOK_ON_LOGIN, PLAYERHOOK_ON_LOGOUT, PLAYERHOOK_ON_BEFORE_TELEPORT}) { }
    void Changed(Player* player)
    {
        if (!player) return;
        std::lock_guard lock(observations.mutex);
        Enabled(); observations.book.Lifecycle(player->GetGUID().GetCounter());
    }
    void OnPlayerLogin(Player* player) override { Changed(player); }
    void OnPlayerLogout(Player* player) override { Changed(player); }
    bool OnPlayerBeforeTeleport(Player* player, uint32 map, float x, float y, float z, float orientation,
        uint32, Unit*) override
    {
        if (!player) return true;
        auto life = Life(player);
        auto instance = player->GetBattlegroundId(); auto side = player->GetBgTeamId();
        std::lock_guard lock(observations.mutex);
        if (Enabled()) observations.book.Travel(life, instance, side, {map, x, y, z, orientation});
        return true;
    }
};
class ArenaObservationGroups final : public GroupScript
{
public:
    ArenaObservationGroups() : GroupScript("LeeroyRealmArenaObservationGroups", {GROUPHOOK_ON_ADD_MEMBER,
        GROUPHOOK_ON_REMOVE_MEMBER, GROUPHOOK_ON_CHANGE_LEADER, GROUPHOOK_ON_DISBAND, GROUPHOOK_ON_CREATE}) { }
    void Changed(Group* group)
    {
        if (!group) return;
        std::lock_guard lock(observations.mutex);
        Enabled(); observations.book.GroupChanged(GroupId(group));
    }
    void OnCreate(Group* group, Player*) override { Changed(group); }
    void OnAddMember(Group* group, ObjectGuid) override { Changed(group); }
    void OnRemoveMember(Group* group, ObjectGuid, RemoveMethod, ObjectGuid, char const*) override { Changed(group); }
    void OnChangeLeader(Group* group, ObjectGuid, ObjectGuid) override { Changed(group); }
    void OnDisband(Group* group) override { Changed(group); }
};
class ArenaObservationWorld final : public WorldScript
{
public:
    ArenaObservationWorld() : WorldScript("LeeroyRealmArenaObservationWorld", {WORLDHOOK_ON_UPDATE}) { }
    void OnUpdate(uint32) override
    {
        std::lock_guard lock(observations.mutex);
        Enabled(); // RealmModule polls intentionally do not run while disabled.
    }
};

// All queue and persistent team reads below execute on the world thread.
bool ReadQueue(ArenaObservation& record)
{
    if (record.poisoned) return false;
    auto const& source = record.source;
    auto* team = sArenaTeamMgr->GetArenaTeamById(source.team);
    if (!team || team->GetType() != source.type) return false;
    auto guids = Members(source);
    auto teamRoster = TeamRoster(team);
    if (teamRoster.empty() || !std::includes(teamRoster.begin(), teamRoster.end(), guids.begin(), guids.end())) return false;
    if (record.verified && !ExactTeam(record, team)) return false;
    auto queueType = BattlegroundMgr::BGQueueTypeId(static_cast<BattlegroundTypeId>(source.bgType), static_cast<uint8>(source.type));
    if (queueType == BATTLEGROUND_QUEUE_NONE) return false;
    auto& queue = sBattlegroundMgr->GetBattlegroundQueue(queueType);
    if (reinterpret_cast<std::uintptr_t>(&queue) != source.queue) return false;
    std::optional<GroupQueueInfo> common;
    bool complete = true;
    for (auto const& life : source.members)
    {
        if (!SameLife(life)) return false;
        auto* player = ObjectAccessor::FindConnectedPlayer(PlayerGuid(life.guid));
        bool awaiting = ArenaObservationBook::AwaitingEntry(record, life);
        if (FindOnline(life.guid) != player || player->IsBeingTeleported())
        {
            // A prior exact invitation and copied normal TeleportTo candidate
            // authorize retention only. They never prove entry or enrollment.
            if (!awaiting || !player->IsBeingTeleported() || player->GetBattlegroundId() != record.match->id
                || player->GetBgTeamId() != record.side || Destination(player->GetTeleportDest()) != record.destination) return false;
            complete = false; continue;
        }
        auto queued = queue.m_QueuedPlayers.find(PlayerGuid(life.guid));
        if (queued == queue.m_QueuedPlayers.end())
        {
            complete = false;
            auto entry = record.entries.find(life.guid);
            if (awaiting && player->GetBattlegroundId() == record.match->id && player->GetMapId() == record.match->map
                && player->GetBgTeamId() == record.side) continue;
            if (!record.match || entry == record.entries.end() || !player->InArena()
                || player->GetBattlegroundId() != record.match->id || player->GetMapId() != record.match->map
                || GroupId(player->GetGroup()) != entry->second.group || player->GetBgTeamId() != record.side) return false;
            continue;
        }
        if (reinterpret_cast<std::uintptr_t>(queued->second) != source.row) return false;
        GroupQueueInfo row;
        if (!queue.GetPlayerGroupInfoData(PlayerGuid(life.guid), &row)
            || !row.IsRated || row.ArenaTeamId != source.team || row.ArenaType != source.type
            || row.BgTypeId != source.bgType || row.BracketId != source.bracket || row.JoinTime != source.joined
            || player->GetBattlegroundQueueIndex(queueType) >= PLAYER_MAX_BATTLEGROUND_QUEUES) return false;
        std::vector<std::uint32_t> actual;
        for (auto guid : row.Players) actual.push_back(guid.GetCounter());
        if ((!record.match && actual != guids) || !std::includes(guids.begin(), guids.end(), actual.begin(), actual.end())) return false;
        auto* original = player->InBattleground() ? player->GetOriginalGroup() : player->GetGroup();
        if (!original || GroupId(original) != source.group || reinterpret_cast<std::uintptr_t>(original) != source.groupObject
            || GroupRoster(original) != guids) return false;
        if (common && (row.IsInvitedToBGInstanceGUID != common->IsInvitedToBGInstanceGUID
            || row.RemoveInviteTime != common->RemoveInviteTime || row.teamId != common->teamId)) return false;
        common = row;
    }
    if (!record.verified)
    {
        if (!complete || !common) return false;
        record.verified = true; record.teamObject = reinterpret_cast<std::uintptr_t>(team); record.teamRoster = std::move(teamRoster);
        record.teamCaptain = team->GetCaptain().GetCounter();
        record.baseline = team->GetRating();
        record.baselineAvailable = !common->IsInvitedToBGInstanceGUID
            && common->ArenaTeamRating == std::max<std::uint32_t>(record.baseline, 1);
    }
    if (common && common->IsInvitedToBGInstanceGUID)
    {
        auto instance = observations.book.instances.find(common->IsInvitedToBGInstanceGUID);
        if (instance == observations.book.instances.end() || instance->second.ended || !common->RemoveInviteTime) return false;
        auto* bg = sBattlegroundMgr->GetBattleground(instance->second.id, BATTLEGROUND_TYPE_NONE);
        if (!bg || reinterpret_cast<std::uintptr_t>(bg) != instance->second.object || !bg->isArena() || !bg->isRated()
            || bg->GetArenaType() != source.type || bg->GetBracketId() != source.bracket || common->teamId > TEAM_HORDE
            || bg->GetArenaTeamIdForTeam(common->teamId) != source.team) return false;
        if (record.match && (*record.match != instance->second || record.expiry != common->RemoveInviteTime
            || record.side != common->teamId)) return false;
        if (!record.match)
        {
            if (!complete) return false;
            if (!observations.book.Bind(source.insertion, instance->second, common->teamId, common->RemoveInviteTime)) return false;
            auto const* position = bg->GetTeamStartPosition(common->teamId);
            if (!position) return false;
            record.destination = {bg->GetMapId(), position->GetPositionX(), position->GetPositionY(),
                position->GetPositionZ(), position->GetOrientation()};
        }
    }
    if (record.baselineAvailable && team->GetRating() != record.baseline) record.baselineAvailable = false;
    return true;
}

void Poll()
{
    std::lock_guard lock(observations.mutex);
    if (!Enabled()) return;
    auto& module = RealmModule::Instance();
    // Reconcile end facts first so their final rating is never mistaken for an
    // unrelated baseline change in the ordinary live-row pass.
    for (std::size_t count = 0; count < 20 && !observations.ends.empty(); ++count)
    {
        auto end = std::move(observations.ends.front()); observations.ends.pop_front();
        bool watched = false, valid = !end.ownerSourced && end.match.epoch == module.Epoch() && end.winner < 2
            && end.teams[0] && end.teams[1] && end.teams[0] != end.teams[1];
        std::vector<std::uint32_t> members;
        for (auto const& entry : end.members)
        {
            valid = valid && SameLife(entry.life);
            watched = watched || module.Watched(entry.life.guid); members.push_back(entry.life.guid);
        }
        std::sort(members.begin(), members.end());
        std::array<std::int32_t, 2> changes{};
        bool bound = false;
        for (auto const& [id, record] : observations.book.records)
        {
            (void)id;
            if (!ArenaObservationBook::Matches(record, end)) continue;
            bool sameLife = std::all_of(record.source.members.begin(), record.source.members.end(), SameLife);
            if (!sameLife) continue;
            bound = true;
            auto* team = sArenaTeamMgr->GetArenaTeamById(record.source.team);
            if (record.baselineAvailable && ExactTeam(record, team))
            {
                // There is no public final-rating-change getter in this core.
                // Only one exact retained team baseline may supply a delta.
                std::size_t baselines = 0;
                for (auto const& [otherId, other] : observations.book.records)
                {
                    (void)otherId;
                    if (other.source.team == record.source.team && other.baselineAvailable) ++baselines;
                }
                auto delta = std::int64_t(team->GetRating()) - record.baseline;
                if (baselines == 1 && delta >= std::numeric_limits<std::int32_t>::min()
                    && delta <= std::numeric_limits<std::int32_t>::max()) changes[record.side] = static_cast<std::int32_t>(delta);
            }
        }
        if (valid && watched && bound && module.On() && module.Epoch() == end.match.epoch)
        {
            auto loser = 1u - end.winner;
            Publish(MakeArenaEnd(end.match.type, end.teams[end.winner], end.teams[loser],
                changes[end.winner], changes[loser], members));
        }
        std::erase_if(observations.book.records, [&](auto const& pair)
            { return pair.second.match && pair.second.match->generation == end.match.generation; });
    }
    // Fixed work bound and rotating insertion cursor; no FIFO head can starve
    // a later observation. One record contains at most five native queue rows.
    std::vector<std::uint64_t> visit;
    auto& records = observations.book.records;
    auto next = records.upper_bound(observations.cursor);
    for (std::size_t count = 0, size = std::min<std::size_t>(25, records.size()); count < size; ++count)
    {
        if (next == records.end()) next = records.begin();
        visit.push_back(next->first); ++next;
    }
    for (auto id : visit)
    {
        observations.cursor = id;
        auto found = records.find(id);
        if (found == records.end()) continue;
        // An end already copied to the bounded inbox must remain available to
        // its later end pass, even if its native instance has been destroyed.
        if (found->second.match && std::any_of(observations.ends.begin(), observations.ends.end(), [&](auto const& end)
            { return end.match.generation == found->second.match->generation; })) continue;
        if (!ReadQueue(found->second)) { records.erase(found); continue; }
        auto& record = found->second;
        if (!record.reported)
        {
            record.reported = true;
            auto* team = sArenaTeamMgr->GetArenaTeamById(record.source.team);
            if (team && team->GetCaptain() == PlayerGuid(record.source.captain) && module.Watched(record.source.captain)
                && module.On() && module.Epoch() == record.source.epoch)
                Publish(MakeArenaQueue(record.source.team, record.source.type, true));
        }
    }
}
}
void RegisterRealmArenaObserverHandlers()
{
    RealmModule::Instance().RegisterPoll("arena_observer", 100, Poll);
}
void AddRealmArenaObserverScripts()
{
    new ArenaObservationBattlegrounds(); new ArenaObservationLifecycle(); new ArenaObservationGroups(); new ArenaObservationWorld();
}
}
