// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "OfflineRaidJob.h"
#include "GuildPorts.h"
#include "LeeroyOfflineRaidPolicy.h"
#include "LeeroyRaidRecovery.h"
#include "LeeroyBenchmarkLoot.h"
#include "LeeroyIntegrations.h"
#include "LeeroyPinSet.h"
#include "LeeroyTrainingProtocol.h"
#include "TrainingCommands.h"
#include "TrainingRivals.h"
#include "TrainingTap.h"
#include "TrainingBenchmarkLoot.h"

#if !defined(LEEROY_HAS_DUNGEON_CLEAR)
#error "LEEROY_HAS_DUNGEON_CLEAR comes from mod-leeroy.cmake; re-run cmake"
#endif

#if LEEROY_HAS_DUNGEON_CLEAR
#include "AiObjectContext.h"
#include "DBCStores.h"
#include "Corpse.h"
#include "Event.h"
#include "Formations.h"
#include "Group.h"
#include "GroupMgr.h"
#include "InstanceSaveMgr.h"
#include "InstanceScript.h"
#include "Log.h"
#include "Map.h"
#include "MapMgr.h"
#include "ObjectMgr.h"
#include "PlayerbotAI.h"
#include "Playerbots.h"
#include "RandomPlayerbotMgr.h"
#include "WorldSession.h"

#include "Ai/Dungeon/DungeonClear/Action/DcActionShared.h"
#include "Ai/Dungeon/DungeonClear/Settings/DcSettings.h"
#include "Ai/Dungeon/DungeonClear/Util/DcLeaderSignal.h"
#include "Ai/Dungeon/DungeonClear/Util/DcRun.h"
#include "DcStrategyGate.h"
#include "TestRun/DcTestDriver.h"
#include "TestRun/DcTestRunManager.h"
#include "Util/DcDungeonAccess.h"
#include "Util/DcPlayerbotsConfig.h"

#include <algorithm>
#include <cmath>
#include <memory>
#include <map>
#include <limits>
#include <set>
#include <vector>
#endif

#include <string>
#include <utility>

namespace Leeroy::Guild
{
namespace
{
LeeroyCommandResult Fail(std::string reason) { return {false, std::move(reason)}; }

#if LEEROY_HAS_DUNGEON_CLEAR
constexpr uint32 GroupingMs = 10000;
constexpr uint32 SetupMs = 60000;
constexpr uint32 RestartSettleMs = 20000;
constexpr uint32 CleanupMs = 60000;

enum class Stage { Grouping, Teleporting, Starting, Running, Restarting, TearingDown };

char const* Name(Stage stage)
{
    switch (stage)
    {
        case Stage::Grouping: return "grouping";
        case Stage::Teleporting: return "teleporting";
        case Stage::Starting: return "starting";
        case Stage::Running: return "running";
        case Stage::Restarting: return "restarting";
        case Stage::TearingDown: return "tearing_down";
    }
    return "unknown";
}

struct Member
{
    uint32 guid;
    WorldLocation origin;
    Difficulty originalDifficulty;
    bool originalLeaderFlag = false;
    bool changed = false;
    bool masterAssigned = false;
    bool masterReleased = false, journalRestored = false;
    RaidFinalReset finalReset;
    bool moved = false;
    bool restartIssued = false;
    bool returnIssued = false;
    bool finished = false;
    bool cleanupNoted = false;
    uint32 returnRetryAt = 0;
};

struct Job
{
    OfflineRaidCmd command;
    RaidJobMode mode = RaidJobMode::Offline;
    std::vector<uint32> bosses;
    std::map<uint32, RaidBodyLease> bodies;
    RaidBodyLease driverBody;
    RaidCleanupTerminal terminal;
    OfflineRaidLimits limits{};
    JobEvent event;
    AreaTriggerTeleport entrance;
    Difficulty difficulty = RAID_DIFFICULTY_10MAN_NORMAL;
    ObjectGuid group, driver;
    uint32 human = 0;
    uint32 guild = 0;
    uint8 team = 0;
    uint64 trainingToken = 0;
    uint64 rewardToken = 0;
    bool pinsInstalled = false, registered = false;
    bool rewardBound = false, groupReleased = false;
    std::vector<Member> members;
    std::vector<uint8> previousBossStates;
    std::vector<uint32> bossWipes;
    uint32 elapsed = 0, stageElapsed = 0, dcOff = 0, restarts = 0;
    Stage stage = Stage::Grouping;
    bool announced = false, prepared = false, leaderSent = false, followersSent = false;
    bool dcIssued = false, cleanupFailed = false;
    bool waitOverrideInstalled = false, previousWaitOverride = false, previousWaitValue = false;
    std::string terminalReason, terminalState = "done";
};

// World-thread only. Store identifiers and value snapshots; every Player/Group is resolved anew on each tick.
std::unique_ptr<Job> active;
uint64 nextRewardToken = 1; // Independent of the later instance-bound training-run token.

ObjectGuid Guid(uint32 low) { return ObjectGuid::Create<HighGuid::Player>(low); }
template<class T> std::uintptr_t Identity(T* object) { return reinterpret_cast<std::uintptr_t>(object); }
RaidBodyLease Lease(Player* player)
{
    return {Identity(player), Identity(GET_PLAYERBOT_AI(player)), Identity(player->GetSession())};
}
bool SameBody(RaidBodyLease const& body, Player* player)
{
    return player && body.Matches(Identity(player), Identity(GET_PLAYERBOT_AI(player)), Identity(player->GetSession()));
}

bool HumanConnected(uint32 low)
{
    Player* player = low ? ObjectAccessor::FindConnectedPlayer(Guid(low)) : nullptr;
    return player && player->GetSession(); // A worldport is still an online human.
}

std::vector<uint32> Requested(OfflineRaidCmd const& cmd)
{
    std::vector<uint32> out{cmd.leaderGuid};
    out.insert(out.end(), cmd.members.begin(), cmd.members.end());
    return out;
}

bool Contains(Job const& job, uint32 low)
{
    return std::any_of(job.members.begin(), job.members.end(), [low](Member const& member)
    {
        return member.guid == low;
    });
}

bool Benchmark(Job const& job) { return job.mode == RaidJobMode::Benchmark; }

bool Controlled(Job const& job, Player* player)
{
    if (!player || !GET_PLAYERBOT_AI(player) || IsSelfBot(player)
        || player->GetGUID().GetCounter() == job.human || player->GetGUID().GetCounter() == HumanGuid()
        || DcTestRunManager::Instance().IsReserved(player->GetGUID())) return false;
    if (Benchmark(job))
    {
        auto body = job.bodies.find(player->GetGUID().GetCounter());
        if (body == job.bodies.end() || !SameBody(body->second, player)
            || !player->GetSession() || player->GetSession()->IsLoggingOut()) return false;
    }
    return Benchmark(job) ? Training::RivalIdentity(player) && player->GetLevel() >= 80
        && player->GetTeamId() == job.team : IsCast(player->GetGUID().GetCounter());
}

Group* FindGroup(Job const& job)
{
    return job.group ? sGroupMgr->GetGroupByGUID(job.group.GetCounter()) : nullptr;
}

bool OwnsGroup(Job const& job, Group const* group, bool complete = true)
{
    if (!group || group->GetGUID() != job.group || !group->isRaidGroup()
        || group->isLFGGroup() || group->isBGGroup() || group->isBFGroup())
        return false;
    if (Benchmark(job) && complete && group->GetMembersCount() != job.members.size()) return false;
    for (auto const& slot : group->GetMemberSlots())
    {
        if ((!Benchmark(job) && !IsCast(slot.guid.GetCounter())) || !Contains(job, slot.guid.GetCounter())
            || DcTestRunManager::Instance().IsReserved(slot.guid))
            return false;
        auto const member = std::find_if(job.members.begin(), job.members.end(), [&slot](Member const& candidate)
        {
            return candidate.guid == slot.guid.GetCounter();
        });
        if (Benchmark(job) && !Controlled(job, FindOnline(slot.guid.GetCounter()))) return false;
        if (Player* player = FindOnline(slot.guid.GetCounter()); player && member->masterAssigned)
        {
            PlayerbotAI* ai = GET_PLAYERBOT_AI(player);
            if (!Controlled(job, player) || !ai || IsSelfBot(player)
                || (ai->GetMaster() && Identity(ai->GetMaster()) != job.driverBody.player)
                || (Benchmark(job) && (!ai->GetMaster() || Training::DummyTrialOwns(member->guid))))
                return false;
        }
    }
    return true;
}

bool Controlled(Player* player)
{
    return player && IsCast(player->GetGUID().GetCounter()) && GET_PLAYERBOT_AI(player) && !IsSelfBot(player)
        && !DcTestRunManager::Instance().IsReserved(player->GetGUID());
}

bool InJobGroup(Job const& job, Player* player)
{
    return Controlled(job, player) && player->GetGroup() && player->GetGroup()->GetGUID() == job.group;
}

bool Present(Job const& job, Player* player)
{
    return InJobGroup(job, player) && player->IsInWorld() && !player->IsBeingTeleported()
        && player->GetMapId() == job.command.mapId && player->GetInstanceId() == job.event.instanceId;
}

std::string InstanceConflict(OfflineRaidCmd const& cmd)
{
    for (uint32 low : Requested(cmd))
        if (Player* player = ObjectAccessor::FindConnectedPlayer(Guid(low)))
        {
            MapEntry const* destination = player->IsBeingTeleported()
                ? sMapStore.LookupEntry(player->GetTeleportDest().GetMapId()) : nullptr;
            if ((player->IsInWorld() && player->GetMap()->IsDungeon()) || (destination && destination->IsDungeon()))
                return "in_instance:" + std::to_string(low);
        }
    return {};
}

std::string Unusable(Player* player)
{
    if (!player)
        return "offline";
    if (!Controlled(player))
        return "not_available";
    if (player->InBattleground() || player->InBattlegroundQueue())
        return "in_bg";
    if (player->isDead() || player->HasPlayerFlag(PLAYER_FLAGS_GHOST))
        return "dead";
    if (player->IsInFlight() || player->GetTransport() || player->GetVehicle())
        return "in_flight";
    if (player->GetGroup() && (player->GetGroup()->isLFGGroup() || player->GetGroup()->isBGGroup()
        || player->GetGroup()->isBFGroup()))
        return "in_lfg";
    if (player->IsBeingTeleported() || player->HasPendingBind())
        return "teleporting";
    if (player->GetMap()->IsDungeon())
        return "in_instance";
    if (player->IsInCombat())
        return "in_combat";
    return {};
}

void Enter(Job& job, Stage stage)
{
    job.stage = stage;
    job.stageElapsed = 0;
    job.announced = false;
}

void Stop(Job& job, std::string reason, bool failed = false)
{
    if (job.stage == Stage::TearingDown)
        return;
    job.terminalReason = std::move(reason);
    job.terminalState = failed ? "failed" : "done";
    if (job.rewardToken) Training::BenchmarkRewards().Returning(job.rewardToken);
    Enter(job, Stage::TearingDown);
}

void SetupFailure(Job& job, std::string reason)
{
    Stop(job, "setup:" + std::string(Name(job.stage)) + ":" + reason, true);
}

void Progress(Job& job)
{
    if (job.terminal.Reported()) return;
    job.event.state = "progress";
    job.event.stage = Name(job.stage);
    PublishJob(job.event);
    job.announced = true;
}

Player* Driver(Job const& job)
{
    Player* driver = DcTestDriver::Get();
    return driver && driver->GetGUID() == job.driver
        && (!Benchmark(job) || SameBody(job.driverBody, driver)) ? driver : nullptr;
}

void ApplyRaidHabits(Job const& job, Player* player, Player* driver)
{
    PlayerbotAI* ai = GET_PLAYERBOT_AI(player);
    ai->SetMaster(driver);
    ai->ResetStrategies();
    Training::JournalReset(player->GetGUID().GetCounter());
    ai->ChangeStrategy("-follow", BOT_STATE_NON_COMBAT);
    ai->ChangeStrategy("-follow", BOT_STATE_COMBAT);
    auto* formation = static_cast<FormationValue*>(ai->GetAiObjectContext()->GetValue<Formation*>("formation"));
    if (formation)
        formation->Load("arrow");
    if (!PlayerbotAI::IsTank(player, true) && !PlayerbotAI::IsHeal(player, true))
    {
        ai->ChangeStrategy("+wait for attack", BOT_STATE_COMBAT);
        ai->GetAiObjectContext()->GetValue<uint8>("wait for attack time")->Set(4);
    }
    // Benchmark corpses are never opened by the ordinary noncombat loot strategy.
    if (Benchmark(job)) ai->ChangeStrategy("-loot", BOT_STATE_NON_COMBAT);
}

void Grouping(Job& job)
{
    if (job.stageElapsed >= GroupingMs)
    {
        SetupFailure(job, "timeout");
        return;
    }
    if (auto const conflict = InstanceConflict(job.command); !conflict.empty())
    {
        SetupFailure(job, conflict);
        return;
    }
    Player* driver = Driver(job);
    Player* leader = FindOnline(job.command.leaderGuid);
    if (!driver || !leader || !PlayerbotAI::IsTank(leader, true))
    {
        SetupFailure(job, "leader_not_tank");
        return;
    }

    if (Benchmark(job))
    {
        if (!Pins().Owned(kPinOwnerBenchmark).empty())
        {
            SetupFailure(job, "pin_owner_busy");
            return;
        }
        auto requested = Requested(job.command);
        LeeroySetExtraPins(kPinOwnerBenchmark, {requested.begin(), requested.end()}, kPinMute);
        job.pinsInstalled = true;
        LeeroyApplyPins();
        // SetPinnedBots returns the WHOLE accepted union, not this owner's incremental count.
        if (!std::all_of(requested.begin(), requested.end(), [](uint32 guid)
            { return sRandomPlayerbotMgr.IsPinned(guid); }))
        {
            SetupFailure(job, "pin_refused");
            return;
        }
    }

    // Complete the usable-roster preflight before removing anyone from an existing group.
    for (uint32 low : Requested(job.command))
    {
        Player* player = FindOnline(low);
        if (Training::DummyTrialOwns(low) || (Benchmark(job)
            && (!Controlled(job, player) || !Training::RivalAvailable(player, 80, true)
                || !Training::RivalBindingsSafe(player, job.command.mapId)
                || player->GetTeamId() != job.team)))
        {
            SetupFailure(job, "not_available:" + std::to_string(low));
            return;
        }
        if (auto const reason = Benchmark(job) ? std::string{} : Unusable(player); !reason.empty())
        {
            job.event.skipped.emplace_back(low, reason);
            continue;
        }
        job.members.push_back({low, WorldLocation(player->GetMapId(), player->GetPosition()),
                               player->GetRaidDifficulty()});
        job.members.back().originalLeaderFlag = player->HasPlayerFlag(PLAYER_FLAGS_GROUP_LEADER);
    }
    if (!Contains(job, job.command.leaderGuid) || !Contains(job, job.command.lootMasterGuid)
        || job.members.size() < job.command.minMembers)
    {
        SetupFailure(job, "too_few");
        return;
    }
    for (auto const& member : job.members)
        job.event.guids.push_back(member.guid);

    job.members.front().changed = true;
    if (leader->GetGroup())
        leader->RemoveFromGroup();
    auto created = std::make_unique<Group>();
    if (!created->Create(leader))
    {
        SetupFailure(job, "create_failed");
        return;
    }
    Group* group = created.release();
    sGroupMgr->AddGroup(group);
    job.group = group->GetGUID();
    group->ConvertToRaid();
    for (std::size_t index = 1; index < job.members.size(); ++index)
    {
        Member& member = job.members[index];
        Player* player = FindOnline(member.guid);
        member.changed = true;
        if (player->GetGroup())
            player->RemoveFromGroup();
        if (!group->AddMember(player))
        {
            SetupFailure(job, "add_failed:" + std::to_string(member.guid));
            return;
        }
    }
    group->SetRaidDifficulty(job.difficulty);
    group->SetLootMethod(MASTER_LOOT);
    group->SetLootThreshold(Benchmark(job) ? ITEM_QUALITY_POOR : ITEM_QUALITY_EPIC);
    group->SetMasterLooterGuid(Guid(job.command.lootMasterGuid));
    uint32 subgroup = 0;
    for (Member& member : job.members)
    {
        ObjectGuid const guid = Guid(member.guid);
        group->ChangeMembersGroup(guid, static_cast<uint8>(subgroup++ % MAX_RAID_SUBGROUPS));
        group->SetGroupMemberFlag(guid, member.guid == job.command.leaderGuid, MEMBER_FLAG_MAINTANK);
        bool const assistant = std::find(job.command.assistants.begin(), job.command.assistants.end(), member.guid)
            != job.command.assistants.end();
        group->SetGroupMemberFlag(guid, assistant, MEMBER_FLAG_ASSISTANT);
        ApplyRaidHabits(job, FindOnline(member.guid), driver);
        member.masterAssigned = true;
    }
    group->SendUpdate();
    if (Benchmark(job))
    {
        if (nextRewardToken == std::numeric_limits<uint64>::max())
        {
            SetupFailure(job, "loot_guard_exhausted");
            return;
        }
        Training::BenchmarkLootScope scope;
        scope.token = nextRewardToken++;
        scope.group = job.group.GetRawValue();
        scope.groupLow = job.group.GetCounter();
        scope.map = job.command.mapId;
        scope.bodies = job.bodies;
        auto token = scope.token;
        if (!Training::BenchmarkRewards().Install(std::move(scope)))
        {
            SetupFailure(job, "loot_guard_unavailable");
            return;
        }
        job.rewardToken = token; // Pending before the first worldport: no destructive action until Bind.
    }
    Enter(job, Stage::Teleporting);
}

void Teleporting(Job& job)
{
    if (job.stageElapsed >= SetupMs)
    {
        SetupFailure(job, "timeout");
        return;
    }
    Group* group = FindGroup(job);
    if (!OwnsGroup(job, group) || group->GetLeaderGUID() != Guid(job.command.leaderGuid))
    {
        SetupFailure(job, "group_changed");
        return;
    }
    if (!job.prepared)
    {
        for (Member const& member : job.members)
        {
            Player* player = FindOnline(member.guid);
            if (!InJobGroup(job, player) || player->IsBeingTeleported())
                return;
            if (Benchmark(job) && !Training::RivalBindingsSafe(player, job.command.mapId))
            {
                SetupFailure(job, "foreign_bind:" + std::to_string(member.guid));
                return;
            }
            if (player->GetMap()->IsDungeon())
            {
                SetupFailure(job, "in_instance:" + std::to_string(member.guid));
                return;
            }
            if (!player->CheckInstanceCount(0))
            {
                SetupFailure(job, "instance_limit:" + std::to_string(member.guid));
                return;
            }
        }
        for (Member const& member : job.members)
        {
            Player* player = FindOnline(member.guid);
            sInstanceSaveMgr->PlayerCreateBoundInstancesMaps(player->GetGUID());
            if (!Benchmark(job))
                for (uint8 difficulty = 0; difficulty < 4; ++difficulty)
                    sInstanceSaveMgr->PlayerUnbindInstance(player->GetGUID(), job.command.mapId,
                        Difficulty(difficulty), true, player);
            DcDungeonAccess::GrantEntry(player, job.command.mapId, job.difficulty);
        }
        job.prepared = true;
    }
    auto const& entrance = job.entrance;
    if (!job.leaderSent)
    {
        Player* leader = FindOnline(job.command.leaderGuid);
        if (!InJobGroup(job, leader) || leader->IsBeingTeleported())
            return;
        if (Benchmark(job) && !Training::RivalBindingsSafe(leader, job.command.mapId))
        {
            SetupFailure(job, "foreign_bind:" + std::to_string(job.command.leaderGuid));
            return;
        }
        if (leader->TeleportTo(entrance.target_mapId, entrance.target_X, entrance.target_Y,
            entrance.target_Z, entrance.target_Orientation))
        {
            job.leaderSent = true;
            job.members.front().moved = true;
        }
        return;
    }
    Player* leader = FindOnline(job.command.leaderGuid);
    if (!InJobGroup(job, leader) || leader->IsBeingTeleported() || leader->GetMapId() != job.command.mapId
        || !leader->GetInstanceId())
        return;
    if (!job.event.instanceId)
        job.event.instanceId = leader->GetInstanceId();
    if (leader->GetInstanceId() != job.event.instanceId || leader->GetMap()->GetDifficulty() != job.difficulty)
    {
        SetupFailure(job, "instance_changed");
        return;
    }
    if (!job.trainingToken)
    {
        job.trainingToken = Training::MarkJobRun(job.command.leaderGuid);
        if (!job.trainingToken)
        {
            SetupFailure(job, "training_run_unavailable");
            return;
        }
    }
    if (Benchmark(job) && !job.registered)
    {
        auto* instance = leader->GetInstanceScript();
        if (!instance || !instance->GetEncounterCount() || std::any_of(job.bosses.begin(), job.bosses.end(),
            [instance](uint32 boss) { return boss >= instance->GetEncounterCount(); }))
        {
            SetupFailure(job, "bad_bosses");
            return;
        }
        try
        {
            Training::TrainingTap::Instance().RegisterInstance(job.command.mapId, job.event.instanceId, job.bosses);
            job.registered = true;
        }
        catch (...)
        {
            SetupFailure(job, "telemetry_unavailable");
            return;
        }
    }
    if (Benchmark(job) && !job.rewardBound)
    {
        if (!Training::BenchmarkRewards().Bind(job.rewardToken, job.event.instanceId))
        {
            SetupFailure(job, "loot_guard_unavailable");
            return;
        }
        job.rewardBound = true;
    }
    // The leader's completed worldport establishes its bind before any follower resolves a destination copy.
    if (!job.followersSent)
    {
        bool allSent = true;
        for (std::size_t index = 1; index < job.members.size(); ++index)
        {
            Member& member = job.members[index];
            if (member.moved)
                continue;
            Player* player = FindOnline(member.guid);
            if (!InJobGroup(job, player) || player->IsBeingTeleported())
            {
                allSent = false;
                continue;
            }
            if (player->GetMap()->IsDungeon())
            {
                SetupFailure(job, "in_instance:" + std::to_string(member.guid));
                return;
            }
            if (Benchmark(job) && !Training::RivalBindingsSafe(player, job.command.mapId,
                job.event.instanceId, job.difficulty))
            {
                SetupFailure(job, "foreign_bind:" + std::to_string(member.guid));
                return;
            }
            member.moved = player->TeleportTo(entrance.target_mapId, entrance.target_X, entrance.target_Y,
                entrance.target_Z, entrance.target_Orientation);
            allSent = allSent && member.moved;
        }
        job.followersSent = allSent;
    }
    if (job.followersSent && std::all_of(job.members.begin(), job.members.end(), [&job](Member const& member)
        { return Present(job, FindOnline(member.guid)); }))
        Enter(job, Stage::Starting);
}

bool ReadBosses(Job& job, Player* leader, bool baseline)
{
    InstanceScript* instance = leader ? leader->GetInstanceScript() : nullptr;
    if (!instance || !instance->GetEncounterCount() || instance->GetEncounterCount() > 1024)
        return false;
    uint32 const count = instance->GetEncounterCount();
    if (baseline)
    {
        job.previousBossStates.clear();
        job.bossWipes.assign(count, 0);
        for (uint32 index = 0; index < count; ++index)
        {
            uint8 const state = static_cast<uint8>(instance->GetBossState(index));
            if (state == DONE || state == TO_BE_DECIDED)
                return false;
            job.previousBossStates.push_back(state);
        }
    }
    else if (count != job.previousBossStates.size())
        return false;
    job.event.kills = 0;
    for (uint32 index = 0; index < count; ++index)
    {
        uint8 const current = static_cast<uint8>(instance->GetBossState(index));
        if (IsWipe(job.previousBossStates[index], current))
        {
            ++job.bossWipes[index];
            ++job.event.wipes;
        }
        job.previousBossStates[index] = current;
        if (current == DONE)
            ++job.event.kills;
    }
    return true;
}

bool Enable(Job& job, Player* leader)
{
    Player* driver = Driver(job);
    if (!driver || !Present(job, leader) || !OwnsGroup(job, FindGroup(job))
        || leader->GetGroup()->GetLeaderGUID() != leader->GetGUID()
        || DcLeaderSignal::FindLeaderTank(leader) != leader)
        return false;
    for (Member const& member : job.members)
        if (Player* player = FindOnline(member.guid); Present(job, player))
            DcStrategyGate::Reconcile(player);
    PlayerbotAI* ai = GET_PLAYERBOT_AI(leader);
    if (!job.waitOverrideInstalled)
    {
        job.previousWaitOverride = DcSettings::HasOverride(leader->GetGUID(), "WaitAtBoss");
        job.previousWaitValue = DcSettings::GetBool(leader->GetGUID(), "WaitAtBoss");
        if (!DcSettings::SetOverride(leader->GetGUID(), "WaitAtBoss", 0.0))
        {
            SetupFailure(job, "wait_override_failed");
            return false;
        }
        job.waitOverrideInstalled = true;
    }
    job.dcIssued = true;
    ai->DoSpecificAction("dc on", Event("dc", "", driver), true);
    return DcRun::Of(ai).enabled;
}

void Starting(Job& job)
{
    if (job.stageElapsed >= SetupMs)
    {
        SetupFailure(job, "timeout");
        return;
    }
    Player* leader = FindOnline(job.command.leaderGuid);
    if (!Present(job, leader))
        return;
    if (!OwnsGroup(job, FindGroup(job)) || leader->GetGroup()->GetLeaderGUID() != leader->GetGUID())
    {
        SetupFailure(job, "group_changed");
        return;
    }
    if (job.previousBossStates.empty() && !ReadBosses(job, leader, true))
    {
        SetupFailure(job, "no_fresh_encounters");
        return;
    }
    if (Enable(job, leader))
        Enter(job, Stage::Running);
}

void Running(Job& job, uint32 diff)
{
    Player* leader = FindOnline(job.command.leaderGuid);
    bool const leaderPresent = Present(job, leader) && OwnsGroup(job, FindGroup(job))
        && leader->GetGroup()->GetLeaderGUID() == leader->GetGUID();
    if (leaderPresent && !ReadBosses(job, leader, false))
    {
        Stop(job, "encounters_changed", true);
        return;
    }
    if (Benchmark(job) && leaderPresent && Training::FrontierReached(job.previousBossStates, job.bosses))
    {
        Stop(job, "frontier");
        return; // TickOfflineRaid immediately enters teardown in this same post-map world tick.
    }
    uint32 present = 0;
    for (Member const& member : job.members)
        if (Present(job, FindOnline(member.guid)))
            ++present;
    bool const enabled = leaderPresent && DcRun::Of(GET_PLAYERBOT_AI(leader)).enabled;
    job.dcOff = enabled ? 0 : AdvanceElapsed(job.dcOff, diff);
    uint32 const mostWipes = job.bossWipes.empty() ? 0 : *std::max_element(job.bossWipes.begin(), job.bossWipes.end());
    OfflineRaidState const state{job.elapsed, HumanConnected(job.human), leaderPresent, present, job.event.wipes,
        mostWipes, job.event.kills, static_cast<uint32>(job.previousBossStates.size()),
        enabled, job.dcOff, job.restarts};
    Decision const decision = Decide(state, job.limits);
    if (decision.verdict == Verdict::Done)
        Stop(job, decision.reason);
    else if (decision.verdict == Verdict::Restart)
    {
        ++job.restarts;
        for (Member& member : job.members)
            member.restartIssued = false;
        Enter(job, Stage::Restarting);
    }
}

void Revive(Player* player)
{
    player->CombatStop(true);
    if (!player->IsAlive() || player->HasPlayerFlag(PLAYER_FLAGS_GHOST))
    {
        player->RemoveAurasDueToSpell(27827); // Spirit of Redemption must not kill the freshly resurrected character.
        player->ResurrectPlayer(1.0f, false);
        player->SpawnCorpseBones();
    }
}

void Restarting(Job& job)
{
    if (job.stageElapsed >= SetupMs)
    {
        SetupFailure(job, "timeout");
        return;
    }
    if (!OwnsGroup(job, FindGroup(job)))
    {
        Stop(job, "group_changed", true);
        return;
    }
    bool allReady = true;
    for (Member& member : job.members)
    {
        Player* player = FindOnline(member.guid);
        if (!Present(job, player))
        {
            allReady = false;
            continue;
        }
        if (!member.restartIssued)
        {
            Revive(player);
            auto const& point = job.entrance;
            member.restartIssued = player->TeleportTo(point.target_mapId, point.target_X, point.target_Y,
                point.target_Z, point.target_Orientation);
            allReady = false;
        }
    }
    Player* leader = FindOnline(job.command.leaderGuid);
    if (Present(job, leader) && !ReadBosses(job, leader, false))
    {
        Stop(job, "encounters_changed", true);
        return;
    }
    if (allReady && job.stageElapsed >= RestartSettleMs && Enable(job, leader))
    {
        job.dcOff = 0;
        Enter(job, Stage::Running);
    }
}

bool AtOrigin(Member const& member, Player* player)
{
    return player && player->IsInWorld() && !player->IsBeingTeleported()
        && !player->GetInstanceId()
        && player->GetMapId() == member.origin.GetMapId()
        && player->GetDistance2d(member.origin.GetPositionX(), member.origin.GetPositionY()) <= 5.0f
        && std::abs(player->GetPositionZ() - member.origin.GetPositionZ()) <= 5.0f;
}

bool BenchmarkRecoveryLocation(Job const& job, Member const& member, Player* player)
{
    if (!player) return false;
    if (AtOrigin(member, player)) return true;
    if (member.moved && job.event.instanceId && player->GetMapId() == job.command.mapId
        && player->GetInstanceId() == job.event.instanceId) return true;
    // A ghost may be at a graveyard outside the instance. Require its actual corpse to belong to this pull,
    // not merely a dead flag on a body that somebody moved elsewhere.
    if ((!player->IsAlive() || player->HasPlayerFlag(PLAYER_FLAGS_GHOST)) && player->GetGroup()
        && player->GetGroup()->GetGUID() == job.group && GET_PLAYERBOT_AI(player)
        && Identity(GET_PLAYERBOT_AI(player)->GetMaster()) == job.driverBody.player)
        if (auto* corpse = player->GetCorpse(); corpse && corpse->GetOwnerGUID() == player->GetGUID()
            && corpse->GetMapId() == job.command.mapId && corpse->GetInstanceId() == job.event.instanceId
            && job.event.instanceId) return true;
    return false;
}

void CleanupFailure(Job& job, Member& member, std::string reason, bool retain = false)
{
    job.cleanupFailed = true;
    member.finished = !Benchmark(job) && !retain; // Never release a failed restoration or an unverified rival return.
    if (!member.cleanupNoted)
    {
        job.event.skipped.emplace_back(member.guid, "cleanup:" + reason);
        member.cleanupNoted = true;
        LOG_WARN("module.leeroy", "leeroy: raid {} retains cleanup for guid {}: {}",
            job.event.id, member.guid, reason);
    }
}

void DisableOwnedRun(Job& job)
{
    if (!job.dcIssued)
        return;
    Player* leader = FindOnline(job.command.leaderGuid);
    if (!Present(job, leader) || !OwnsGroup(job, FindGroup(job)))
        return;
    PlayerbotAI* ai = GET_PLAYERBOT_AI(leader);
    if (Player* driver = Driver(job))
        ai->DoSpecificAction("dc off", Event("dc", "", driver), true);
    // dc off deliberately ignores a dead/non-elected tank. The harness uses this owned-run teardown directly.
    if (DcRun::Of(ai).enabled)
        DcActionShared::DisableDungeonClear(ai, "Offline raid ended.");
    job.dcIssued = false;
}

bool RestoreWaitOverride(Job& job)
{
    if (!job.waitOverrideInstalled) return true;
    ObjectGuid const owner = Guid(job.command.leaderGuid);
    // Restore only this job's installed value; preserve a detectable later replacement or removal.
    if (DcSettings::HasOverride(owner, "WaitAtBoss") && !DcSettings::GetBool(owner, "WaitAtBoss"))
    {
        if (job.previousWaitOverride)
        {
            if (!DcSettings::SetOverride(owner, "WaitAtBoss", job.previousWaitValue ? 1.0 : 0.0)
                || !DcSettings::HasOverride(owner, "WaitAtBoss")
                || DcSettings::GetBool(owner, "WaitAtBoss") != job.previousWaitValue) return false;
        }
        else
        {
            DcSettings::ResetOverride(owner, "WaitAtBoss");
            if (DcSettings::HasOverride(owner, "WaitAtBoss")) return false;
        }
    }
    job.waitOverrideInstalled = false;
    return true;
}

bool Teardown(Job& job)
{
    // Cancellation can arrive between the leader's teleport request and its acknowledgement. Bind the cleanup
    // to the returned leader's actual instance only while the group we created still owns that participant.
    if (!job.event.instanceId && job.leaderSent)
        if (Player* leader = FindOnline(job.command.leaderGuid); InJobGroup(job, leader)
            && !leader->IsBeingTeleported() && leader->GetMapId() == job.command.mapId)
            job.event.instanceId = leader->GetInstanceId();
    DisableOwnedRun(job);
    if (job.registered)
    {
        Training::TrainingTap::Instance().UnregisterInstance(job.command.mapId, job.event.instanceId);
        job.registered = false;
    }
    bool pendingTransfer = false;
    for (Member const& member : job.members)
        if (Player* player = ObjectAccessor::FindConnectedPlayer(Guid(member.guid)); member.changed && player)
            pendingTransfer = pendingTransfer || player->IsBeingTeleported();

    if (Benchmark(job))
    {
        // Restore BEFORE disband/releasing the master. A groupless random bot clears its master on the next
        // AI update, which would destroy the proof needed for a failed journal's safe retry.
        bool pendingRestore = job.group && !job.groupReleased && !OwnsGroup(job, FindGroup(job), false);
        for (Member& member : job.members)
        {
            if (pendingTransfer) { pendingRestore = true; break; }
            if (member.changed && !member.finished)
                if (auto* player = FindOnline(member.guid); Controlled(job, player)
                    && !Training::RivalBindingsSafe(player, job.command.mapId, job.event.instanceId, job.difficulty))
                {
                    CleanupFailure(job, member, "foreign_bind", true);
                    pendingRestore = true;
                    continue;
                }
            if (!member.masterAssigned || member.finished || (member.masterReleased && member.journalRestored)) continue;
            auto* player = FindOnline(member.guid);
            if (!Controlled(job, player) || !player->GetGroup() || player->GetGroup()->GetGUID() != job.group
                || Identity(GET_PLAYERBOT_AI(player)->GetMaster()) != job.driverBody.player)
            {
                CleanupFailure(job, member, "restore_ownership", true);
                pendingRestore = true;
                continue;
            }
            if (!BenchmarkRecoveryLocation(job, member, player))
            {
                CleanupFailure(job, member, "location_changed", true);
                pendingRestore = true;
                continue;
            }
            if (member.journalRestored) continue;
            player->CombatStop(true);
            if (player->IsBeingTeleported()) { pendingRestore = true; continue; }
            if (Training::RestoreJournaled(player, "*")) member.journalRestored = true;
            else
            {
                CleanupFailure(job, member, "journal_restore", true);
                pendingRestore = true;
            }
        }
        if (pendingRestore || pendingTransfer)
        {
            // A retained journal must not leave an otherwise owned bot fighting the frontier. Return it while
            // keeping its group/controller receipt; do not restore or teleport a replacement session.
            for (Member& member : job.members)
            {
                auto* player = FindOnline(member.guid);
                if (!member.changed || !Controlled(job, player) || player->IsBeingTeleported()
                    || !player->GetGroup() || player->GetGroup()->GetGUID() != job.group
                    || Identity(GET_PLAYERBOT_AI(player)->GetMaster()) != job.driverBody.player
                    || !BenchmarkRecoveryLocation(job, member, player)) continue;
                player->CombatStop(true);
                if (!member.moved || AtOrigin(member, player) || !job.event.instanceId
                    || player->GetMapId() != job.command.mapId || player->GetInstanceId() != job.event.instanceId)
                    continue;
                Revive(player);
                if (!member.returnIssued || job.stageElapsed >= member.returnRetryAt)
                {
                    member.returnIssued = player->TeleportTo(member.origin);
                    member.returnRetryAt = AdvanceElapsed(job.stageElapsed, 5000);
                }
            }
            return false;
        }
        // Restore the exact run while the leader still has our group/controller/location proof. Once released,
        // a correctly returned leader may wander while another member is still returning.
        if (job.trainingToken)
        {
            if (!Training::ClearTrainingRun(job.command.leaderGuid, job.trainingToken)) return false;
            job.trainingToken = 0;
        }
        if (!RestoreWaitOverride(job)) return false;
    }

    Group* group = FindGroup(job);
    if (!pendingTransfer && OwnsGroup(job, group, !Benchmark(job)))
    {
        group->Disband(); // Disband deletes the Group; no pointer to it is used below.
        job.groupReleased = true;
    }

    for (Member& member : job.members)
    {
        if (member.finished)
            continue;
        if (!member.changed)
        {
            member.finished = true;
            continue;
        }
        Player* connected = ObjectAccessor::FindConnectedPlayer(Guid(member.guid));
        if (connected && connected->IsBeingTeleported())
        {
            if (job.stageElapsed >= CleanupMs)
                CleanupFailure(job, member, "teleport_timeout");
            continue;
        }
        Player* player = FindOnline(member.guid);
        if (!player)
        {
            if (job.stageElapsed >= CleanupMs)
                CleanupFailure(job, member, "offline");
            continue;
        }
        if (!Controlled(job, player) || (player->GetGroup() && player->GetGroup()->GetGUID() != job.group))
        {
            CleanupFailure(job, member, "ownership_changed");
            continue;
        }
        PlayerbotAI* ai = GET_PLAYERBOT_AI(player);
        if (member.masterAssigned && ((ai->GetMaster() && Identity(ai->GetMaster()) != job.driverBody.player)
            || (Benchmark(job) && !member.masterReleased && !ai->GetMaster())))
        {
            CleanupFailure(job, member, "master_changed");
            continue;
        }
        bool const inInstance = job.event.instanceId && player->GetMapId() == job.command.mapId
            && player->GetInstanceId() == job.event.instanceId;
        bool const recoveringGhost = (!player->IsAlive() || player->HasPlayerFlag(PLAYER_FLAGS_GHOST))
            && ((player->GetGroup() && player->GetGroup()->GetGUID() == job.group)
                || (member.masterAssigned && Identity(ai->GetMaster()) == job.driverBody.player));
        if ((Benchmark(job) && !BenchmarkRecoveryLocation(job, member, player))
            || (!Benchmark(job) && member.moved && !member.returnIssued && !AtOrigin(member, player)
                && !inInstance && !recoveringGhost))
        {
            CleanupFailure(job, member, "location_changed");
            continue;
        }
        if (player->GetGroup())
        {
            // An outsider joined our group: remove only this job's members, re-resolving after each removal.
            player->RemoveFromGroup();
            if (player->GetGroup())
            {
                if (job.stageElapsed >= CleanupMs)
                    CleanupFailure(job, member, "group_not_released");
                continue;
            }
        }
        if (member.guid == job.command.leaderGuid && job.dcIssued && DcRun::Of(ai).enabled
            && Identity(ai->GetMaster()) == job.driverBody.player)
        {
            // After leaving a now-mixed group this can only affect the original leader's own run state.
            DcActionShared::DisableDungeonClear(ai, "Offline raid ended.");
            job.dcIssued = false;
        }
        if (Identity(ai->GetMaster()) == job.driverBody.player)
        {
            if (Benchmark(job) && !member.journalRestored && !Training::RestoreJournaled(player, "*"))
            {
                CleanupFailure(job, member, "journal_restore", true);
                continue;
            }
            member.journalRestored = true;
            ai->SetMaster(nullptr);
            member.masterReleased = true;
            if (!Benchmark(job))
            {
                ai->ResetStrategies();
                Training::JournalReset(member.guid);
            }
        }
        if (Benchmark(job) && member.masterReleased)
        {
            bool const reset = member.finalReset.TryRun([&]
            {
                ai->ResetStrategies(); // Ungrouped, masterless stock strategies, independent of release receipt.
                ai->Reset(false); // Controlled already rejected IsLoggingOut; never cancel a pending logout.
                Training::JournalReset(member.guid);
            });
            if (!reset)
            {
                CleanupFailure(job, member, "stock_reset", true);
                continue; // The disbanded group's other members must still release their masters this pass.
            }
        }
        Revive(player);
        if (!member.moved || AtOrigin(member, player))
        {
            player->SetRaidDifficulty(member.originalDifficulty);
            if (Benchmark(job) && member.guid == job.command.leaderGuid)
            {
                // Group::Create sets this flag; the installed Group::Disband does not undo it.
                if (member.originalLeaderFlag) player->SetPlayerFlag(PLAYER_FLAGS_GROUP_LEADER);
                else player->RemovePlayerFlag(PLAYER_FLAGS_GROUP_LEADER);
            }
            member.finished = true;
            continue;
        }
        if (job.stageElapsed >= CleanupMs && !Benchmark(job))
        {
            CleanupFailure(job, member, "return_timeout");
            continue;
        }
        if (Benchmark(job) && member.returnIssued && job.stageElapsed >= member.returnRetryAt)
            member.returnIssued = false;
        if (!member.returnIssued)
        {
            member.returnIssued = player->TeleportTo(member.origin);
            member.returnRetryAt = AdvanceElapsed(job.stageElapsed, 5000);
        }
    }
    if (!std::all_of(job.members.begin(), job.members.end(), [](Member const& member) { return member.finished; }))
        return false;

    // Keep the exact registration until its saved per-key values have actually been restored.
    if (job.trainingToken)
    {
        if (!Training::ClearTrainingRun(job.command.leaderGuid, job.trainingToken)) return false;
        job.trainingToken = 0;
    }
    if (!RestoreWaitOverride(job)) return false;
    if (job.pinsInstalled)
    {
        auto requested = Requested(job.command);
        auto expected = std::unordered_set<uint32>(requested.begin(), requested.end());
        if (Pins().Owned(kPinOwnerBenchmark) != expected) return false; // Never erase a replacement owner's set.
        LeeroySetExtraPins(kPinOwnerBenchmark, {});
        LeeroyApplyPins();
        job.pinsInstalled = false;
    }
    if (job.rewardToken)
    {
        Training::BenchmarkRewards().Revoke(job.rewardToken);
        job.rewardToken = 0;
    }
    return true;
}

bool RunTeardown(Job& job)
{
    if (job.rewardToken && Training::BenchmarkRewards().Contaminated(job.rewardToken))
    {
        job.terminalState = "failed";
        job.terminalReason = "reward_scope_unproven";
    }
    bool complete = false;
    try { complete = Teardown(job); }
    catch (...)
    {
        job.cleanupFailed = true; // Keep every incomplete receipt and the original cleanup deadline.
        LOG_ERROR("module.leeroy", "leeroy: raid {} cleanup exception; recovery retained", job.event.id);
    }
    auto report = job.terminal.Observe(complete, job.stageElapsed, Benchmark(job));
    if (report != RaidCleanupReport::None)
    {
        auto event = job.event;
        bool const failed = report == RaidCleanupReport::Failed || job.cleanupFailed;
        event.state = failed ? "failed" : job.terminalState;
        event.stage.clear();
        event.reason = failed ? "cleanup_failed:" + job.terminalReason : job.terminalReason;
        LOG_INFO("module.leeroy", "leeroy: {} {} {}: {} ({} kills, {} wipes); recovery retained={}",
            event.kind, event.id, event.state, event.reason, event.kills, event.wipes, !complete);
        PublishJob(event);
    }
    return complete; // A failed terminal never releases a still-owned recovery reservation.
}
#endif
}

LeeroyCommandResult HandleStartOfflineRaid(nlohmann::json const& message)
{
    std::string error;
    auto const cmd = ParseOfflineRaid(message, error);
    if (!cmd)
        return Fail(error);
    return StartRaidJob(message.at("id").get<std::string>(), *cmd, RaidJobMode::Offline);
}

LeeroyCommandResult StartRaidJob([[maybe_unused]] std::string const& id,
    [[maybe_unused]] OfflineRaidCmd const& command, [[maybe_unused]] RaidJobMode mode,
    [[maybe_unused]] std::vector<uint32> const& bosses)
{
#if !LEEROY_HAS_DUNGEON_CLEAR
    return Fail("driver_unavailable:no_dungeon_clear");
#else
    auto const* cmd = &command;
    bool const benchmark = mode == RaidJobMode::Benchmark;
    if (!GuildEnabled())
        return Fail("disabled");
    if (benchmark && (!HumanGuid() || !LeeroyModule::Instance().GuildId())) return Fail("identity_unavailable");
    if (!KeepCast())
        return Fail("keep_cast_off");
    if (!DC_PB_CONFIG(ApplyInstanceStrategies, applyInstanceStrategies))
        return Fail("driver_unavailable:instance_strategies_off");
    if (active)
        return Fail("busy");
    if (benchmark && !Training::TrainingAllowed()) return Fail("training_off");
    if (benchmark && !Training::BenchmarkLootHooksReady()) return Fail("loot_guard_unavailable");
    if (benchmark && (bosses.empty() || bosses.size() > 20)) return Fail("bad field: bosses");
    if (benchmark && !Pins().Owned(kPinOwnerBenchmark).empty()) return Fail("pin_owner_busy");
    if (HumanConnected(HumanGuid()))
        return Fail("human_online");
    for (uint32 low : Requested(*cmd))
    {
        if (Training::DummyTrialOwns(low)) return Fail("busy:" + std::to_string(low));
        if (benchmark)
        {
            if (IsCast(low)) return Fail("cast:" + std::to_string(low));
            if (!Training::RivalAvailable(FindOnline(low), 80)) return Fail("not_rival:" + std::to_string(low));
            if (!Training::RivalBindingsSafe(FindOnline(low), cmd->mapId)) return Fail("foreign_bind:" + std::to_string(low));
        }
        else if (low == HumanGuid() || !IsCast(low)) return Fail("not_cast:" + std::to_string(low));
    }
    if (auto const conflict = InstanceConflict(*cmd); !conflict.empty())
        return Fail(conflict);
    Player* leader = FindOnline(cmd->leaderGuid);
    if (!leader || (!benchmark && !Controlled(leader)) || !PlayerbotAI::IsTank(leader, true))
        return Fail("leader_not_tank");
    if (auto const reason = benchmark ? std::string{} : Unusable(leader); !reason.empty())
        return Fail("leader_unavailable:" + reason);
    if (benchmark)
        for (uint32 low : Requested(*cmd))
            if (FindOnline(low)->GetTeamId() != leader->GetTeamId()) return Fail("faction:" + std::to_string(low));
    MapEntry const* map = sMapStore.LookupEntry(cmd->mapId);
    Difficulty const difficulty = Difficulty(RaidDifficulty(cmd->size, cmd->heroic));
    if (!map || !map->IsRaid())
        return Fail("bad field: map_id");
    if (!GetMapDifficultyData(cmd->mapId, difficulty))
        return Fail("no_difficulty");
    AreaTriggerTeleport const* entrance = sObjectMgr->GetMapEntranceTrigger(cmd->mapId);
    if (!entrance || entrance->target_mapId != cmd->mapId || !MapMgr::IsValidMapCoord(entrance->target_mapId,
        entrance->target_X, entrance->target_Y, entrance->target_Z, entrance->target_Orientation))
        return Fail("no_entrance");

    std::string why;
    DcTestDriver::Readiness const readiness = DcTestDriver::Ensure(&why);
    if (readiness == DcTestDriver::Readiness::PendingLogin)
        return Fail("driver_pending");
    if (readiness == DcTestDriver::Readiness::Unavailable)
        return Fail("driver_unavailable:" + why);
    Player* driver = DcTestDriver::Get();
    if (!driver || driver->GetGUID().GetCounter() == HumanGuid() || IsCast(driver->GetGUID().GetCounter()))
        return Fail("driver_unavailable:driver_not_separate");
    for (uint32 low : Requested(*cmd))
        if (low == driver->GetGUID().GetCounter()) return Fail("driver_unavailable:driver_is_member");

    auto job = std::make_unique<Job>();
    job->command = *cmd;
    job->mode = mode;
    job->bosses = bosses;
    job->entrance = *entrance;
    job->difficulty = difficulty;
    job->human = HumanGuid();
    job->guild = LeeroyModule::Instance().GuildId();
    job->team = static_cast<uint8>(leader->GetTeamId());
    job->driver = driver->GetGUID();
    job->driverBody = Lease(driver);
    if (benchmark)
        for (uint32 low : Requested(*cmd)) job->bodies.emplace(low, Lease(FindOnline(low)));
    job->limits = {cmd->maxWipes, cmd->maxWipesPerBoss, cmd->timeoutMin * 60000, cmd->minMembers, 8};
    job->event.id = id;
    job->event.kind = benchmark ? "benchmark" : "offline_raid";
    active = std::move(job);
    // No stage side effect or progress event precedes this ack. Driver-pending retries remain caller-owned.
    return {true, {}};
#endif
}

void TickOfflineRaid([[maybe_unused]] uint32 diffMs)
{
#if LEEROY_HAS_DUNGEON_CLEAR
    if (!active)
        return;
    Job& job = *active;
    job.elapsed = AdvanceElapsed(job.elapsed, diffMs);
    job.stageElapsed = AdvanceElapsed(job.stageElapsed, diffMs);
    if (job.stage != Stage::TearingDown)
    {
        auto reason = OfflineRaidStopReason(HumanConnected(job.human) || HumanConnected(HumanGuid()),
            GuildEnabled(), KeepCast(), Driver(job) != nullptr, job.elapsed, job.limits.timeoutMs,
            DC_PB_CONFIG(ApplyInstanceStrategies, applyInstanceStrategies));
        if (reason.empty() && job.human != HumanGuid())
            reason = "human_changed";
        if (reason.empty() && Benchmark(job) && job.guild != LeeroyModule::Instance().GuildId())
            reason = "guild_changed";
        if (reason.empty() && Benchmark(job) && !Training::TrainingAllowed()) reason = "training_off";
        if (job.rewardToken && Training::BenchmarkRewards().Contaminated(job.rewardToken))
            reason = "reward_scope_unproven";
        if (reason.empty() && Benchmark(job) && job.pinsInstalled)
        {
            auto requested = Requested(job.command);
            if (Pins().Owned(kPinOwnerBenchmark) != std::unordered_set<uint32>(requested.begin(), requested.end()))
                reason = "pin_owner_changed";
            for (uint32 guid : Requested(job.command))
                if (!sRandomPlayerbotMgr.IsPinned(guid)) { reason = "pin_lost"; break; }
        }
        if (reason.empty() && Benchmark(job) && job.event.instanceId && job.stage != Stage::Running)
            if (auto* leader = FindOnline(job.command.leaderGuid); Present(job, leader))
                if (auto* instance = leader->GetInstanceScript(); instance && instance->GetEncounterCount() <= 1024)
                {
                    std::vector<uint8> states;
                    for (uint32 boss = 0; boss < instance->GetEncounterCount(); ++boss)
                        states.push_back(static_cast<uint8>(instance->GetBossState(boss)));
                    if (Training::FrontierReached(states, job.bosses)) reason = "frontier";
                }
        if (!reason.empty())
            Stop(job, reason, reason == "instance_strategies_off" || reason == "reward_scope_unproven");
    }
    if (!job.announced)
        Progress(job);
    bool const alreadyTearingDown = job.stage == Stage::TearingDown;
    switch (job.stage)
    {
        case Stage::Grouping: Grouping(job); break;
        case Stage::Teleporting: Teleporting(job); break;
        case Stage::Starting: Starting(job); break;
        case Stage::Running: Running(job, diffMs); break;
        case Stage::Restarting: Restarting(job); break;
        case Stage::TearingDown:
            if (RunTeardown(job))
                active.reset();
            break;
    }
    // A frontier is observed AFTER this map update. Stop/return now, without permitting another planned map tick.
    // Never access job after Teardown has released active in the switch above.
    if (!alreadyTearingDown && active && active->stage == Stage::TearingDown && Benchmark(*active))
        if (RunTeardown(*active)) active.reset();
#endif
}

bool OfflineRaidOwns([[maybe_unused]] uint32 guid)
{
#if LEEROY_HAS_DUNGEON_CLEAR
    return active && (active->command.leaderGuid == guid
        || std::find(active->command.members.begin(), active->command.members.end(), guid) != active->command.members.end());
#else
    return false;
#endif
}

bool BenchmarkRunning()
{
#if LEEROY_HAS_DUNGEON_CLEAR
    return active && Benchmark(*active);
#else
    return false;
#endif
}

bool BenchmarkOwnsRun([[maybe_unused]] uint32 leader)
{
#if LEEROY_HAS_DUNGEON_CLEAR
    if (!active || !Benchmark(*active) || active->command.leaderGuid != leader || !active->event.instanceId
        || active->stage == Stage::TearingDown
        || active->human != HumanGuid() || active->guild != LeeroyModule::Instance().GuildId()
        || !Driver(*active) || !OwnsGroup(*active, FindGroup(*active))) return false;
    auto* player = FindOnline(leader);
    if (!Present(*active, player) || player->GetGroup()->GetLeaderGUID() != Guid(leader)) return false;
    for (auto const& member : active->members)
    {
        auto* bot = FindOnline(member.guid);
        if (!InJobGroup(*active, bot) || !GET_PLAYERBOT_AI(bot)->GetMaster()
            || Identity(GET_PLAYERBOT_AI(bot)->GetMaster()) != active->driverBody.player
            || Training::DummyTrialOwns(member.guid)) return false;
    }
    return true;
#else
    return false;
#endif
}

bool BenchmarkCanClearRun([[maybe_unused]] uint32 leader, [[maybe_unused]] uint64 token)
{
#if LEEROY_HAS_DUNGEON_CLEAR
    if (!active || !Benchmark(*active) || active->command.leaderGuid != leader
        || !token || active->trainingToken != token) return false;
    auto member = std::find_if(active->members.begin(), active->members.end(),
        [leader](Member const& candidate) { return candidate.guid == leader; });
    auto* player = FindOnline(leader);
    if (member == active->members.end() || !Controlled(*active, player)
        || !BenchmarkRecoveryLocation(*active, *member, player)
        || (player->GetGroup() && player->GetGroup()->GetGUID() != active->group)) return false;
    auto master = Identity(GET_PLAYERBOT_AI(player)->GetMaster());
    return member->masterReleased ? master == 0 : master == active->driverBody.player;
#else
    return false;
#endif
}

void EndBenchmark([[maybe_unused]] std::string const& reason)
{
#if LEEROY_HAS_DUNGEON_CLEAR
    if (!active || !Benchmark(*active)) return;
    Stop(*active, reason);
    // May be called by the training-off hook after the guild ticker already ran this frame.
    if (RunTeardown(*active)) active.reset();
#endif
}

void BenchmarkPlayerLogout([[maybe_unused]] uint32 guid)
{
#if LEEROY_HAS_DUNGEON_CLEAR
    if (!active || !Benchmark(*active)) return;
    bool owned = false;
    if (auto body = active->bodies.find(guid); body != active->bodies.end())
    {
        body->second.Revoke();
        owned = true;
    }
    if (guid == active->driver.GetCounter()) { active->driverBody.Revoke(); owned = true; }
    if (owned)
    {
        if (active->rewardToken) Training::BenchmarkRewards().Revoke(active->rewardToken);
        Stop(*active, "participant_logout", true);
    }
    // The lifecycle hook only revokes ownership. The next world tick performs bounded cleanup.
#endif
}

void BenchmarkPlayerLogin([[maybe_unused]] uint32 guid)
{
#if LEEROY_HAS_DUNGEON_CLEAR
    if (active && Benchmark(*active) && (guid == active->human || guid == HumanGuid()))
        Stop(*active, "human_online"); // Latch even if the human logs out before the next world update.
#endif
}

void SweepLeftoverGroups()
{
#if LEEROY_HAS_DUNGEON_CLEAR
    if (active || !GuildEnabled() || !KeepCast())
        return;
    std::set<ObjectGuid> visited;
    for (uint32 low : CastMembers())
    {
        Player* player = FindOnline(low);
        Group* group = player ? player->GetGroup() : nullptr;
        if (!group || !group->isRaidGroup() || group->isLFGGroup() || group->isBGGroup() || group->isBFGroup()
            || !visited.insert(group->GetGUID()).second || !IsCast(group->GetLeaderGUID().GetCounter()))
            continue;
        bool owned = true;
        for (auto const& slot : group->GetMemberSlots())
        {
            if (slot.guid.GetCounter() == HumanGuid() || !IsCast(slot.guid.GetCounter())
                || DcTestRunManager::Instance().IsReserved(slot.guid))
                owned = false;
            if (Player* member = FindOnline(slot.guid.GetCounter()))
                if (PlayerbotAI* ai = GET_PLAYERBOT_AI(member); ai && DcRun::Of(ai).enabled)
                    owned = false; // An actively driven raid is not a leftover group from a server restart.
        }
        if (!owned)
            continue;
        ObjectGuid const leader = group->GetLeaderGUID();
        group->Disband();
        LOG_INFO("module.leeroy", "leeroy: disbanded leftover cast raid led by guid {}", leader.GetCounter());
    }
#endif
}

void RegisterOfflineRaidHandler() { RegisterCommand("start_offline_raid", HandleStartOfflineRaid); }
}
