// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyOfflineRaidPolicy.h"
#include "LeeroyGuildCommands.h"
#include "LeeroyRealmCleanup.h"
#include "TestHarness.h"

#include <limits>

using namespace Leeroy::Guild;

LEEROY_TEST(OfflineRaid_StopRequiresConnectedMindAndOwnedLiveJob)
{
    CHECK(OfflineStopError(true, true, false, false, false, false).empty());
    CHECK_EQ(OfflineStopError(false, true, false, false, false, false), "mind_disconnected");
    CHECK_EQ(OfflineStopError(true, false, false, false, false, false), "unknown_raid");
    CHECK_EQ(OfflineStopError(true, false, true, false, false, false), "raid_finished");
    CHECK_EQ(OfflineStopError(true, true, false, false, false, true), "boss_engaged");
    CHECK_EQ(OfflineStopError(true, true, false, false, false, std::nullopt), "encounters_unavailable");
}

LEEROY_TEST(OfflineRaid_StopIsIdempotentDuringOwnedTeardown)
{
    CHECK(OfflineStopError(true, true, false, true, true, std::nullopt).empty());
    CHECK_EQ(OfflineStopError(true, true, false, true, false, false), "raid_finishing");
    CHECK_EQ(OfflineStopError(true, false, true, true, true, false), "raid_finished");
}

LEEROY_TEST(OfflineRaid_ProgressIsEngageKillOrWipeNotLoadedGridOrSameState)
{
    CHECK(BossProgress(0, 1)); CHECK(BossProgress(1, 3)); CHECK(BossProgress(1, 2));
    CHECK(BossProgress(1, 0)); CHECK(!BossProgress(5, 0)); CHECK(!BossProgress(1, 1));
    CHECK(!BossProgress(0, 0)); CHECK(!BossProgress(3, 3));
}

LEEROY_TEST(OfflineRaid_StopTargetIsBoundedAndTyped)
{
    std::string error;
    CHECK_EQ(*ParseOfflineRaidStop({{"raid_id", "raid1"}}, error), "raid1");
    CHECK(error.empty());
    for (auto value : {nlohmann::json(nullptr), nlohmann::json(true), nlohmann::json(3),
        nlohmann::json(""), nlohmann::json(std::string(129, 'a')), nlohmann::json("bad\nid")})
    {
        CHECK(!ParseOfflineRaidStop({{"raid_id", value}}, error));
        CHECK_EQ(error, "bad field: raid_id");
    }
    CHECK(!ParseOfflineRaidStop(nlohmann::json::object(), error));
    CHECK(ParseOfflineRaidStop({{"raid_id", std::string(128, 'a')}}, error).has_value());
}

LEEROY_TEST(OfflineRaid_ProgressFieldsAreAdditiveAndPreserveZeroAndFalse)
{
    JobEvent e;
    CHECK(!MakeJobEvent(e).contains("boss_engaged"));
    e.membersInInstance = 0; e.lastProgressTs = 123; e.bossEngaged = false;
    auto j = MakeJobEvent(e);
    CHECK_EQ(j.at("members_in_instance"), 0);
    CHECK_EQ(j.at("last_progress_ts"), 123);
    CHECK_EQ(j.at("boss_engaged"), false);
}

namespace
{
OfflineRaidLimits const limits{6, 3, 180 * 60000, 8, 8};
OfflineRaidState Base() { return {0, false, true, 10, 0, 0, 0, 12, true, 0, 0}; }
}

LEEROY_TEST(OfflineRaid_HumanOnlineBeatsEverything)
{
    auto state = Base();
    state.humanOnline = true;
    state.elapsedMs = 1000000000;
    state.wipes = 99;
    state.leaderPresent = false;
    CHECK(Decide(state, limits).verdict == Verdict::Done);
    CHECK_EQ(Decide(state, limits).reason, "human_login");
}

LEEROY_TEST(OfflineRaid_Order)
{
    auto state = Base();
    state.elapsedMs = limits.timeoutMs - 1;
    CHECK(Decide(state, limits).verdict == Verdict::Continue);
    state.elapsedMs = limits.timeoutMs;
    CHECK_EQ(Decide(state, limits).reason, "timeout");
    state = Base();
    state.leaderPresent = false;
    state.membersPresent = 7;
    CHECK_EQ(Decide(state, limits).reason, "leader_lost");
    state.leaderPresent = true;
    state.wipes = 6;
    state.maxWipesOnOneBoss = 3;
    CHECK_EQ(Decide(state, limits).reason, "too_few");
    state.membersPresent = 8;
    CHECK_EQ(Decide(state, limits).reason, "boss_wipe_cap");
    state.maxWipesOnOneBoss = 2;
    CHECK_EQ(Decide(state, limits).reason, "wipe_cap");
    state.wipes = 5;
    state.bossesDone = 12;
    CHECK_EQ(Decide(state, limits).reason, "cleared");
}

LEEROY_TEST(OfflineRaid_EmptyOrInconsistentBossRosterNeverClaimsCleared)
{
    auto state = Base();
    state.bossesTotal = 0;
    CHECK(Decide(state, limits).verdict == Verdict::Continue);
    state.bossesDone = 1;
    CHECK(Decide(state, limits).verdict == Verdict::Continue);
    state.bossesTotal = 1;
    state.bossesDone = 2;
    CHECK(Decide(state, limits).verdict == Verdict::Continue);
}

LEEROY_TEST(OfflineRaid_DcStopped)
{
    auto state = Base();
    state.dcEnabled = false;
    state.dcOffMs = 29999;
    CHECK(Decide(state, limits).verdict == Verdict::Continue);
    state.dcOffMs = 30000;
    CHECK(Decide(state, limits).verdict == Verdict::Restart);
    CHECK_EQ(Decide(state, limits).reason, "dc_stopped");
    state.restarts = 8;
    CHECK(Decide(state, limits).verdict == Verdict::Done);
    CHECK_EQ(Decide(state, limits).reason, "dc_stalled");
    state.dcEnabled = true;
    CHECK(Decide(state, limits).verdict == Verdict::Continue);
}

LEEROY_TEST(OfflineRaid_ClearedWinsOverRestartButNeverOverWipeCap)
{
    auto state = Base();
    state.bossesDone = state.bossesTotal;
    state.dcEnabled = false;
    state.dcOffMs = 30000;
    state.restarts = 8;
    CHECK_EQ(Decide(state, limits).reason, "cleared");
    state.wipes = 6;
    CHECK_EQ(Decide(state, limits).reason, "wipe_cap");
}

LEEROY_TEST(OfflineRaid_IsWipe)
{
    CHECK(IsWipe(1, 0));
    CHECK(IsWipe(1, 2));
    CHECK(!IsWipe(1, 3));
    CHECK(!IsWipe(0, 0));
    CHECK(!IsWipe(3, 0));
    CHECK(!IsWipe(2, 0));
    CHECK(!IsWipe(1, 1));
    CHECK(!IsWipe(5, 2));
}

LEEROY_TEST(OfflineRaid_FreshCopyAcceptsBossesWhoseGridsHaveNotLoaded)
{
    // NOT_STARTED 0, IN_PROGRESS 1, FAIL 2, DONE 3, SPECIAL 4, TO_BE_DECIDED 5. A new ICC copy entered at
    // the door reads Marrowgar as 0 and every later boss as 5 until its grid loads.
    CHECK(FreshEncounters({0, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5}, 0));
    CHECK(FreshEncounters({5, 5, 5, 5, 5}, 0));
    CHECK(FreshEncounters({0, 0, 0}, 0));
    CHECK(!FreshEncounters({0, 3, 5}, 0));
    CHECK(!FreshEncounters({5, 5, 3}, 0));
    CHECK(!FreshEncounters({0, 5, 5}, 1u << 2));  // A saved copy's credit counts even before its boss loads.
    CHECK(!FreshEncounters({}, 0));
}

LEEROY_TEST(OfflineRaid_TimersSaturateInsteadOfExtendingTheRunAfterOverflow)
{
    auto const maximum = std::numeric_limits<std::uint32_t>::max();
    CHECK_EQ(AdvanceElapsed(100, 20), 120u);
    CHECK_EQ(AdvanceElapsed(maximum - 5, 5), maximum);
    CHECK_EQ(AdvanceElapsed(maximum - 5, 6), maximum);
    CHECK_EQ(AdvanceElapsed(maximum, maximum), maximum);
    auto state = Base();
    state.elapsedMs = AdvanceElapsed(maximum - 5, 6);
    CHECK_EQ(Decide(state, limits).reason, "timeout");
}

LEEROY_TEST(OfflineRaid_RuntimeDisableAndDriverLossStopSetupAndRestarts)
{
    CHECK(OfflineRaidStopReason(false, true, true, true, 10, 100).empty());
    CHECK_EQ(OfflineRaidStopReason(false, false, true, true, 10, 100), "module_disabled");
    CHECK_EQ(OfflineRaidStopReason(false, true, false, true, 10, 100), "keep_cast_off");
    CHECK_EQ(OfflineRaidStopReason(false, true, true, false, 10, 100), "driver_lost");
    CHECK_EQ(OfflineRaidStopReason(false, false, false, false, 100, 100), "timeout");
    CHECK_EQ(OfflineRaidStopReason(true, false, false, false, 100, 100), "human_login");
    CHECK_EQ(OfflineRaidStopReason(false, true, true, true, 10, 100, false), "instance_strategies_off");
    CHECK_EQ(OfflineRaidStopReason(true, true, true, true, 10, 100, false), "human_login");
}

LEEROY_TEST(OfflineRaid_StrayRecallWaitsForGraceAndSafeOwnership)
{
    OfflineRecall recall;
    CHECK(recall.Observe(false, true, 29999) == RecallAction::Wait);
    CHECK(recall.Observe(false, false, 1) == RecallAction::Wait);
    CHECK_EQ(recall.attempts, 0u);
    // Unsafe means no mutation: combat, unknown encounters, foreign body/group,
    // PvP, transfers, dead bodies or an unproven destination all use this path.
    CHECK(recall.Observe(false, false, 60000) == RecallAction::Wait);
    CHECK(recall.Observe(false, true, 0) == RecallAction::Summon);
    CHECK_EQ(recall.attempts, 1u);
}

LEEROY_TEST(OfflineRaid_StrayRecallRequiresObservedArrivalAndBoundsRetries)
{
    OfflineRecall recall;
    CHECK(recall.Observe(false, true, 30000) == RecallAction::Summon);
    recall.pending = true; // TeleportTo accepted, but it has not arrived.
    CHECK(recall.Observe(false, true, 59999) == RecallAction::Wait);
    CHECK(recall.Observe(false, true, 1) == RecallAction::Summon);
    CHECK_EQ(recall.attempts, 2u);
    CHECK(recall.Observe(true, false, 0) == RecallAction::Arrived);
    CHECK(recall.Observe(true, false, 0) == RecallAction::Wait);
    CHECK_EQ(recall.absentMs, 0u);
    CHECK_EQ(recall.attempts, 2u); // Returning does not grant another unlimited budget.
    CHECK(recall.Observe(false, true, 30000) == RecallAction::Wait);
    CHECK(recall.Observe(false, true, 30000) == RecallAction::Summon);
    CHECK_EQ(recall.attempts, 3u);
    CHECK(recall.Observe(false, true, std::numeric_limits<std::uint32_t>::max()) == RecallAction::Wait);
}

LEEROY_TEST(OfflineRaid_RefusedRecallCostsAnAttemptAndNeverReportsArrival)
{
    OfflineRecall recall;
    for (unsigned i = 0; i < OfflineRecallAttempts; ++i)
    {
        CHECK(recall.Observe(false, true, OfflineRecallRetryMs) == RecallAction::Summon);
        recall.pending = false; // A refused TeleportTo is not a completed recovery.
    }
    CHECK(recall.Observe(true, true, 1) == RecallAction::Wait);
    CHECK(recall.Observe(false, true, OfflineRecallRetryMs) == RecallAction::Wait);
}

LEEROY_TEST(OfflineRaid_RecallDoesNotWeakenMinimumRosterOrLeaderStops)
{
    auto state = Base();
    state.membersPresent = 9;
    CHECK(Decide(state, limits).verdict == Verdict::Continue);
    state.membersPresent = 10; // An observed arrival restores roster strength.
    CHECK(Decide(state, limits).verdict == Verdict::Continue);
    state.membersPresent = 7;
    CHECK_EQ(Decide(state, limits).reason, "too_few");
    state.membersPresent = 10;
    state.leaderPresent = false;
    CHECK_EQ(Decide(state, limits).reason, "leader_lost");
}

namespace
{
RecallQuiet QuietRaid()
{
    RecallQuiet quiet;
    quiet.encounterKnown = quiet.driver = quiet.leaderAi = quiet.leaderMasterIsDriver = quiet.leaderAlive = true;
    return quiet;
}
RecallBody SafeBody()
{
    RecallBody body;
    body.inWorld = body.alive = body.difficultyMatches = body.destinationMatches = true;
    return body;
}
bool Recallable(RecallQuiet const& quiet, RecallBody const& body, RecallPvp const& pvp = {}, bool owned = true)
{
    return RecallSafe(quiet, owned, body, [&pvp] { return pvp; });
}
}

LEEROY_TEST(OfflineRaid_RecallSafeAdmitsOnlyAQuietOwnedFreeBody)
{
    CHECK(Recallable(QuietRaid(), SafeBody()));
    CHECK(!Recallable(QuietRaid(), SafeBody(), {}, false)); // A body this job does not hold is never moved.
    OfflineRecall recall;
    CHECK(recall.Observe(false, Recallable(QuietRaid(), SafeBody()), OfflineRecallGraceMs) == RecallAction::Summon);
}

// Each quiet-raid condition on its own must refuse: removing any one from the policy fails here.
LEEROY_TEST(OfflineRaid_RecallWaitsForEveryQuietRaidCondition)
{
    using Q = RecallQuiet;
    for (auto required : {&Q::encounterKnown, &Q::driver, &Q::leaderAi, &Q::leaderMasterIsDriver, &Q::leaderAlive})
    {
        auto quiet = QuietRaid();
        quiet.*required = false;
        CHECK(!Recallable(quiet, SafeBody()));
    }
    for (auto blocking : {&Q::bossEngaged, &Q::realmGroup, &Q::leaderGhost, &Q::leaderCombat, &Q::leaderTransport,
        &Q::leaderVehicle, &Q::memberCombat})
    {
        auto quiet = QuietRaid();
        quiet.*blocking = true;
        CHECK(!Recallable(quiet, SafeBody()));
    }
}

// Combat, transfer, bind and dungeon checks on the stray itself, one at a time.
LEEROY_TEST(OfflineRaid_RecallWaitsForEveryBodyCondition)
{
    using B = RecallBody;
    for (auto required : {&B::inWorld, &B::alive, &B::difficultyMatches, &B::destinationMatches})
    {
        auto body = SafeBody();
        body.*required = false;
        CHECK(!Recallable(QuietRaid(), body));
    }
    for (auto blocking : {&B::teleporting, &B::ghost, &B::combat, &B::flight, &B::transport, &B::vehicle,
        &B::pendingBind, &B::inDungeon})
    {
        auto body = SafeBody();
        body.*blocking = true;
        CHECK(!Recallable(QuietRaid(), body));
    }
}

LEEROY_TEST(OfflineRaid_RecallWaitsForEveryPvpAndWintergraspExposure)
{
    for (auto busy : {&RecallPvp::duel, &RecallPvp::battleground, &RecallPvp::battlegroundQueue,
        &RecallPvp::arenaObserved})
    {
        RecallPvp pvp;
        pvp.*busy = true;
        CHECK(RecallPvpBusy(pvp));
        CHECK(!Recallable(QuietRaid(), SafeBody(), pvp));
    }
    using Facts = Leeroy::Realm::WintergraspMemberFacts;
    for (auto member : {&Facts::foreignHistory, &Facts::inZone, &Facts::inBattlefieldGroup,
        &Facts::queued, &Facts::invited, &Facts::inWar})
    {
        RecallPvp pvp;
        pvp.wintergrasp.*member = true;
        CHECK(RecallPvpBusy(pvp));
        CHECK(!Recallable(QuietRaid(), SafeBody(), pvp));
        OfflineRecall recall;
        CHECK(recall.Observe(false, Recallable(QuietRaid(), SafeBody(), pvp), 30000) == RecallAction::Wait);
        CHECK_EQ(recall.attempts, 0u);
    }
}

// The battlefield and arena-observer lookups run last: never for a body in a dungeon,
// not owned, unsafe, or while the raid is not quiet.
LEEROY_TEST(OfflineRaid_RecallReadsPvpOnlyAfterCheaperChecksPass)
{
    int reads = 0;
    auto reader = [&reads] { ++reads; return RecallPvp{}; };
    auto dungeon = SafeBody();
    dungeon.inDungeon = true;
    CHECK(!RecallSafe(QuietRaid(), true, dungeon, reader));
    CHECK(!RecallSafe(QuietRaid(), false, SafeBody(), reader));
    auto loud = QuietRaid();
    loud.memberCombat = true;
    CHECK(!RecallSafe(loud, true, SafeBody(), reader));
    auto fighting = SafeBody();
    fighting.combat = true;
    CHECK(!RecallSafe(QuietRaid(), true, fighting, reader));
    CHECK_EQ(reads, 0);
    CHECK(RecallSafe(QuietRaid(), true, SafeBody(), reader));
    CHECK_EQ(reads, 1);
}

namespace
{
MasterFacts HeldMasterless()
{
    MasterFacts facts;
    facts.driver = facts.assigned = facts.sameBody = facts.inJobGroup = facts.masterEmpty = true;
    return facts;
}
}

// 2026-11-07: 3112 and 251 were in the raid group with an empty master (stock
// ResetAiAction), so teardown retained restore_ownership for both and never ended.
LEEROY_TEST(OfflineRaid_MasterlessHeldMemberGetsTheDriverBack)
{
    CHECK(ReassertDriverMaster(HeldMasterless()));
}

LEEROY_TEST(OfflineRaid_MasterRepairNeverSeizesABodyThisJobDoesNotHold)
{
    using M = MasterFacts;
    for (auto required : {&M::driver, &M::assigned, &M::sameBody, &M::inJobGroup, &M::masterEmpty})
    {
        auto facts = HeldMasterless();
        facts.*required = false; // masterEmpty false: a foreign, non-empty master stays someone else's.
        CHECK(!ReassertDriverMaster(facts));
    }
    for (auto blocking : {&M::released, &M::foreignLease, &M::masterIsDriver})
    {
        auto facts = HeldMasterless();
        facts.*blocking = true; // A released master is teardown's own act; never undo it.
        CHECK(!ReassertDriverMaster(facts));
    }
}

LEEROY_TEST(OfflineRaid_RestartDropsAnAbsentStrayAfterGraceWhenEnoughRemain)
{
    CHECK(DecideRestartRoster(OfflineRestartAbsentMs, 1, 9, true, 8) == RestartRoster::DropAbsent);
    CHECK(DecideRestartRoster(OfflineRestartAbsentMs, 2, 8, true, 8) == RestartRoster::DropAbsent);
    CHECK(DecideRestartRoster(OfflineRestartAbsentMs - 1, 1, 9, true, 8) == RestartRoster::Wait);
    CHECK(DecideRestartRoster(OfflineRestartAbsentMs, 0, 10, true, 8) == RestartRoster::Wait);
}

LEEROY_TEST(OfflineRaid_RestartKeepsTooFewAndLeaderRules)
{
    CHECK(DecideRestartRoster(OfflineRestartAbsentMs, 3, 7, true, 8) == RestartRoster::TooFew);
    // The leader is never dropped: its own absence waits for the restart timeout.
    CHECK(DecideRestartRoster(OfflineRestartAbsentMs, 1, 9, false, 8) == RestartRoster::Wait);
    CHECK(DecideRestartRoster(OfflineRestartAbsentMs * 10, 1, 9, false, 8) == RestartRoster::Wait);
}

LEEROY_TEST(OfflineRaid_LostJournalFreeMemberStopsBlockingAfterCleanupFails)
{
    CHECK(AbandonLostMember(false, true, false, OfflineCleanupFailMs));
    CHECK(!AbandonLostMember(false, true, false, OfflineCleanupFailMs - 1));
    CHECK(!AbandonLostMember(false, false, false, OfflineCleanupFailMs)); // Still ours: clean it up.
    CHECK(!AbandonLostMember(false, true, true, OfflineCleanupFailMs * 100)); // Its journal needs its own restore.
    CHECK(!AbandonLostMember(true, true, false, OfflineCleanupFailMs * 100)); // Benchmarks keep strict recovery.
}

LEEROY_TEST(OfflineRaid_StuckSlotReleasesTenMinutesAfterFailureWhenNothingIsHeld)
{
    CHECK(DecideSlotRelease(false, true, OfflineSlotReleaseMs, 0) == SlotRelease::Release);
    CHECK(DecideSlotRelease(false, true, OfflineSlotReleaseMs - 1, 0) == SlotRelease::Wait);
    CHECK(DecideSlotRelease(false, false, OfflineSlotReleaseMs * 10, 0) == SlotRelease::Keep); // No failure report yet.
}

LEEROY_TEST(OfflineRaid_StuckSlotStaysWhileAMemberIsProvablyHeld)
{
    CHECK(DecideSlotRelease(false, true, OfflineSlotReleaseMs, 1) == SlotRelease::Keep);
    CHECK(DecideSlotRelease(false, true, std::numeric_limits<std::uint32_t>::max(), 3) == SlotRelease::Keep);
    CHECK(DecideSlotRelease(true, true, OfflineSlotReleaseMs * 10, 0) == SlotRelease::Keep); // Never a benchmark.
}
