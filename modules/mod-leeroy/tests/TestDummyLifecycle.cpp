// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyDummyLifecycle.h"
#include "LeeroyBotJournal.h"
#include "LeeroyDummyDamage.h"
#include "LeeroyTrainingProtocol.h"
#include "TestHarness.h"

using namespace Leeroy::Training;

LEEROY_TEST(Training_DummyUsabilityRejectsEveryIndependentConflict)
{
    DummyBotState usable{true, true, true, false, false, false, false, false};
    CHECK(DummyBotUsable(usable));
    for (auto field : {&DummyBotState::isCast, &DummyBotState::online, &DummyBotState::alive,
        &DummyBotState::inCombat, &DummyBotState::grouped, &DummyBotState::onInstanceMap,
        &DummyBotState::inBattleground, &DummyBotState::inOfflineRaid, &DummyBotState::nonPermanentBound})
    {
        auto altered = usable;
        altered.*field = !(altered.*field);
        CHECK(!DummyBotUsable(altered));
    }
    CHECK_EQ(kDummyEntry, 31146);
    CHECK_EQ(kDummyPlaces.size(), 2);
}

LEEROY_TEST(Training_DummySuccessRequiresObservedReturnAfterFullFight)
{
    DummyLifecycle job(1000, 30);
    DummyObservation state;
    CHECK(job.Step(1000, state) == DummyStep::Prepare);
    job.Moving();
    CHECK(job.Step(2000, state) == DummyStep::Move);
    state.allArrived = true;
    CHECK(job.Step(3000, state) == DummyStep::StartFight);
    CHECK(job.Fighting(3000));
    state.anyFighting = true;
    CHECK(job.Step(32999, state) == DummyStep::Fight);
    CHECK(job.Step(33000, state) == DummyStep::Return);
    CHECK(job.Phase() == DummyPhase::Returning);
    CHECK(job.Step(40000, state) == DummyStep::Return); // Accepted teleport is not an observed arrival.
    state.allReturned = true;
    CHECK(job.Step(40001, state) == DummyStep::Finish);
    CHECK(!job.TerminalFailed());
    CHECK(job.Step(999999, state) == DummyStep::Wait);
}

LEEROY_TEST(Training_DummyCancellationDoesNotRenewTheReturnDeadline)
{
    DummyLifecycle job(0, 300);
    job.Moving();
    CHECK(job.Fighting(1000));
    DummyObservation state{true, true, true, true, false};
    CHECK(job.Step(2000, state) == DummyStep::Return);
    CHECK_EQ(job.Reason(), "human_login");
    for (std::uint64_t now = 3000; now < 32000; now += 1000) job.Stop("training_off", now);
    CHECK(job.Step(31999, state) == DummyStep::Return);
    CHECK(job.Step(32000, state) == DummyStep::ReportFailure);
    CHECK(job.Phase() == DummyPhase::Recovery);
    CHECK(job.TerminalFailed());
    CHECK_EQ(job.Reason(), "human_login");
    CHECK(job.Step(32001, state) == DummyStep::Return);
    CHECK(job.Step(32002, state) == DummyStep::Wait);
    state.allReturned = true;
    CHECK(job.Step(34001, state) == DummyStep::Recovered); // No second terminal success after reported failure.
}

LEEROY_TEST(Training_DummyQueuedTimeAndMovementFailureAreBounded)
{
    DummyLifecycle queued(1000, 30);
    DummyObservation state;
    CHECK(queued.Step(21000, state) == DummyStep::Return);
    CHECK_EQ(queued.Reason(), "move_timeout");
    CHECK(!queued.Fighting(21000));
    CHECK(queued.Step(51000, state) == DummyStep::ReportFailure);
    CHECK(queued.Phase() == DummyPhase::Recovery);
    CHECK(queued.Step(52000, state) == DummyStep::Return);
    CHECK(queued.Step(52001, state) == DummyStep::Wait);
}

LEEROY_TEST(Training_DummyOffBeforePreparationDoesNotBeginGameWork)
{
    DummyLifecycle job(1000, 30);
    DummyObservation state{false, false, false, false, true};
    CHECK(job.Step(1001, state) == DummyStep::Finish);
    CHECK_EQ(job.Reason(), "training_off");
    CHECK(job.TerminalFailed());
}

LEEROY_TEST(Training_DummyExternalDamageAlarmIsScopedAndResetBetweenSlots)
{
    DummyDamageAlarm alarm;
    alarm.Reset(9000, {101, 102});
    alarm.Damage(9000, 101, 40); // Damage from the requested dummy does not invalidate the comparison.
    alarm.Damage(8000, 101, 0);
    alarm.Damage(8000, 103, 40);
    CHECK(!alarm.Attacked(101));
    CHECK(!alarm.Attacked(103));
    alarm.Damage(8000, 102, 40);
    CHECK(alarm.Attacked(102));
    CHECK(!alarm.Attacked(101));
    alarm.Reset(9001, {102});
    CHECK(!alarm.Attacked(102));
    alarm.Damage(0, 102, 1); // Unknown/environmental incoming source is conservatively unsafe too.
    CHECK(alarm.Attacked(102));
}

LEEROY_TEST(Training_DummyReturnProofRejectsNewOwnerOrSessionBeforeAnyReset)
{
    DummyReturnState state{true, true, true, true, false, false, false, false, true, true};
    CHECK(DecideDummyReturn(state) == DummyReturnStep::ResetStrategies);
    state.owned = false;
    CHECK(DecideDummyReturn(state) == DummyReturnStep::Block);
    state.owned = true;
    state.sameSession = false;
    CHECK(DecideDummyReturn(state) == DummyReturnStep::Block);
    state.sameSession = true;
    state.atTrial = false;
    CHECK(DecideDummyReturn(state) == DummyReturnStep::Block);
}

LEEROY_TEST(Training_DummyReturnRequiresResetAndArrivalDespiteAcceptedTeleport)
{
    DummyReturnState state{true, true, true, true, false, true, true, false, true, false};
    CHECK(DecideDummyReturn(state) == DummyReturnStep::ResetStrategies);
    state.atOrigin = false;
    state.atTrial = true;
    state.resetDone = true;
    CHECK(DecideDummyReturn(state) == DummyReturnStep::Teleport);
    state.teleporting = true;
    CHECK(DecideDummyReturn(state) == DummyReturnStep::Wait);
    state.atOrigin = true;
    CHECK(DecideDummyReturn(state) == DummyReturnStep::Wait);
    state.teleporting = false;
    state.owned = false; // A later acquisition requires no mutation once our reset and return were already verified.
    CHECK(DecideDummyReturn(state) == DummyReturnStep::Complete);
    state.sameSession = false;
    CHECK(DecideDummyReturn(state) == DummyReturnStep::Block);
}

LEEROY_TEST(Training_DummyClaimedSetupFailureStillRequiresActualOriginProof)
{
    DummyReturnState state{true, true, true, true, false, false, true, true, true, false};
    // A refused/throwing teleport or failed setup is not proof the participant stayed at its saved origin.
    CHECK(DecideDummyReturn(state) == DummyReturnStep::Block);
    state.atOrigin = true;
    CHECK(DecideDummyReturn(state) == DummyReturnStep::Complete);
    state.prepared = false; // A genuinely untouched request is the only unconditional completion case.
    state.atOrigin = false;
    CHECK(DecideDummyReturn(state) == DummyReturnStep::Complete);
}

LEEROY_TEST(Training_DummyResetPrecedesGroupAndMasterRelease)
{
    DummyReturnState state{true, true, true, true, false, false, false, false, true, true, false, false, false};
    CHECK(DecideDummyReturn(state) == DummyReturnStep::ResetStrategies);
    CHECK(DecideDummyReturn(state) == DummyReturnStep::ResetStrategies); // A throwing reset retains control.
    state.resetDone = true;
    CHECK(DecideDummyReturn(state) == DummyReturnStep::ReleaseGroup);
    CHECK(DecideDummyReturn(state) == DummyReturnStep::ReleaseGroup); // Refused/asynchronous removal is not release.
    state.groupReleased = true;
    CHECK(DecideDummyReturn(state) == DummyReturnStep::RestoreLeaderFlag);
    state.leaderRestored = true;
    CHECK(DecideDummyReturn(state) == DummyReturnStep::ReleaseMaster);
    state.released = true;
    CHECK(DecideDummyReturn(state) == DummyReturnStep::FinalStockReset);
    state.finalResetDone = true;
    CHECK(DecideDummyReturn(state) == DummyReturnStep::Teleport);
    state.atOrigin = true;
    CHECK(DecideDummyReturn(state) == DummyReturnStep::Complete);
}

LEEROY_TEST(Training_DummyPartialGroupCreationStillRequiresOwnedCleanup)
{
    DummyReturnState state{true, true, true, true, false, true, true, true, true, false, false, false, false};
    CHECK(DecideDummyReturn(state) == DummyReturnStep::ReleaseGroup);
    state.groupReleased = true;
    CHECK(DecideDummyReturn(state) == DummyReturnStep::RestoreLeaderFlag);
    state.leaderRestored = true;
    CHECK(DecideDummyReturn(state) == DummyReturnStep::FinalStockReset);
    state.finalResetDone = true;
    CHECK(DecideDummyReturn(state) == DummyReturnStep::Complete);
    state.groupReleased = false;
    state.owned = false; // A different group or an added outsider cannot be disbanded as partial setup.
    CHECK(DecideDummyReturn(state) == DummyReturnStep::Block);
}

LEEROY_TEST(Training_DummyGroupScopeRejectsTransferInvitesAndExtraMembers)
{
    CHECK(DummyGroupProven(false, true, true, 1, true, 0, true));
    CHECK(!DummyGroupProven(false, true, false, 1, true, 0, true));
    CHECK(!DummyGroupProven(false, true, true, 2, true, 0, true));
    CHECK(!DummyGroupProven(false, true, true, 1, false, 0, true));
    CHECK(!DummyGroupProven(false, true, true, 1, true, 1, true));
    CHECK(!DummyGroupProven(false, true, true, 1, true, 0, false));
    CHECK(DummyGroupProven(false, false, true, 0, false, 0, true)); // Exact retained pre-insertion object.
    CHECK(!DummyGroupProven(false, true, true, 0, false, 0, true)); // Inconsistent partial insertion is unresolved.
    CHECK(!DummyGroupProven(false, false, false, 0, false, 0, true)); // Disappearance without our release intent.
    CHECK(DummyGroupProven(true, false, false, 0, false, 0, true));
    CHECK(!DummyGroupProven(true, true, false, 1, true, 0, true));
}

LEEROY_TEST(Training_DummyOwnershipRejectsCurrentHumanAndReacquisition)
{
    DummyOwnerState state{true, false, true, false, true, false, false, true, true};
    CHECK(DummyOwnerProven(state));
    for (auto field : {&DummyOwnerState::sameSession, &DummyOwnerState::isHuman, &DummyOwnerState::isCast,
        &DummyOwnerState::isSelfBot, &DummyOwnerState::sameGuild, &DummyOwnerState::invited,
        &DummyOwnerState::reserved, &DummyOwnerState::groupOwned, &DummyOwnerState::masterOwned,
        &DummyOwnerState::loggingOut})
    {
        auto changed = state;
        changed.*field = !(changed.*field);
        CHECK(!DummyOwnerProven(changed));
        DummyReturnState returning{true, true, true, true, false, true, false, false,
            DummyOwnerProven(changed), false, false, false};
        CHECK(DecideDummyReturn(returning) == DummyReturnStep::Block);
    }
}

LEEROY_TEST(Training_DummyFinalStockResetRetainsAnIndependentRetryReceipt)
{
    DummyReturnState state{true, true, true, true, false, true, true, true, true, false, true, true, false};
    CHECK(DecideDummyReturn(state) == DummyReturnStep::FinalStockReset);
    CHECK(DecideDummyReturn(state) == DummyReturnStep::FinalStockReset); // A throwing final reset did not complete.
    state.owned = false;
    CHECK(DecideDummyReturn(state) == DummyReturnStep::Block); // Never rebuild a new owner's defaults.
    state.owned = true;
    state.finalResetDone = true;
    CHECK(DecideDummyReturn(state) == DummyReturnStep::Complete);
}

LEEROY_TEST(Training_DummyPendingLogoutWaitsWithoutResettingOrRenewingCleanup)
{
    DummyReturnState returning{true, true, true, true, false, true, false, false, false, false,
        false, false, false, true};
    CHECK(DecideDummyReturn(returning) == DummyReturnStep::Wait);
    DummyLifecycle job(0, 30);
    job.Stop("human_login", 1000);
    DummyObservation state;
    CHECK(job.Step(1001, state) == DummyStep::Return);
    CHECK(DecideDummyReturn(returning) == DummyReturnStep::Wait);
    CHECK(job.Step(31000, state) == DummyStep::ReportFailure);
    returning.loggingOut = false;
    returning.sameSession = false;
    CHECK(DecideDummyReturn(returning) == DummyReturnStep::Block); // A new login cannot inherit cleanup.
}

LEEROY_TEST(Training_DummyNonPermanentBindingsProtectAdmissionAndDisband)
{
    DummyBotState eligible{true, true, true, false, false, false, false, false, false};
    CHECK(DummyBotUsable(eligible));
    eligible.nonPermanentBound = true;
    CHECK(!DummyBotUsable(eligible));
    CHECK(!DummyGroupProven(false, true, true, 1, true, 0, true, true)); // A later new bind is also protected.
    CHECK(DummyGroupProven(false, true, true, 1, true, 0, true, false)); // Permanent-only bindings are not removed.
    CHECK(DummyGroupProven(true, false, false, 0, false, 0, true, true)); // No disband remains after release.
}

LEEROY_TEST(Training_DummyStopNamesCleanupPendingNotTrainingOff)
{
    DummyLifecycle job(0, 60);
    job.Moving();
    DummyObservation state;
    state.enabled = false;
    state.offReason = "cleanup_pending";
    CHECK(job.Step(1000, state) == DummyStep::Return);
    CHECK_EQ(job.Reason(), "cleanup_pending");
    DummyLifecycle other(0, 60);
    other.Moving();
    DummyObservation unnamed;
    unnamed.enabled = false;
    unnamed.offReason = {};
    CHECK(other.Step(1000, unnamed) == DummyStep::Return);
    CHECK_EQ(other.Reason(), "training_off");
}

LEEROY_TEST(Training_DummyMasterReassertedOnlyOnAnOtherwiseOwnedBody)
{
    // The stock reset our own solo party triggers leaves every fact but the master intact.
    DummyOwnerState state{true, false, true, false, true, false, false, true, false, false};
    CHECK(!DummyOwnerProven(state));
    CHECK(DummyMasterReassertable(state, false, false, true));
    CHECK(!DummyMasterReassertable(state, false, false, false)); // A foreign master is an ownership change.
    CHECK(!DummyMasterReassertable(state, true, false, true));   // Released on purpose: never put back.
    CHECK(!DummyMasterReassertable(state, false, true, true));   // Our party is gone: not the stock reset.
    for (auto field : {&DummyOwnerState::sameSession, &DummyOwnerState::isCast, &DummyOwnerState::sameGuild,
        &DummyOwnerState::groupOwned})
    {
        auto altered = state;
        altered.*field = false;
        CHECK(!DummyMasterReassertable(altered, false, false, true));
    }
    for (auto field : {&DummyOwnerState::isHuman, &DummyOwnerState::isSelfBot, &DummyOwnerState::invited,
        &DummyOwnerState::reserved, &DummyOwnerState::loggingOut})
    {
        auto altered = state;
        altered.*field = true;
        CHECK(!DummyMasterReassertable(altered, false, false, true));
    }
}

LEEROY_TEST(Training_DummyHeldWhileAnyControlRemains)
{
    DummyHoldState none{true, false, false, false, false, false};
    CHECK(!DummyMemberHeld(none));
    CHECK(DummyMemberHeld({true, false, true, false, false, false}));  // An unregistered group object we own.
    CHECK(DummyMemberHeld({true, false, false, true, false, false}));  // Still listed in our solo party.
    CHECK(DummyMemberHeld({true, false, false, false, true, true}));   // Same body, the driver as master.
    CHECK(!DummyMemberHeld({true, false, false, false, false, true})); // A new session has a fresh master.
    CHECK(!DummyMemberHeld({true, false, false, false, true, false}));
    CHECK(!DummyMemberHeld({true, true, true, true, true, true}));     // Proved returned.
    CHECK(!DummyMemberHeld({false, false, true, true, true, true}));   // Never prepared: never touched.
}

LEEROY_TEST(Training_DummyReleaseWaitsTheBoundThenKeepsOnlyHeldBodies)
{
    CHECK(DecideDummyRelease(false, kDummyReleaseMs * 10, 0) == DummyRelease::Wait);
    CHECK(DecideDummyRelease(true, kDummyReleaseMs - 1, 0) == DummyRelease::Wait);
    CHECK(DecideDummyRelease(true, kDummyReleaseMs, 0) == DummyRelease::Release);
    CHECK(DecideDummyRelease(true, kDummyReleaseMs, 1) == DummyRelease::Keep);
}

LEEROY_TEST(Training_DummyKeepsEveryMemberStillReturningPastTheBound)
{
    DummyHoldState releasedAndTeleporting{true, false, false, false, true, false}; // Group and master let go.
    CHECK(!DummyMemberHeld(releasedAndTeleporting));
    // Still being returned (not blocked): the job still acts on it, so it stays kept and fenced.
    CHECK(DummyFateAfterBound(releasedAndTeleporting, false) == DummyMemberFate::Keep);
    // Blocked with nothing of ours left: abandoned, never touched or fenced by the trial again.
    CHECK(DummyFateAfterBound(releasedAndTeleporting, true) == DummyMemberFate::Abandon);
    DummyHoldState inOurParty{true, false, false, true, false, false};
    CHECK(DummyFateAfterBound(inOurParty, true) == DummyMemberFate::Keep); // Blocked but held.
    CHECK(DummyFateAfterBound({true, true, true, true, true, true}, true) == DummyMemberFate::Done);
    CHECK(DummyFateAfterBound({false, false, false, false, false, false}, false) == DummyMemberFate::Done);
    CHECK(DummyTrialFences(DummyMemberFate::Keep));
    CHECK(!DummyTrialFences(DummyMemberFate::Abandon));
    CHECK(!DummyTrialFences(DummyMemberFate::Done));
    CHECK(!DummyBoundPassed(false, kDummyReleaseMs));
    CHECK(!DummyBoundPassed(true, kDummyReleaseMs - 1));
    CHECK(DummyBoundPassed(true, kDummyReleaseMs));
}

LEEROY_TEST(Training_DummyAbandonedJournaledMemberStaysFencedByItsJournal)
{
    // Blocked, unheld and journaled: the journal no longer keeps the trial alive.
    DummyHoldState unheld{true, false, false, false, true, false};
    BotJournal journal;
    journal.NoteValue("wait for attack time", "4");
    CHECK(!journal.Empty()); // HasTrainingJournal fences the body and RestoreTrainingBots restores it.
    auto const fate = DummyFateAfterBound(unheld, true);
    CHECK(fate == DummyMemberFate::Abandon);
    CHECK(!DummyTrialFences(fate)); // So the trial stops fencing it and restoration is no longer skipped.
    // With no member kept, the job is released instead of being kept until a restart.
    CHECK(DecideDummyRelease(true, kDummyReleaseMs, DummyTrialFences(fate) ? 1 : 0) == DummyRelease::Release);
}

LEEROY_TEST(Training_DummyLetGoReleasesOurControlWhereItStandsAndNeverTeleports)
{
    // 2026-11-23: 1507 left the trial spot while every ownership fact held, so its return blocked
    // (location_changed) with our solo party and the driver still on it, and the trial kept it 3 h.
    DummyReturnState moved;
    moved.prepared = moved.connected = moved.sameSession = moved.online = moved.owned = true;
    moved.groupReleased = moved.leaderRestored = moved.finalResetDone = false;
    CHECK(DecideDummyReturn(moved) == DummyReturnStep::Block); // The return still refuses the teleport home.
    CHECK(DecideDummyLetGo(moved) == DummyReturnStep::ResetStrategies);
    moved.resetDone = true;
    CHECK(DecideDummyLetGo(moved) == DummyReturnStep::ReleaseGroup);
    moved.groupReleased = true;
    CHECK(DecideDummyLetGo(moved) == DummyReturnStep::RestoreLeaderFlag);
    moved.leaderRestored = true;
    CHECK(DecideDummyLetGo(moved) == DummyReturnStep::ReleaseMaster);
    moved.released = true;
    CHECK(DecideDummyLetGo(moved) == DummyReturnStep::FinalStockReset);
    moved.finalResetDone = true;
    CHECK(DecideDummyLetGo(moved) == DummyReturnStep::Complete); // Nothing of ours is left on it.
    for (int bits = 0; bits < 64; ++bits)
    {
        DummyReturnState any;
        any.prepared = any.connected = any.sameSession = any.online = any.owned = true;
        any.resetDone = (bits & 1) != 0;
        any.groupReleased = (bits & 2) != 0;
        any.leaderRestored = (bits & 4) != 0;
        any.released = (bits & 8) != 0;
        any.finalResetDone = (bits & 16) != 0;
        any.atOrigin = (bits & 32) != 0;
        CHECK(DecideDummyLetGo(any) != DummyReturnStep::Teleport);
        CHECK(DecideDummyLetGo(any) != DummyReturnStep::Block);
    }
}

LEEROY_TEST(Training_DummyLetGoTouchesNothingThatIsNotProvablyOurs)
{
    DummyReturnState ours;
    ours.prepared = ours.connected = ours.sameSession = ours.online = ours.owned = true;
    CHECK(DecideDummyLetGo(ours) == DummyReturnStep::ResetStrategies);
    for (auto field : {&DummyReturnState::connected, &DummyReturnState::sameSession, &DummyReturnState::online,
        &DummyReturnState::owned})
    {
        auto changed = ours;
        changed.*field = false;
        CHECK(DecideDummyLetGo(changed) == DummyReturnStep::Wait);
    }
    for (auto field : {&DummyReturnState::teleporting, &DummyReturnState::loggingOut})
    {
        auto changed = ours;
        changed.*field = true;
        CHECK(DecideDummyLetGo(changed) == DummyReturnStep::Wait);
    }
    auto untouched = ours;
    untouched.prepared = false;
    CHECK(DecideDummyLetGo(untouched) == DummyReturnStep::Complete);
}

LEEROY_TEST(Training_DummyLetGoOwnershipAcceptsOnlyOurPartyWithTheDriverOrNoMaster)
{
    DummyOwnerState proven{true, false, true, false, true, false, false, true, true, false};
    CHECK(DummyLetGoOwned(proven, false, false, false));
    auto masterless = proven;
    masterless.masterOwned = false; // The stock reset our party triggers dropped the driver.
    CHECK(DummyLetGoOwned(masterless, false, false, true));
    CHECK(!DummyLetGoOwned(masterless, false, false, false)); // A foreign master took it.
    for (auto field : {&DummyOwnerState::sameSession, &DummyOwnerState::isCast, &DummyOwnerState::sameGuild,
        &DummyOwnerState::groupOwned})
    {
        auto altered = proven;
        altered.*field = false;
        CHECK(!DummyLetGoOwned(altered, false, false, false));
    }
    for (auto field : {&DummyOwnerState::isHuman, &DummyOwnerState::isSelfBot, &DummyOwnerState::invited,
        &DummyOwnerState::reserved, &DummyOwnerState::loggingOut})
    {
        auto altered = proven;
        altered.*field = true;
        CHECK(!DummyLetGoOwned(altered, false, false, false));
    }
}

LEEROY_TEST(Training_DummyMovedMemberLetGoAtTheBoundEndsTheTrial)
{
    // Before: blocked, still in our solo party under the driver, so the trial kept it for good.
    DummyHoldState held{true, false, false, true, true, true};
    CHECK(DummyMemberHeld(held));
    CHECK(DummyFateAfterBound(held, true) == DummyMemberFate::Keep);
    // After letting go: no party of ours lists it and the driver is not its master.
    DummyHoldState letGo{true, false, false, false, true, false};
    CHECK(!DummyMemberHeld(letGo));
    auto const fate = DummyFateAfterBound(letGo, true);
    CHECK(fate == DummyMemberFate::Abandon); // Unreturned: native's receipt says released, never restored.
    CHECK(DecideDummyRelease(true, kDummyReleaseMs, DummyTrialFences(fate) ? 1 : 0) == DummyRelease::Release);
}

LEEROY_TEST(Training_DummyCoreDisbandsAOneMemberPartyOnEnteringAMap)
{
    // LFGPlayerScript::OnPlayerMapChanged outside an LFG dungeon: a group of one member or fewer is disbanded.
    CHECK(!DummySoloPartySurvivesMapEntry(1, false));
    CHECK(!DummySoloPartySurvivesMapEntry(0, false));
    CHECK(DummySoloPartySurvivesMapEntry(2, false));
    CHECK(DummySoloPartySurvivesMapEntry(1, true));
}

LEEROY_TEST(Training_DummyPartyFormedBeforeAFarTeleportIsLostOnArrival)
{
    // The 2026-11-10..14 failure. The old order formed the solo party and took the driver before moving the bot from
    // Northrend to Orgrimmar. The core disbands that party as the bot is added to Kalimdor, before its AI wakes, so
    // the first move check finds the driver still master but no party: an ownership change, and no reassert.
    CHECK(!DummySoloPartySurvivesMapEntry(1, false));
    bool const groupOwned = DummyGroupProven(false, false, false, 0, false, 0, false); // Our party object is gone.
    CHECK(!groupOwned);
    DummyOwnerState arrived{true, false, true, false, true, false, false, groupOwned, true, false};
    CHECK(!DummyOwnerProven(arrived));
    CHECK_EQ(DummyOwnerMissing(arrived), "group");
    CHECK(!DummyMasterReassertable(arrived, false, false, false)); // The master is not empty: nothing to put back.
    CHECK(DecideDummyMove({false, DummyOwnerProven(arrived), true, false, true, true, false, 0}) == DummyMoveStep::Block);

    // The fixed order moves the bot untouched: in transit it is the plain unbound body, which a map change cannot
    // take from us, and the party is formed at the dummy on the map of the fight.
    DummyOwnerState unbound{true, false, true, false, true, false, false,
        DummyGroupProven(true, false, false, 0, false, 0, false), true, false};
    CHECK(DummyOwnerProven(unbound));
    CHECK(DecideDummyMove({false, true, true, false, true, false, false, 0}) == DummyMoveStep::Setup);
    CHECK(DummyGroupProven(false, true, true, 1, true, 0, true)); // Our party there, until ReturnMember releases it.
    // On the way home the party is always released before the teleport, so the rule cannot take it either.
    for (int bits = 0; bits < 16; ++bits)
    {
        DummyReturnState home;
        home.prepared = home.connected = home.sameSession = home.online = home.owned = home.atTrial = true;
        home.groupReleased = false;
        home.released = (bits & 1) != 0;
        home.resetDone = (bits & 2) != 0;
        home.leaderRestored = (bits & 4) != 0;
        home.finalResetDone = (bits & 8) != 0;
        CHECK(DecideDummyReturn(home) != DummyReturnStep::Teleport);
        CHECK(DecideDummyReturn(home) != DummyReturnStep::Complete);
    }
    DummyReturnState done;
    done.prepared = done.connected = done.sameSession = done.online = done.owned = done.atTrial = true;
    done.released = done.resetDone = true;
    CHECK(DecideDummyReturn(done) == DummyReturnStep::Teleport); // Every release done: now it may go home.
}

LEEROY_TEST(Training_DummyMoveFormsThePartyAtTheDummyThenWaitsForTheStockReset)
{
    DummyMoveState state{false, true, true, false, true, false, false, 0};
    CHECK(DecideDummyMove({true, false, false, true, false, false, false, 0}) == DummyMoveStep::Wait); // Teleporting.
    auto with = [&state](auto change) { auto copy = state; change(copy); return DecideDummyMove(copy); };
    CHECK(with([](DummyMoveState& s) { s.owned = false; }) == DummyMoveStep::Block);
    CHECK(with([](DummyMoveState& s) { s.alive = false; }) == DummyMoveStep::Died);
    CHECK(with([](DummyMoveState& s) { s.inCombat = true; }) == DummyMoveStep::Attacked);
    CHECK(with([](DummyMoveState& s) { s.atTrial = false; }) == DummyMoveStep::Wait);
    CHECK(DecideDummyMove(state) == DummyMoveStep::Setup);
    state.setUp = true;
    state.sinceSetupMs = kDummySettleMs - 1;
    CHECK(DecideDummyMove(state) == DummyMoveStep::Settle); // The stock reset has not come yet.
    state.reasserted = true;
    CHECK(DecideDummyMove(state) == DummyMoveStep::Arrived); // It came and the driver is back: fight now.
    state.reasserted = false;
    state.sinceSetupMs = kDummySettleMs;
    CHECK(DecideDummyMove(state) == DummyMoveStep::Arrived); // No reset within the bound: fight anyway.
    CHECK(kDummySettleMs < kDummyMoveMs); // Settling fits the move deadline.
    state.atTrial = false;
    CHECK(DecideDummyMove(state) == DummyMoveStep::Wait); // Wandered off before the fight: not arrived.
}

LEEROY_TEST(Training_DummyOwnerMissingNamesEachFailedFact)
{
    DummyOwnerState proven{true, false, true, false, true, false, false, true, true, false};
    CHECK(DummyOwnerProven(proven));
    CHECK_EQ(DummyOwnerMissing(proven), "");
    struct Case { bool DummyOwnerState::*field; char const* name; };
    for (auto [field, name] : {Case{&DummyOwnerState::sameSession, "session"}, Case{&DummyOwnerState::isHuman, "human"},
        Case{&DummyOwnerState::isCast, "cast"}, Case{&DummyOwnerState::isSelfBot, "selfbot"},
        Case{&DummyOwnerState::sameGuild, "guild"}, Case{&DummyOwnerState::invited, "invite"},
        Case{&DummyOwnerState::reserved, "reserved"}, Case{&DummyOwnerState::groupOwned, "group"},
        Case{&DummyOwnerState::masterOwned, "master"}, Case{&DummyOwnerState::loggingOut, "logout"}})
    {
        auto changed = proven;
        changed.*field = !(changed.*field);
        CHECK(!DummyOwnerProven(changed));
        CHECK_EQ(DummyOwnerMissing(changed), name);
    }
    auto both = proven;
    both.groupOwned = false;
    both.masterOwned = false;
    CHECK_EQ(DummyOwnerMissing(both), "group,master");
}
