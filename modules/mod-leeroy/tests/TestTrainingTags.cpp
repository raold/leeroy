// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyTrainingTags.h"
#include "LeeroyTrainingProtocol.h"
#include "TestHarness.h"

using namespace Leeroy::Training;

LEEROY_TEST(Training_RejectedBossDoneDoesNotClosePull)
{
    CHECK(ReviewBossState(true, false, 1) == BossRecordDecision::Confirm);
    CHECK(ReviewBossState(false, true, 1) == BossRecordDecision::Confirm); // DONE refused by living minion.
    CHECK(ReviewBossState(false, true, 3) == BossRecordDecision::Killed);
    CHECK(ReviewBossState(false, true, 2) == BossRecordDecision::Wiped);
    CHECK(ReviewBossState(false, true, 0) == BossRecordDecision::Wiped);
    CHECK(ReviewBossState(true, false, 0) == BossRecordDecision::Discard); // Start never committed.
    CHECK(ReviewBossState(true, false, 5) == BossRecordDecision::Discard); // Loading/unknown is not an engage.
    CHECK(ReviewBossState(false, false, 5) == BossRecordDecision::Retain); // No invented terminal outcome.
}

LEEROY_TEST(Training_PeriodicHealCannotLeakItsSpellIntoLaterDamage)
{
    TrainingTags tags;
    TagScope scope{77, 1, 0};
    tags.Damage(1, 2, scope, 50); // Periodic heal shares ModifyPeriodicDamageAurasTick.
    tags.Heal(1, 2, scope, 50, 400);
    auto heal = tags.TakeHeal(2, 1, scope); // HealBySpell reverses the pair.
    CHECK(heal.known);
    CHECK_EQ(heal.spell, 50);
    CHECK_EQ(heal.raw, 400);
    CHECK(!tags.TakeDamage(1, 2, scope).known);
    tags.Damage(1, 2, scope, 60);
    CHECK(!tags.TakeHeal(1, 2, scope).known); // Even an untagged heal invalidates pending damage attribution.
    CHECK(!tags.TakeDamage(1, 2, scope).known);
    tags.Heal(1, 2, scope, 50, 400);
    tags.Damage(1, 2, scope, 60);
    CHECK_EQ(tags.TakeDamage(1, 2, scope).spell, 60);
    CHECK(!tags.TakeHeal(1, 2, scope).known);
}

LEEROY_TEST(Training_SpellTagsAreSingleUseAndBoundToSlotGenerationAndPair)
{
    TrainingTags tags;
    TagScope scope{77, 1, 0};
    tags.Damage(1, 2, scope, 60);
    CHECK(tags.TakeDamage(2, 1, scope).known);
    CHECK(!tags.TakeDamage(1, 2, scope).known);
    for (auto wrong : {TagScope{78, 1, 0}, TagScope{77, 2, 0}, TagScope{77, 1, 1}})
    {
        tags.Damage(1, 2, scope, 60);
        CHECK(!tags.TakeDamage(1, 2, wrong).known);
        CHECK(!tags.TakeDamage(1, 2, scope).known);
    }
    tags.Damage(1, 2, scope, 60);
    CHECK(!tags.TakeDamage(1, 3, scope).known);
    tags.Damage(0, 2, scope, 60); // Despawned periodic caster is represented explicitly by zero.
    CHECK_EQ(tags.TakeDamage(0, 2, scope).spell, 60);
    tags.Clear();
    CHECK(!tags.TakeDamage(0, 2, scope).known);
}

LEEROY_TEST(Training_OffThenOnBeforeTickCannotResurrectPriorRecording)
{
    std::uint64_t telemetryEpoch = 0, trainingEpoch = 0;
    auto pullOpening = telemetryEpoch, dummyOpening = trainingEpoch;
    CHECK(RecordingStillCurrent(true, pullOpening, telemetryEpoch));
    ++telemetryEpoch; // Mind disarms, then rearms in the same world command batch before any Tick.
    CHECK(!RecordingStillCurrent(false, pullOpening, telemetryEpoch));
    CHECK(!RecordingStillCurrent(true, pullOpening, telemetryEpoch));
    CHECK(RecordingStillCurrent(true, telemetryEpoch, telemetryEpoch));
    CHECK(RecordingStillCurrent(true, dummyOpening, trainingEpoch)); // Passive telemetry is a separate control.
    ++trainingEpoch;
    CHECK(!RecordingStillCurrent(true, dummyOpening, trainingEpoch));
    CHECK(RecordingStillCurrent(true, telemetryEpoch, telemetryEpoch));
}
