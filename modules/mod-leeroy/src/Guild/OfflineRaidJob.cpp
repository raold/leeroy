// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "OfflineRaidJob.h"
#include "GuildPorts.h"
#include "LeeroyOfflineRaidPolicy.h"

#if !defined(LEEROY_HAS_DUNGEON_CLEAR)
#error "LEEROY_HAS_DUNGEON_CLEAR comes from mod-leeroy.cmake; re-run cmake"
#endif

#if LEEROY_HAS_DUNGEON_CLEAR
#include "AiObjectContext.h"
#include "DBCStores.h"
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
    bool changed = false;
    bool masterAssigned = false;
    bool moved = false;
    bool restartIssued = false;
    bool returnIssued = false;
    bool finished = false;
};

struct Job
{
    OfflineRaidCmd command;
    OfflineRaidLimits limits{};
    JobEvent event;
    AreaTriggerTeleport entrance;
    Difficulty difficulty = RAID_DIFFICULTY_10MAN_NORMAL;
    ObjectGuid group, driver;
    uint32 human = 0;
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

ObjectGuid Guid(uint32 low) { return ObjectGuid::Create<HighGuid::Player>(low); }

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

Group* FindGroup(Job const& job)
{
    return job.group ? sGroupMgr->GetGroupByGUID(job.group.GetCounter()) : nullptr;
}

bool OwnsGroup(Job const& job, Group const* group)
{
    if (!group || group->GetGUID() != job.group || !group->isRaidGroup()
        || group->isLFGGroup() || group->isBGGroup() || group->isBFGroup())
        return false;
    for (auto const& slot : group->GetMemberSlots())
    {
        if (!IsCast(slot.guid.GetCounter()) || !Contains(job, slot.guid.GetCounter())
            || DcTestRunManager::Instance().IsReserved(slot.guid))
            return false;
        auto const member = std::find_if(job.members.begin(), job.members.end(), [&slot](Member const& candidate)
        {
            return candidate.guid == slot.guid.GetCounter();
        });
        if (Player* player = FindOnline(slot.guid.GetCounter()); player && member->masterAssigned)
        {
            PlayerbotAI* ai = GET_PLAYERBOT_AI(player);
            if (!ai || IsSelfBot(player) || (ai->GetMaster() && ai->GetMaster()->GetGUID() != job.driver))
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
    return Controlled(player) && player->GetGroup() && player->GetGroup()->GetGUID() == job.group;
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
    Enter(job, Stage::TearingDown);
}

void SetupFailure(Job& job, std::string reason)
{
    Stop(job, "setup:" + std::string(Name(job.stage)) + ":" + reason, true);
}

void Progress(Job& job)
{
    job.event.state = "progress";
    job.event.stage = Name(job.stage);
    PublishJob(job.event);
    job.announced = true;
}

Player* Driver(Job const& job)
{
    Player* driver = DcTestDriver::Get();
    return driver && driver->GetGUID() == job.driver ? driver : nullptr;
}

void ApplyRaidHabits(Player* player, Player* driver)
{
    PlayerbotAI* ai = GET_PLAYERBOT_AI(player);
    ai->SetMaster(driver);
    ai->ResetStrategies();
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
    // Training-ground Part A will journal-reset here once its journal exists, using these habits as the baseline.
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

    // Complete the usable-roster preflight before removing anyone from an existing group.
    for (uint32 low : Requested(job.command))
    {
        Player* player = FindOnline(low);
        if (auto const reason = Unusable(player); !reason.empty())
        {
            job.event.skipped.emplace_back(low, reason);
            continue;
        }
        job.members.push_back({low, WorldLocation(player->GetMapId(), player->GetPosition()),
                               player->GetRaidDifficulty()});
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
    group->SetLootThreshold(ITEM_QUALITY_EPIC);
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
        ApplyRaidHabits(FindOnline(member.guid), driver);
        member.masterAssigned = true;
    }
    group->SendUpdate();
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
        && player->GetMapId() == member.origin.GetMapId()
        && player->GetDistance2d(member.origin.GetPositionX(), member.origin.GetPositionY()) <= 5.0f
        && std::abs(player->GetPositionZ() - member.origin.GetPositionZ()) <= 5.0f;
}

void CleanupFailure(Job& job, Member& member, std::string reason)
{
    job.cleanupFailed = true;
    member.finished = true;
    job.event.skipped.emplace_back(member.guid, "cleanup:" + reason);
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

bool Teardown(Job& job)
{
    // Cancellation can arrive between the leader's teleport request and its acknowledgement. Bind the cleanup
    // to the returned leader's actual instance only while the group we created still owns that participant.
    if (!job.event.instanceId && job.leaderSent)
        if (Player* leader = FindOnline(job.command.leaderGuid); InJobGroup(job, leader)
            && !leader->IsBeingTeleported() && leader->GetMapId() == job.command.mapId)
            job.event.instanceId = leader->GetInstanceId();
    DisableOwnedRun(job);
    bool pendingTransfer = false;
    for (Member const& member : job.members)
        if (Player* player = ObjectAccessor::FindConnectedPlayer(Guid(member.guid)); member.changed && player)
            pendingTransfer = pendingTransfer || player->IsBeingTeleported();

    Group* group = FindGroup(job);
    if (!pendingTransfer && OwnsGroup(job, group))
        group->Disband(); // Disband deletes the Group; no pointer to it is used below.

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
        if (!Controlled(player) || (player->GetGroup() && player->GetGroup()->GetGUID() != job.group))
        {
            CleanupFailure(job, member, "ownership_changed");
            continue;
        }
        PlayerbotAI* ai = GET_PLAYERBOT_AI(player);
        if (member.masterAssigned && ai->GetMaster() && ai->GetMaster()->GetGUID() != job.driver)
        {
            CleanupFailure(job, member, "master_changed");
            continue;
        }
        bool const inInstance = job.event.instanceId && player->GetMapId() == job.command.mapId
            && player->GetInstanceId() == job.event.instanceId;
        bool const recoveringGhost = (!player->IsAlive() || player->HasPlayerFlag(PLAYER_FLAGS_GHOST))
            && ((player->GetGroup() && player->GetGroup()->GetGUID() == job.group)
                || (member.masterAssigned && ai->GetMaster() && ai->GetMaster()->GetGUID() == job.driver));
        if (member.moved && !member.returnIssued && !AtOrigin(member, player) && !inInstance && !recoveringGhost)
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
            && ai->GetMaster() && ai->GetMaster()->GetGUID() == job.driver)
        {
            // After leaving a now-mixed group this can only affect the original leader's own run state.
            DcActionShared::DisableDungeonClear(ai, "Offline raid ended.");
            job.dcIssued = false;
        }
        if (ai->GetMaster() && ai->GetMaster()->GetGUID() == job.driver)
        {
            ai->SetMaster(nullptr);
            ai->ResetStrategies();
        }
        Revive(player);
        if (!member.moved || AtOrigin(member, player))
        {
            player->SetRaidDifficulty(member.originalDifficulty);
            member.finished = true;
            continue;
        }
        if (job.stageElapsed >= CleanupMs)
        {
            CleanupFailure(job, member, "return_timeout");
            continue;
        }
        if (!member.returnIssued)
            member.returnIssued = player->TeleportTo(member.origin);
    }
    if (!std::all_of(job.members.begin(), job.members.end(), [](Member const& member) { return member.finished; }))
        return false;

    // Cast pins remain owned by P1a's one pin composer. This job never installs a second pin set or logs bots out.
    if (job.waitOverrideInstalled)
    {
        ObjectGuid const owner = Guid(job.command.leaderGuid);
        // Restore only the key this job changed, and preserve a later explicit value or removal. Other run-local
        // settings (including future training-ground overrides) are never cleared by this job.
        if (DcSettings::HasOverride(owner, "WaitAtBoss") && !DcSettings::GetBool(owner, "WaitAtBoss"))
        {
            if (job.previousWaitOverride)
                DcSettings::SetOverride(owner, "WaitAtBoss", job.previousWaitValue ? 1.0 : 0.0);
            else
                DcSettings::ResetOverride(owner, "WaitAtBoss");
        }
        job.waitOverrideInstalled = false;
    }
    job.event.state = job.cleanupFailed ? "failed" : job.terminalState;
    job.event.stage.clear();
    job.event.reason = job.cleanupFailed ? "cleanup_failed:" + job.terminalReason : job.terminalReason;
    LOG_INFO("module.leeroy", "leeroy: offline raid {} {}: {} ({} kills, {} wipes)",
        job.event.id, job.event.state, job.event.reason, job.event.kills, job.event.wipes);
    PublishJob(job.event);
    return true;
}
#endif
}

LeeroyCommandResult HandleStartOfflineRaid(nlohmann::json const& message)
{
    std::string error;
    auto const cmd = ParseOfflineRaid(message, error);
    if (!cmd)
        return Fail(error);
#if !LEEROY_HAS_DUNGEON_CLEAR
    return Fail("driver_unavailable:no_dungeon_clear");
#else
    if (!GuildEnabled())
        return Fail("disabled");
    if (!KeepCast())
        return Fail("keep_cast_off");
    if (!DC_PB_CONFIG(ApplyInstanceStrategies, applyInstanceStrategies))
        return Fail("driver_unavailable:instance_strategies_off");
    if (active)
        return Fail("busy");
    if (HumanConnected(HumanGuid()))
        return Fail("human_online");
    for (uint32 low : Requested(*cmd))
        if (low == HumanGuid() || !IsCast(low))
            return Fail("not_cast:" + std::to_string(low));
    if (auto const conflict = InstanceConflict(*cmd); !conflict.empty())
        return Fail(conflict);
    Player* leader = FindOnline(cmd->leaderGuid);
    if (!leader || !Controlled(leader) || !PlayerbotAI::IsTank(leader, true))
        return Fail("leader_not_tank");
    if (auto const reason = Unusable(leader); !reason.empty())
        return Fail("leader_unavailable:" + reason);
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

    active = std::make_unique<Job>();
    active->command = *cmd;
    active->entrance = *entrance;
    active->difficulty = difficulty;
    active->human = HumanGuid();
    active->driver = driver->GetGUID();
    active->limits = {cmd->maxWipes, cmd->maxWipesPerBoss, cmd->timeoutMin * 60000, cmd->minMembers, 8};
    active->event.id = message.at("id").get<std::string>();
    active->event.kind = "offline_raid";
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
        if (!reason.empty())
            Stop(job, reason, reason == "instance_strategies_off");
    }
    if (!job.announced)
        Progress(job);
    switch (job.stage)
    {
        case Stage::Grouping: Grouping(job); break;
        case Stage::Teleporting: Teleporting(job); break;
        case Stage::Starting: Starting(job); break;
        case Stage::Running: Running(job, diffMs); break;
        case Stage::Restarting: Restarting(job); break;
        case Stage::TearingDown:
            if (Teardown(job))
                active.reset();
            break;
    }
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

bool OfflineRaidOwns(std::uint32_t guid)
{
#if LEEROY_HAS_DUNGEON_CLEAR
    return active && (active->command.leaderGuid == guid
        || std::find(active->command.members.begin(), active->command.members.end(), guid)
            != active->command.members.end());
#else
    (void)guid;
    return false;
#endif
}

void RegisterOfflineRaidHandler() { RegisterCommand("start_offline_raid", HandleStartOfflineRaid); }
}
