// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmCraftProof.h"
#include "TestHarness.h"

using namespace Leeroy::Realm;
namespace
{
constexpr std::uintptr_t SpellKey = 81, OtherSpell = 82, ThreadKey = 91, OtherThread = 92;
CraftCastProof Prepared(std::uint64_t serial = 11)
{
    CraftCastProof cast(serial, {{{41001, 4}, {}, {}}}, 1);
    CHECK(cast.Prepare(SpellKey)); return cast;
}
CraftCastProof Casting(std::uint64_t serial = 11)
{
    auto cast = Prepared(serial); CHECK(cast.PreCast(SpellKey, ThreadKey)); return cast;
}
CraftCastProof Produced(std::uint64_t serial = 11)
{
    auto cast = Casting(serial); CHECK(cast.Created(ThreadKey, {1001, {41001, 4}}));
    CHECK(cast.PostCast(SpellKey, ThreadKey)); return cast;
}
CraftRecipeFacts Recipe()
{
    CraftRecipeFacts recipe;
    recipe.effects[0] = {CraftEffectKind::CreateItem, true, false, false, false, false, 3, 1, 0, 0, 41001, 20};
    return recipe;
}
}
LEEROY_TEST(RealmCraft_OnlyExactPreparePrecastItemPostcastCommits)
{
    auto cast = Prepared(); CHECK(cast.State() == CraftCastState::Prepared);
    cast.NativeReturned(true); CHECK(cast.State() == CraftCastState::Prepared);
    CHECK(cast.PreCast(SpellKey, ThreadKey)); CHECK(cast.Created(ThreadKey, {1001, {41001, 4}}));
    CHECK(cast.State() == CraftCastState::Casting); CHECK(cast.PostCast(SpellKey, ThreadKey));
    CHECK(cast.State() == CraftCastState::Complete); CHECK_EQ(cast.Total(), 4u);
    CHECK_EQ(cast.Items()[0].guid, 1001u);
}
LEEROY_TEST(RealmCraft_SameRecipeDifferentNativeSpellCannotBeAdopted)
{
    auto cast = Prepared(); CHECK(!cast.PreCast(OtherSpell, ThreadKey));
    CHECK(cast.State() == CraftCastState::Unknown);
    CHECK(!cast.PreCast(SpellKey, ThreadKey)); CHECK(!cast.PostCast(SpellKey, ThreadKey));
}
LEEROY_TEST(RealmCraft_ReusedPointerPoisonsEvenAProducedReceipt)
{
    auto cast = Prepared(); CHECK(!cast.Prepare(SpellKey));
    CHECK(cast.State() == CraftCastState::Unknown);
    cast = Produced(); CHECK(!cast.Prepare(SpellKey));
    CHECK(cast.State() == CraftCastState::Unknown);
}
LEEROY_TEST(RealmCraft_PrecastWithoutPreparationCannotOpenProduction)
{
    CraftCastProof cast(11, {{{41001, 4}, {}, {}}}, 1);
    CHECK(!cast.PreCast(SpellKey, ThreadKey)); CHECK(cast.State() == CraftCastState::Unknown);
}
LEEROY_TEST(RealmCraft_FailedPrepareHasASeparateNoEffectsTerminalProof)
{
    auto cast = Prepared(); cast.NativeReturned(false); CHECK(cast.State() == CraftCastState::Rejected);
    cast = Casting(); cast.NativeReturned(false); CHECK(cast.State() == CraftCastState::Unknown);
    cast = Produced(); cast.NativeReturned(false); CHECK(cast.State() == CraftCastState::Unknown);
}
LEEROY_TEST(RealmCraft_ExactPreparingCancellationCanFinishFailed)
{
    auto cast = Prepared(); CHECK(cast.Cancel(SpellKey, true));
    CHECK(cast.State() == CraftCastState::Cancelled);
    cast = Prepared(); CHECK(!cast.Cancel(OtherSpell, true)); CHECK(cast.State() == CraftCastState::Unknown);
    cast = Prepared(); CHECK(!cast.Cancel(SpellKey, false)); CHECK(cast.State() == CraftCastState::Unknown);
}
LEEROY_TEST(RealmCraft_CancellationAfterPrecastNeverClaimsNoProduction)
{
    auto cast = Casting(); CHECK(!cast.Cancel(SpellKey, true)); CHECK(cast.State() == CraftCastState::Unknown);
    cast = Casting(); CHECK(cast.Created(ThreadKey, {1001, {41001, 4}}));
    CHECK(!cast.Cancel(SpellKey, true)); CHECK(cast.State() == CraftCastState::Unknown);
}
LEEROY_TEST(RealmCraft_PartialBagStoreAndUnexpectedEntryRemainUnknown)
{
    auto cast = Casting(); CHECK(!cast.Created(ThreadKey, {1001, {41001, 3}}));
    CHECK(!cast.PostCast(SpellKey, ThreadKey)); CHECK(cast.State() == CraftCastState::Unknown);
    cast = Casting(); CHECK(!cast.Created(ThreadKey, {1001, {41002, 4}}));
    cast = Casting(); CHECK(!cast.Created(ThreadKey, {0, {41001, 4}}));
}
LEEROY_TEST(RealmCraft_ItemAndPostcastMustStayOnTheOpeningThread)
{
    auto cast = Casting(); CHECK(!cast.Created(OtherThread, {1001, {41001, 4}}));
    cast = Casting(); CHECK(cast.Created(ThreadKey, {1001, {41001, 4}}));
    CHECK(!cast.PostCast(SpellKey, OtherThread)); CHECK(cast.State() == CraftCastState::Unknown);
}
LEEROY_TEST(RealmCraft_MissingOrExtraEffectsCannotComplete)
{
    auto cast = Casting(); CHECK(!cast.PostCast(SpellKey, ThreadKey));
    cast = Casting(); CHECK(cast.Created(ThreadKey, {1001, {41001, 4}}));
    CHECK(!cast.Created(ThreadKey, {1002, {41001, 4}})); CHECK(!cast.PostCast(SpellKey, ThreadKey));
}
LEEROY_TEST(RealmCraft_MergedStacksKeepTheirExactGuidAndEachEffectCount)
{
    CraftCastProof cast(11, {{{41001, 4}, {41001, 2}, {}}}, 2);
    CHECK(cast.Prepare(SpellKey)); CHECK(cast.PreCast(SpellKey, ThreadKey));
    CHECK(cast.Created(ThreadKey, {1001, {41001, 4}}));
    CHECK(cast.Created(ThreadKey, {1001, {41001, 2}})); CHECK(cast.PostCast(SpellKey, ThreadKey));
    CHECK_EQ(cast.Total(), 6u); CHECK_EQ(cast.Items()[1].guid, 1001u);
}
LEEROY_TEST(RealmCraft_NestedOrNonplayerPreparationClosesTheOnlyCandidate)
{
    CraftCandidate frame; auto cast = Casting(); CHECK(frame.Open(cast.Dispatch()));
    CHECK(!frame.Open(12)); CHECK_EQ(frame.Boundary(), cast.Dispatch());
    cast.Poison("nested_preparation"); CHECK_EQ(frame.Dispatch(), 0u);
    CHECK(!cast.Created(ThreadKey, {1001, {41001, 4}}));
    CHECK(!cast.PostCast(SpellKey, ThreadKey)); CHECK(frame.Open(12));
}
LEEROY_TEST(RealmCraft_StaleCandidateStorageCannotGrowOrBeReopenedByLateHooks)
{
    CraftCandidate frame; CHECK(!frame.Open(0)); CHECK(frame.Open(11));
    for (std::uint64_t serial = 12; serial < 1012; ++serial)
    {
        CHECK_EQ(frame.Boundary(), serial - 1); CHECK(frame.Open(serial)); CHECK(!frame.Open(serial));
    }
    CHECK_EQ(frame.Boundary(), 1011u); CHECK_EQ(frame.Boundary(), 0u);
}
LEEROY_TEST(RealmCraft_LifecycleAndEpochPoisonCannotBeReversedByNativeSuccess)
{
    auto cast = Casting(); cast.Poison("body_lifetime_changed");
    CHECK(!cast.Created(ThreadKey, {1001, {41001, 4}}));
    CHECK(!cast.PostCast(SpellKey, ThreadKey)); cast.NativeReturned(true);
    CHECK(cast.State() == CraftCastState::Unknown); CHECK(cast.Reason() == "body_lifetime_changed");
}
LEEROY_TEST(RealmCraft_RequestedCastsAreDistinctFromProducedItemQuantity)
{
    CraftProgress progress(2); CHECK(progress.Record(Produced(11)));
    CHECK_EQ(progress.Casts(), 1u); CHECK_EQ(progress.Items(), 4u); CHECK(!progress.Complete());
    CHECK(progress.Record(Produced(12))); CHECK_EQ(progress.Casts(), 2u);
    CHECK_EQ(progress.Items(), 8u); CHECK(progress.Complete());
    CHECK(!progress.Record(Produced(13))); CHECK(!progress.Complete());
}
LEEROY_TEST(RealmCraft_ProgressRejectsDuplicateUnprovedAndCorruptSavedReceipts)
{
    CraftProgress progress(2); auto first = Produced(); CHECK(progress.Record(first));
    CHECK(!progress.Record(first)); CHECK(!progress.Complete());
    CraftProgress restored(2, 1, 4, 11); CHECK(restored.Record(Produced(12))); CHECK(restored.Complete());
    CraftProgress corrupt(2, 1, 0, 11); CHECK(!corrupt.Record(Produced(12)));
    CraftProgress unknown(1); CHECK(!unknown.Record(Casting()));
    CraftProgress zero(0); CHECK(!zero.Record(Produced()));
}
LEEROY_TEST(RealmCraft_FixedOutputPlanMatchesNativeMinimumAndStackClamp)
{
    auto recipe = Recipe(); auto plan = PlanCraftOutputs(recipe);
    CHECK(plan.has_value()); CHECK_EQ(plan->size, 1u); CHECK_EQ(plan->outputs[0].count, 4u);
    recipe.effects[0].basePoints = 0; recipe.effects[0].dieSides = 0;
    plan = PlanCraftOutputs(recipe); CHECK(plan.has_value()); CHECK_EQ(plan->outputs[0].count, 1u);
    recipe.effects[0].basePoints = 100; plan = PlanCraftOutputs(recipe);
    CHECK(plan.has_value()); CHECK_EQ(plan->outputs[0].count, 20u);
}
LEEROY_TEST(RealmCraft_CreateItem2RandomScriptTriggerAndDelayedShapesRefuseBeforeCast)
{
    auto recipe = Recipe(); recipe.effects[0].kind = CraftEffectKind::Other; CHECK(!PlanCraftOutputs(recipe));
    recipe = Recipe(); recipe.effects[1].kind = CraftEffectKind::Other; CHECK(!PlanCraftOutputs(recipe));
    recipe = Recipe(); recipe.effects[0].triggered = true; CHECK(!PlanCraftOutputs(recipe));
    recipe = Recipe(); recipe.scripted = true; CHECK(!PlanCraftOutputs(recipe));
    recipe = Recipe(); recipe.linked = true; CHECK(!PlanCraftOutputs(recipe));
    recipe = Recipe(); recipe.loot = true; CHECK(!PlanCraftOutputs(recipe));
    recipe = Recipe(); recipe.specialization = true; CHECK(!PlanCraftOutputs(recipe));
    recipe = Recipe(); recipe.missile = true; CHECK(!PlanCraftOutputs(recipe));
    recipe = Recipe(); recipe.channel = true; CHECK(!PlanCraftOutputs(recipe));
}
LEEROY_TEST(RealmCraft_VariableTargetAndOutputAmountsCannotMasqueradeAsFixedProduction)
{
    auto recipe = Recipe(); recipe.effects[0].selfOnly = false; CHECK(!PlanCraftOutputs(recipe));
    recipe = Recipe(); recipe.effects[0].dieSides = 2; CHECK(!PlanCraftOutputs(recipe));
    recipe = Recipe(); recipe.effects[0].dieSides = -1; CHECK(!PlanCraftOutputs(recipe));
    recipe = Recipe(); recipe.effects[0].perLevel = 1; CHECK(!PlanCraftOutputs(recipe));
    recipe = Recipe(); recipe.effects[0].perComboPoint = 1; CHECK(!PlanCraftOutputs(recipe));
    recipe = Recipe(); recipe.effects[0].outputModifier = true; CHECK(!PlanCraftOutputs(recipe));
    recipe = Recipe(); recipe.effects[0].conditional = true; CHECK(!PlanCraftOutputs(recipe));
    recipe = Recipe(); recipe.effects[0].chained = true; CHECK(!PlanCraftOutputs(recipe));
}
