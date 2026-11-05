// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyTrainingProtocol.h"
#include "TestHarness.h"

using namespace Leeroy::Training;

LEEROY_TEST(Training_HoldWatchdogUsesElapsedBossHoldOnly)
{
    HoldState held{true, true, 1000, false};
    CHECK(!HoldShouldResume(held, 999));
    CHECK(!HoldShouldResume(held, 20999));
    CHECK(HoldShouldResume(held, 21000));
    held.resumed = true;
    CHECK(!HoldShouldResume(held, 90000));
    held.resumed = false;
    held.atBoss = false;
    CHECK(!HoldShouldResume(held, 90000));
    held.atBoss = true;
    held.paused = false;
    CHECK(!HoldShouldResume(held, 90000));
}

LEEROY_TEST(Training_PrimeDecisionDoesNotReplaceTakenDefinition)
{
    CHECK(PrimeEntry("", "entry") == PrimeDecision::Write);
    CHECK(PrimeEntry("entry", "entry") == PrimeDecision::Same);
    CHECK(PrimeEntry("other", "entry") == PrimeDecision::Taken);
}

LEEROY_TEST(Training_RunCleanupDistinguishesOwnedWipeRestartAndClockRollback)
{
    RunWatch run{true, true, false, false, true, 0};
    CHECK(!ShouldClearRun(run, 120000));
    run.dcEnabled = false;
    CHECK(!ShouldClearRun(run, 59999));
    CHECK(ShouldClearRun(run, 60000));
    run.dcOffSinceMs = 1000;
    CHECK(!ShouldClearRun(run, 999));
    run.jobOwned = true;
    CHECK(!ShouldClearRun(run, 120000));
    run.jobEnded = true;
    CHECK(ShouldClearRun(run, 120000));
    run.jobEnded = false;
    run.sameMap = false; // Runtime includes exact instance and group identity in this predicate.
    CHECK(ShouldClearRun(run, 1));
    run.sameMap = true;
    run.leaderOnline = false;
    CHECK(ShouldClearRun(run, 1));
}

LEEROY_TEST(Training_VocabPlanHasStableOrderAndNoClassSpellScan)
{
    GetVocabCmd command{{6, 8}, {1101}, {"often"}};
    auto jobs = PlanVocab(command, false);
    CHECK_EQ(jobs.size(), 4);
    CHECK(jobs[0].kind == VocabKind::Class && jobs[0].cls == 6 && !jobs[0].wantSpells);
    CHECK(jobs[1].kind == VocabKind::Class && jobs[1].cls == 8 && !jobs[1].wantSpells);
    CHECK(jobs[2].kind == VocabKind::Probe && jobs[2].probe == 1101 && jobs[2].wantSpells);
    CHECK(jobs[3].kind == VocabKind::Server && !jobs[3].wantSpells);
    CHECK(!PlanVocab(command, true)[2].wantSpells);
}
