// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyOfflineRaidPolicy.h"

#include <algorithm>
#include <limits>

namespace Leeroy::Guild
{
std::string OfflineStopError(bool connected, bool owned, bool finished, bool tearingDown,
    bool stopped, std::optional<bool> bossEngaged)
{
    if (!connected) return "mind_disconnected";
    if (finished) return "raid_finished";
    if (!owned) return "unknown_raid";
    if (tearingDown) return stopped ? "" : "raid_finishing";
    if (!bossEngaged.has_value()) return "encounters_unavailable";
    if (*bossEngaged) return "boss_engaged";
    return {};
}

std::string DcRosterRefusal(std::size_t bosses)
{
    return bosses ? std::string{} : std::string(OfflineNoDcBosses);
}

DcPhase ReadDcPhase(bool leaderPresent, bool enabled, bool paused, bool pausedAtDoor)
{
    if (!leaderPresent) return DcPhase::Unknown;
    if (!enabled) return DcPhase::Off;
    if (!paused) return DcPhase::On;
    return pausedAtDoor ? DcPhase::Door : DcPhase::Paused;
}

char const* DcPhaseCode(DcPhase phase)
{
    switch (phase)
    {
        case DcPhase::Off: return "off";
        case DcPhase::On: return "on";
        case DcPhase::Paused: return "paused";
        case DcPhase::Door: return "door";
        case DcPhase::Unknown: break;
    }
    return "unknown";
}

std::string DcHold::Observe(DcPhase phase, std::optional<bool> bossEngaged, std::uint32_t diffMs)
{
    bool const held = (phase == DcPhase::Door || phase == DcPhase::Paused) && bossEngaged == false;
    heldMs = held ? AdvanceElapsed(heldMs, diffMs) : 0;
    if (heldMs < OfflineDcHoldStopMs) return {};
    return phase == DcPhase::Door ? "dc_door_blocked" : "dc_paused";
}

bool BossProgress(std::uint8_t before, std::uint8_t after)
{
    return before != after && (after == 1 || after == 3 || IsWipe(before, after));
}

RecallAction OfflineRecall::Observe(bool inside, bool safe, std::uint32_t diffMs)
{
    sinceAttemptMs = AdvanceElapsed(sinceAttemptMs, diffMs);
    if (inside)
    {
        absentMs = 0;
        bool const arrived = pending;
        pending = false;
        return arrived ? RecallAction::Arrived : RecallAction::Wait;
    }
    absentMs = AdvanceElapsed(absentMs, diffMs);
    if (!safe || absentMs < OfflineRecallGraceMs || attempts >= OfflineRecallAttempts
        || (attempts && sinceAttemptMs < OfflineRecallRetryMs)) return RecallAction::Wait;
    ++attempts; // A refused teleport costs an attempt too.
    sinceAttemptMs = 0;
    return RecallAction::Summon;
}

bool RecallQuietOk(RecallQuiet const& q)
{
    return q.encounterKnown && !q.bossEngaged && q.driver && !q.realmGroup && q.leaderAi && q.leaderMasterIsDriver
        && q.leaderAlive && !q.leaderGhost && !q.leaderCombat && !q.leaderTransport && !q.leaderVehicle
        && !q.memberCombat;
}

bool RecallBodySafe(RecallBody const& b)
{
    return b.inWorld && !b.teleporting && b.alive && !b.ghost && !b.combat && !b.flight && !b.transport
        && !b.vehicle && !b.pendingBind && !b.inDungeon && b.difficultyMatches && b.destinationMatches;
}

bool RecallPvpBusy(RecallPvp const& p)
{
    return p.duel || p.battleground || p.battlegroundQueue || p.arenaObserved
        || !Realm::WintergraspPrivateReason(p.wintergrasp).empty();
}

bool ReassertDriverMaster(MasterFacts const& f)
{
    return f.driver && f.assigned && !f.released && f.sameBody && f.inJobGroup && !f.foreignLease
        && !f.masterIsDriver && f.masterEmpty;
}

RestartRoster DecideRestartRoster(std::uint32_t stageElapsedMs, std::uint32_t absent, std::uint32_t present,
    bool leaderPresent, std::uint32_t minMembers)
{
    if (!absent || stageElapsedMs < OfflineRestartAbsentMs || !leaderPresent) return RestartRoster::Wait;
    return present >= minMembers ? RestartRoster::DropAbsent : RestartRoster::TooFew;
}

bool AbandonLostMember(bool benchmark, bool lost, bool trainingJournal, std::uint32_t stageElapsedMs)
{
    return !benchmark && lost && !trainingJournal && stageElapsedMs >= OfflineCleanupFailMs;
}

SlotRelease DecideSlotRelease(bool benchmark, bool failedReported, std::uint32_t sinceReportMs,
    std::uint32_t heldMembers)
{
    if (benchmark || !failedReported) return SlotRelease::Keep;
    if (sinceReportMs < OfflineSlotReleaseMs) return SlotRelease::Wait;
    return heldMembers ? SlotRelease::Keep : SlotRelease::Release;
}

Decision Decide(OfflineRaidState const& state, OfflineRaidLimits const& limits)
{
    if (state.humanOnline)
        return {Verdict::Done, "human_login"};
    if (state.elapsedMs >= limits.timeoutMs)
        return {Verdict::Done, "timeout"};
    if (!state.leaderPresent)
        return {Verdict::Done, "leader_lost"};
    if (state.membersPresent < limits.minMembers)
        return {Verdict::Done, "too_few"};
    if (state.maxWipesOnOneBoss >= limits.maxWipesPerBoss)
        return {Verdict::Done, "boss_wipe_cap"};
    if (state.wipes >= limits.maxWipes)
        return {Verdict::Done, "wipe_cap"};
    if (state.bossesTotal && state.bossesDone == state.bossesTotal)
        return {Verdict::Done, "cleared"};
    if (!state.dcEnabled && state.dcOffMs >= 30000)
        return state.restarts >= limits.maxRestarts ? Decision{Verdict::Done, "dc_stalled"}
                                                    : Decision{Verdict::Restart, "dc_stopped"};
    return {Verdict::Continue, {}};
}

bool IsWipe(std::uint8_t oldState, std::uint8_t newState)
{
    return oldState == 1 && (newState == 0 || newState == 2);
}

bool FreshEncounters(std::vector<std::uint8_t> const& bossStates, std::uint32_t completedEncounterMask)
{
    return !bossStates.empty() && completedEncounterMask == 0
        && std::none_of(bossStates.begin(), bossStates.end(), [](std::uint8_t state) { return state == 3; });
}

std::uint32_t AdvanceElapsed(std::uint32_t elapsed, std::uint32_t diff)
{
    auto const maximum = std::numeric_limits<std::uint32_t>::max();
    return diff > maximum - elapsed ? maximum : elapsed + diff;
}

std::string OfflineRaidStopReason(bool humanOnline, bool moduleEnabled, bool keepCast,
    bool driverPresent, std::uint32_t elapsedMs, std::uint32_t timeoutMs, bool instanceStrategies)
{
    if (humanOnline)
        return "human_login";
    if (elapsedMs >= timeoutMs)
        return "timeout";
    if (!moduleEnabled)
        return "module_disabled";
    if (!keepCast)
        return "keep_cast_off";
    if (!driverPresent)
        return "driver_lost";
    if (!instanceStrategies)
        return "instance_strategies_off";
    return {};
}
}
