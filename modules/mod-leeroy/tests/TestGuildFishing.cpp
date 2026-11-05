// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyGuildFishingProof.h"
#include "LeeroyRealmContinuations.h"
#include "TestHarness.h"
using namespace Leeroy::Guild;
using namespace Leeroy::Realm;

LEEROY_TEST(GuildFishing_ExactDispatchChannelAndBobberRequired)
{
    FishingProof proof;
    CHECK(!proof.Started()); // Pole equip has no native production receipt.
    CHECK(proof.Prepare(7, 0x1234));
    CHECK(!proof.Started());
    CHECK(proof.AddBobber(7, 0x1234, 901));
    CHECK(!proof.Started());
    proof.Cast(0x1234);
    CHECK(proof.Started());
    CHECK(!proof.CanHandBack(true, true, true, true));
    proof.sealed = true;
    CHECK(!proof.CanHandBack(true, true, true, true));
    proof.Remove(901);
    CHECK(proof.CanHandBack(true, true, true, true));
    CHECK(!proof.CanHandBack(false, true, true, true));
    CHECK(!proof.CanHandBack(true, false, true, true));
    CHECK(!proof.CanHandBack(true, true, false, true));
    CHECK(!proof.CanHandBack(true, true, true, false));
}
LEEROY_TEST(GuildFishing_PointerReuseAndForeignReceiptsPoison)
{
    FishingProof original;
    CHECK(original.Prepare(7, 0x1234));
    auto value = original;
    CHECK(!value.Prepare(8, 0x1234)); CHECK(value.poisoned);
    value = original; CHECK(!value.AddBobber(8, 0x1234, 901)); CHECK(value.poisoned);
    value = original; CHECK(!value.AddBobber(7, 0x1235, 901)); CHECK(value.poisoned);
    value = original; value.Cast(0x1235); CHECK(value.poisoned);
    value = original; value.Cancel(0x1235); CHECK(value.poisoned);
    value = original; CHECK(value.AddBobber(7, 0x1234, 901)); value.Remove(902); CHECK(value.poisoned);
}
LEEROY_TEST(GuildFishing_AsyncBobberCandidateCommitsOnlyAtExactPostCast)
{
    FishingProof proof;
    CHECK(proof.Prepare(11, 0x3456));
    // Native prepare returns before the later channel SpellEvent casts.
    CHECK(!proof.Started()); CHECK_EQ(proof.bobber, std::uint64_t{0});
    FishingCastCandidate candidate;
    CHECK(candidate.Open(proof, 0x3456));
    CHECK(candidate.Add(903));
    CHECK(!proof.Started()); CHECK_EQ(proof.bobber, std::uint64_t{0});
    CHECK(candidate.Commit(proof, 0x3456));
    CHECK(proof.Started()); CHECK_EQ(proof.bobber, std::uint64_t{903});
    CHECK(!candidate.Commit(proof, 0x3456)); CHECK(proof.poisoned);
}
LEEROY_TEST(GuildFishing_MissingMismatchedAndDuplicateCastFramesCannotAdoptBobber)
{
    FishingProof original;
    CHECK(original.Prepare(11, 0x3456));
    FishingCastCandidate candidate;
    CHECK(candidate.Open(original, 0x3456)); CHECK(candidate.Add(903));
    // A missing post hook leaves the object uncommitted, regardless of time.
    CHECK_EQ(original.bobber, std::uint64_t{0}); CHECK(!original.Started());
    auto proof = original;
    auto wrong = candidate;
    CHECK(!wrong.Commit(proof, 0x3457)); CHECK(proof.poisoned); CHECK_EQ(proof.bobber, std::uint64_t{0});
    proof = original; auto nested = candidate;
    CHECK(!nested.Add(904)); CHECK(!nested.Commit(proof, 0x3456)); CHECK(proof.poisoned);
    proof = original; auto reused = candidate;
    CHECK(!reused.Open(proof, 0x3456)); CHECK(!reused.Commit(proof, 0x3456)); CHECK(proof.poisoned);
    proof = original; proof.sealed = true;
    FishingCastCandidate sealed;
    CHECK(!sealed.Open(proof, 0x3456));
}
LEEROY_TEST(GuildFishing_SealedPreparationAndUnprovedMissingChannelCannotRelease)
{
    FishingProof value;
    value.sealed = true;
    CHECK(value.CanHandBack(true, true, true, true)); // No preparation or mutation.
    CHECK(!value.Prepare(7, 0x1234)); CHECK(value.poisoned);
    value = {}; CHECK(value.Prepare(7, 0x1234)); value.sealed = true;
    CHECK(!value.CanHandBack(true, true, true, true));
    value.Cancel(0x1234);
    CHECK(value.CanHandBack(true, true, true, true));
    CHECK(FishingActionName("go fishing")); CHECK(FishingActionName("see spell"));
    CHECK(FishingActionName("move near water")); CHECK(FishingActionName("use fishing bobber"));
}
LEEROY_TEST(GuildFishing_DomainFenceDoesNotFenceOtherGuildActivity)
{
    ContinuationLedger ledger("0123456789abcdef0123456789abcdef");
    std::string error;
    auto realm = ledger.Prepare("realm-one", PvpKind::RatedArena, {201}, 8, 40, 1, 1, error);
    auto guild = ledger.Prepare("guild-one", PvpKind::Fishing, {202}, 3, 40, 1, 2, error, ActivityDomain::Guild);
    CHECK(realm && guild);
    ledger.FenceDomain(ActivityDomain::Realm, "realm_off");
    CHECK(!ledger.Validate(realm->token, ContinuationMode::Application, true, 9, 40, 1).empty());
    CHECK(ledger.Validate(guild->token, ContinuationMode::Application, true, 3, 40, 1).empty());
    CHECK(!ledger.Get(guild->token)->applicationFenced);
    CHECK(!ledger.Prepare("guild-pvp-bypass", PvpKind::RatedArena, {203}, 3, 40, 1, 3, error, ActivityDomain::Guild));
    CHECK(!ledger.RequestCleanup(guild->token, "wire-cleanup", {202}, true, 40, 1, error));
    CHECK_EQ(error, std::string("owner_unavailable"));
}
LEEROY_TEST(GuildFishing_InternalCleanupRequiresFreshWholeGuardedReceipt)
{
    ContinuationLedger ledger("0123456789abcdef0123456789abcdef");
    std::string error;
    auto owner = ledger.Prepare("guild-one", PvpKind::Fishing, {202, 203}, 3, 40, 1, 1, error, ActivityDomain::Guild).value();
    CHECK(ledger.Advance(owner.token, owner.revision, PvpStage::Cleaning));
    auto cleaning = ledger.Get(owner.token).value();
    CHECK(cleaning.internalCleanup); CHECK(cleaning.applicationFenced); CHECK(cleaning.cleanupId.empty());
    CHECK(ledger.Validate(owner.token, ContinuationMode::Cleanup, true, 4, 40, 1).empty());
    CHECK(!ledger.Validate(owner.token, ContinuationMode::Application, true, 4, 40, 1).empty());
    ContinuationCleanReceipt receipt{owner.token, "", cleaning.revision, cleaning.members};
    CHECK(!ledger.CanRelease(receipt, false)); CHECK(ledger.CanRelease(receipt, true));
    auto partial = receipt; partial.members.pop_back(); CHECK(!ledger.CanRelease(partial, true));
    auto foreign = receipt; foreign.cleanupId = "wire"; CHECK(!ledger.CanRelease(foreign, true));
    CHECK(!ledger.Validate(owner.token, ContinuationMode::Cleanup, true, 4, 41, 1).empty());
    ledger.FenceDomain(ActivityDomain::Guild, "guild_off");
    CHECK_EQ(ledger.Validate(owner.token, ContinuationMode::Cleanup, false, 4, 40, 1), std::string("guild_off"));
    CHECK(!ledger.CanRelease(receipt, true));
    auto resumed = ledger.Get(owner.token).value();
    receipt.revision = resumed.revision;
    CHECK(ledger.Validate(owner.token, ContinuationMode::Cleanup, true, 4, 40, 1).empty());
    CHECK(ledger.Commit(receipt));
    CHECK(ledger.CommandIdReserved("guild-one"));
}
LEEROY_TEST(GuildFishing_UnknownSurvivesDisableAndCannotBeInternallyCleaned)
{
    ContinuationLedger ledger("0123456789abcdef0123456789abcdef");
    std::string error;
    auto owner = ledger.Prepare("guild-one", PvpKind::Fishing, {202}, 3, 40, 1, 1, error, ActivityDomain::Guild).value();
    ledger.Unknown(owner.token, "missing-original-weapon");
    ledger.FenceDomain(ActivityDomain::Guild, "guild_off");
    auto retained = ledger.Get(owner.token).value();
    CHECK(retained.stage == PvpStage::Unknown);
    CHECK(!ledger.Advance(owner.token, retained.revision, PvpStage::Cleaning));
    CHECK_EQ(retained.reason, std::string("missing-original-weapon"));
}
LEEROY_TEST(GuildFishing_QueueDomainDropPreservesOtherDomainAndBudget)
{
    ActionQueue queue;
    CHECK(queue.Push({"realm-first", "move", {201}, 0, nlohmann::json::object()}));
    CHECK(queue.Push({"guild-second", "guild_fishing", {202}, 0, nlohmann::json::object(), ActivityDomain::Guild}));
    CHECK(queue.Push({"realm-third", "move", {203}, 0, nlohmann::json::object()}));
    unsigned dropped = 0, ran = 0;
    queue.DropIf([](auto const& entry) { return entry.domain == ActivityDomain::Realm; }, "realm_off",
        [&](auto const&, auto const&) { ++dropped; });
    CHECK_EQ(dropped, 2u); CHECK_EQ(queue.Size(), std::size_t{1});
    TickBudget budget(1);
    queue.Drain(budget, [&](auto& entry)
    { CHECK_EQ(entry.id, std::string("guild-second")); ++ran; return true; });
    CHECK_EQ(ran, 1u); CHECK_EQ(budget.Left(), 0u);
}
