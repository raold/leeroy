// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyDummyLifecycle.h"
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
