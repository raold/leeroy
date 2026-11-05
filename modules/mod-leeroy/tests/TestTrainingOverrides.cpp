// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyTrainingOverrides.h"
#include "TestHarness.h"
#include <limits>

using namespace Leeroy::Training;

LEEROY_TEST(Training_OverrideUndoRestoresPriorAndNeverClearsUnrelatedKeys)
{
    TrainingOverrides journal;
    CHECK(journal.NoteApplied("WaitAtBoss", {true, 0}, {true, 1}));
    CHECK(journal.NoteApplied("PartyMaxSpread", {false, 40}, {true, 50}));
    auto hold = journal.Undo("WaitAtBoss", {true, 1});
    CHECK(hold.action == OverrideUndo::Restore);
    CHECK_EQ(hold.value, 0);
    CHECK(journal.Undo("PartyMaxSpread", {true, 50}).action == OverrideUndo::Remove);
    CHECK(journal.Undo("OtherModuleSetting", {true, 75}).action == OverrideUndo::Unowned);
    CHECK(journal.Keys() == std::vector<std::string>({"PartyMaxSpread", "WaitAtBoss"}));
    CHECK(!journal.Empty()); // Planning an undo has not consumed ownership evidence.
    journal.Forget("WaitAtBoss");
    CHECK(journal.Undo("WaitAtBoss", {true, 1}).action == OverrideUndo::Unowned);
    CHECK(!journal.Empty());
}

LEEROY_TEST(Training_OverrideLaterExternalReplacementIsPreserved)
{
    TrainingOverrides journal;
    CHECK(journal.NoteApplied("PartyMaxSpread", {false, 40}, {true, 50}));
    CHECK(journal.Undo("PartyMaxSpread", {true, 60}).action == OverrideUndo::PreserveExternal);
    CHECK(journal.Undo("PartyMaxSpread", {false, 40}).action == OverrideUndo::PreserveExternal);
    CHECK(!journal.CanChange("PartyMaxSpread", {true, 60}));
    CHECK(!journal.ScaleBase("PartyMaxSpread", {true, 60}));
    CHECK(!journal.NoteApplied("PartyMaxSpread", {true, 60}, {true, 70}));
    CHECK(journal.Undo("PartyMaxSpread", {true, 50}).action == OverrideUndo::Remove);
}

LEEROY_TEST(Training_OverrideScaleUsesOriginalBaselineAndNormalizedReadback)
{
    TrainingOverrides journal;
    CHECK(journal.ScaleBase("PullSafetyHpPct", {false, 31}) == std::optional<double>(31));
    CHECK(journal.NoteApplied("PullSafetyHpPct", {false, 31}, {true, 39})); // 38.75 normalized by registry.
    CHECK(journal.ScaleBase("PullSafetyHpPct", {true, 39}) == std::optional<double>(31));
    CHECK(journal.NoteApplied("PullSafetyHpPct", {true, 39}, {true, 47}));
    CHECK(journal.ScaleBase("PullSafetyHpPct", {true, 47}) == std::optional<double>(31));
    CHECK(journal.Undo("PullSafetyHpPct", {true, 39}).action == OverrideUndo::PreserveExternal);
    CHECK(journal.Undo("PullSafetyHpPct", {true, 47}).action == OverrideUndo::Remove);
}

LEEROY_TEST(Training_OverrideInvalidWriteCannotAcquireOwnership)
{
    TrainingOverrides journal;
    auto nan = std::numeric_limits<double>::quiet_NaN();
    auto inf = std::numeric_limits<double>::infinity();
    CHECK(!journal.NoteApplied("", {false, 1}, {true, 2}));
    CHECK(!journal.NoteApplied("WaitAtBoss", {false, nan}, {true, 1}));
    CHECK(!journal.NoteApplied("WaitAtBoss", {false, 0}, {true, inf}));
    CHECK(!journal.NoteApplied("WaitAtBoss", {false, 0}, {false, 0}));
    CHECK(journal.Empty());
    CHECK(journal.NoteApplied("WaitAtBoss", {false, 0}, {true, 1}));
    CHECK(journal.Undo("WaitAtBoss", {true, nan}).action == OverrideUndo::PreserveExternal);
    CHECK(journal.Undo("WaitAtBoss", {true, 1}).action == OverrideUndo::Remove);
}
