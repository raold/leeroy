// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmContinuations.h"
#include "LeeroyRealmIdentity.h"
#include "TestHarness.h"
#include <set>

using namespace Leeroy::Realm;
namespace
{
std::string const Nonce = "0123456789abcdef0123456789abcdef";
PvpOwner Prepare(ContinuationLedger& ledger, std::string id = "queue-one", std::size_t total = 1)
{
    std::string error;
    auto owner = ledger.Prepare(std::move(id), PvpKind::RatedArena, {102, 101}, 7, 30, 1, total, error);
    CHECK(owner.has_value()); CHECK(error.empty());
    return owner.value();
}
ContinuationCleanReceipt Cleanup(ContinuationLedger& ledger, PvpOwner const& owner, std::string id = "cleanup-one")
{
    std::string error;
    CHECK(ledger.RequestCleanup(owner.token, std::move(id), {101, 102}, true, 30, 1, error));
    auto cleaning = ledger.Get(owner.token).value();
    return {cleaning.token, cleaning.cleanupId, cleaning.revision, cleaning.members};
}
}

LEEROY_TEST(RealmContinuations_PrepareFreezesManifestAuthorityAndBootIdentity)
{
    ContinuationLedger first(Nonce), second("fedcba9876543210fedcba9876543210");
    auto owner = Prepare(first);
    CHECK((owner.members == std::vector<std::uint32_t>{101, 102}));
    CHECK(ContinuationLedger::ValidToken(owner.token));
    CHECK(Prepare(second).token != owner.token);
    std::string error;
    auto retry = first.Prepare("queue-one", PvpKind::RatedArena, {101, 102}, 7, 30, 1, 1, error);
    CHECK(retry && retry->token == owner.token);
    CHECK_EQ(first.Size(), std::size_t{1});
    CHECK(!first.Prepare("queue-one", PvpKind::RatedArena, {101, 103}, 7, 30, 1, 1, error));
    CHECK_EQ(error, std::string("busy:duplicate_operation"));
    CHECK(!first.Prepare("queue-one", PvpKind::RatedArena, {101, 102}, 8, 30, 1, 1, error));
    CHECK(!first.Prepare("duplicate-body", PvpKind::RatedArena, {101, 101}, 7, 30, 1, 2, error));
    CHECK(!first.Prepare("zero-body", PvpKind::RatedArena, {101, 0}, 7, 30, 1, 2, error));
    ContinuationLedger unavailable("");
    CHECK(!unavailable.Prepare("new", PvpKind::RatedArena, {101}, 7, 30, 1, 1, error));
    CHECK_EQ(error, std::string("owner_token_unavailable"));
}

LEEROY_TEST(RealmContinuations_DetachedAndUnknownConsumeTheSameReservationCapacity)
{
    ContinuationLedger ledger(Nonce);
    // 300 ordinary reservations and 200 continuing ones fill one shared cap.
    for (std::size_t index = 0; index < 200; ++index)
    {
        auto owner = Prepare(ledger, "queue-" + std::to_string(index), 301 + index);
        CHECK(ledger.Detach(owner.token));
        if (index % 2) ledger.Unknown(owner.token, "body_identity_changed");
    }
    std::string error;
    CHECK(!ledger.Prepare("overflow", PvpKind::Wintergrasp, {103}, 7, 30, 1, 501, error));
    CHECK_EQ(error, std::string("busy:queue_full"));
    CHECK_EQ(ledger.Size(), std::size_t{200});
    // Waiting, fencing and repeated observations never age unknown owners out.
    ledger.FenceAll("realm_off");
    for (unsigned tick = 0; tick < 1000; ++tick) ledger.Visit(20, [](auto const&) { });
    CHECK_EQ(ledger.Size(), std::size_t{200});
}

LEEROY_TEST(RealmContinuations_DisableFencesApplicationAndRequiresOriginalAuthorityForCleanup)
{
    ContinuationLedger ledger(Nonce);
    auto owner = Prepare(ledger);
    CHECK(ledger.Detach(owner.token));
    CHECK(ledger.Validate(owner.token, ContinuationMode::Application, true, 7, 30, 1).empty());
    ledger.FenceAll("realm_off");
    CHECK_EQ(ledger.Size(), std::size_t{1});
    auto disabled = ledger.Get(owner.token).value();
    CHECK_EQ(disabled.reason, std::string("realm_off"));
    CHECK(disabled.applicationFenced);
    CHECK(disabled.members == owner.members);
    CHECK_EQ(ledger.FindOrigin(owner.originId)->token, owner.token);
    CHECK_EQ(ledger.Validate(owner.token, ContinuationMode::Application, true, 8, 30, 1),
        std::string("busy:application_fenced"));
    std::string error;
    CHECK(!ledger.RequestCleanup(owner.token, "cleanup", {101, 102}, false, 30, 1, error));
    CHECK_EQ(error, std::string("realm_off"));
    CHECK(!ledger.RequestCleanup(owner.token, "cleanup", {101, 102}, true, 31, 1, error));
    CHECK_EQ(error, std::string("busy:owner_authority"));
    CHECK(!ledger.RequestCleanup(owner.token, "cleanup", {101, 102}, true, 30, 2, error));
    CHECK(ledger.RequestCleanup(owner.token, "cleanup", {102, 101}, true, 30, 1, error));
    CHECK(ledger.Validate(owner.token, ContinuationMode::Cleanup, true, 8, 30, 1).empty());
    CHECK(!ledger.Validate(owner.token, ContinuationMode::Application, true, 8, 30, 1).empty());
}

LEEROY_TEST(RealmContinuations_CleanupRequiresWholeExactOwnerAndOneController)
{
    ContinuationLedger ledger(Nonce);
    auto owner = Prepare(ledger), other = Prepare(ledger, "queue-two", 2);
    std::string error;
    for (auto const& members : {std::vector<std::uint32_t>{101}, {101, 103}, {101, 102, 102}, {101, 102, 103}})
    {
        CHECK(!ledger.RequestCleanup(owner.token, "cleanup", members, true, 30, 1, error));
        CHECK_EQ(error, std::string("bad field: cleanup_manifest"));
        CHECK(!ledger.Get(owner.token)->applicationFenced);
    }
    auto proof = Cleanup(ledger, owner);
    CHECK(ledger.RequestCleanup(owner.token, "cleanup-one", {102, 101}, true, 30, 1, error));
    CHECK_EQ(ledger.Get(owner.token)->revision, proof.revision);
    CHECK(!ledger.RequestCleanup(owner.token, "another-controller", {101, 102}, true, 30, 1, error));
    CHECK_EQ(error, std::string("busy:cleanup_controller"));
    CHECK(!ledger.RequestCleanup(other.token, "cleanup-one", {101, 102}, true, 30, 1, error));
    CHECK_EQ(error, std::string("busy:duplicate_operation"));
}

LEEROY_TEST(RealmContinuations_StalePartialForeignAndGuardlessProofCannotRelease)
{
    ContinuationLedger ledger(Nonce);
    auto owner = Prepare(ledger);
    auto proof = Cleanup(ledger, owner);
    CHECK(!ledger.CanRelease(proof, false));
    CHECK(ledger.CanRelease(proof, true));
    auto bad = proof; --bad.revision; CHECK(!ledger.Commit(bad));
    bad = proof; bad.members.pop_back(); CHECK(!ledger.Commit(bad));
    bad = proof; bad.members.push_back(103); CHECK(!ledger.Commit(bad));
    bad = proof; bad.cleanupId = "other-cleanup"; CHECK(!ledger.Commit(bad));
    bad = proof; bad.token[0] = 'f'; CHECK(!ledger.Commit(bad));
    CHECK_EQ(ledger.Size(), std::size_t{1});
    CHECK(ledger.Commit(proof));
    CHECK_EQ(ledger.Size(), std::size_t{0});
    CHECK(ledger.Get(owner.token)->stage == PvpStage::Clean);
    CHECK(!ledger.Commit(proof));
    std::string error;
    CHECK(ledger.RequestCleanup(owner.token, "cleanup-one", {102, 101}, true, 30, 1, error));
    CHECK(!ledger.Prepare("queue-one", PvpKind::RatedArena, {101, 102}, 7, 30, 1, 1, error));
    CHECK_EQ(error, std::string("operation_receipt_available"));
    CHECK(ledger.CommandIdReserved("queue-one")); CHECK(ledger.CommandIdReserved("cleanup-one"));
}

LEEROY_TEST(RealmContinuations_UnknownIsAbsorbingEvenAfterFencingAndCleanupRetry)
{
    ContinuationLedger ledger(Nonce);
    auto owner = Prepare(ledger);
    auto proof = Cleanup(ledger, owner);
    ledger.Unknown(owner.token, "body_identity_changed");
    auto unknown = ledger.Get(owner.token).value();
    CHECK(unknown.applicationFenced); CHECK(unknown.stage == PvpStage::Unknown);
    CHECK(!ledger.Commit(proof));
    CHECK(!ledger.Advance(owner.token, unknown.revision, PvpStage::Active));
    CHECK(!ledger.Advance(owner.token, unknown.revision, PvpStage::Cleaning, "worldport_pending"));
    ledger.FenceAll("realm_off");
    std::string error;
    CHECK(!ledger.RequestCleanup(owner.token, "retry", {101, 102}, true, 30, 1, error));
    CHECK_EQ(error, std::string("busy:owner_unknown"));
    CHECK_EQ(ledger.Size(), std::size_t{1});
    CHECK_EQ(ledger.Get(owner.token)->reason, std::string("body_identity_changed"));
    CHECK_EQ(ledger.FindOrigin(owner.originId)->token, owner.token);
    CHECK(ledger.Get(owner.token)->members == owner.members);
}

LEEROY_TEST(RealmContinuations_DelayedCleanupWaitsAndRequiresFreshProof)
{
    ContinuationLedger ledger(Nonce);
    auto owner = Prepare(ledger);
    CHECK(ledger.Detach(owner.token));
    auto original = Cleanup(ledger, owner);
    CHECK(ledger.Advance(owner.token, original.revision, PvpStage::Cleaning, "busy:tick_budget"));
    auto waiting = ledger.Get(owner.token).value();
    CHECK(waiting.stage == PvpStage::Cleaning);
    CHECK(waiting.applicationFenced);
    CHECK_EQ(waiting.cleanupId, original.cleanupId);
    CHECK(waiting.members == original.members);
    CHECK_EQ(waiting.revision, original.revision + 1);
    CHECK_EQ(waiting.reason, std::string("busy:tick_budget"));
    CHECK_EQ(ledger.Size(), std::size_t{1});
    CHECK(ledger.IsDetached(owner.token));
    CHECK(!ledger.CanRelease(original, true));
    CHECK(!ledger.Commit(original));

    CHECK(ledger.Advance(owner.token, waiting.revision, PvpStage::Cleaning, "busy:tick_budget"));
    CHECK_EQ(ledger.Get(owner.token)->revision, waiting.revision);
    CHECK(!ledger.Advance(owner.token, original.revision, PvpStage::Cleaning, "worldport_pending"));
    CHECK_EQ(ledger.Get(owner.token)->reason, waiting.reason);
    CHECK(ledger.Advance(owner.token, waiting.revision, PvpStage::Cleaning, "worldport_pending"));
    auto ready = ledger.Get(owner.token).value();
    CHECK_EQ(ready.revision, waiting.revision + 1);
    CHECK_EQ(ready.reason, std::string("worldport_pending"));
    CHECK(!ledger.Advance(owner.token, ready.revision, PvpStage::Active));
    CHECK(!ledger.Advance(owner.token, ready.revision, PvpStage::Clean));
    CHECK(!ledger.CanRelease(original, true));
    CHECK(!ledger.Commit(original));

    ContinuationCleanReceipt fresh{ready.token, ready.cleanupId, ready.revision, ready.members};
    CHECK(!ledger.CanRelease(fresh, false));
    CHECK(ledger.CanRelease(fresh, true));
    CHECK(ledger.Commit(fresh));
    CHECK_EQ(ledger.Size(), std::size_t{0});
    CHECK(ledger.Get(owner.token)->stage == PvpStage::Clean);
    CHECK_EQ(ledger.Get(owner.token)->revision, ready.revision + 1);
    CHECK(ledger.Get(owner.token)->reason.empty());
    CHECK(!ledger.Advance(owner.token, ready.revision + 1, PvpStage::Cleaning, "busy:tick_budget"));
}

LEEROY_TEST(RealmContinuations_WaitDiagnosticRevisionTracksBoundedObservableChange)
{
    ContinuationLedger ledger(Nonce);
    auto owner = Prepare(ledger);
    std::string const longReason(200, 'x');
    CHECK(ledger.Advance(owner.token, owner.revision, owner.stage, longReason));
    auto waiting = ledger.Get(owner.token).value();
    CHECK_EQ(waiting.reason, longReason.substr(0, 128));
    CHECK_EQ(waiting.revision, owner.revision + 1);
    CHECK(ledger.Advance(owner.token, waiting.revision, waiting.stage, longReason + "different suffix"));
    CHECK_EQ(ledger.Get(owner.token)->revision, waiting.revision);
    CHECK(ledger.Advance(owner.token, waiting.revision, waiting.stage));
    CHECK_EQ(ledger.Get(owner.token)->revision, waiting.revision + 1);
    CHECK(ledger.Get(owner.token)->reason.empty());
    ledger.FenceAll("realm_off");
    auto fenced = ledger.Get(owner.token).value();
    CHECK(!ledger.Advance(owner.token, fenced.revision, fenced.stage, "busy:tick_budget"));
    CHECK_EQ(ledger.Get(owner.token)->revision, fenced.revision);
    CHECK_EQ(ledger.Get(owner.token)->reason, fenced.reason);

    auto proof = Cleanup(ledger, owner);
    CHECK(ledger.Advance(owner.token, proof.revision, PvpStage::Cleaning, longReason));
    auto cleaning = ledger.Get(owner.token).value();
    CHECK_EQ(cleaning.reason, longReason.substr(0, 128));
    CHECK_EQ(cleaning.revision, proof.revision + 1);
    CHECK(ledger.Advance(owner.token, cleaning.revision, PvpStage::Cleaning, longReason + "another suffix"));
    CHECK_EQ(ledger.Get(owner.token)->revision, cleaning.revision);
    CHECK(ledger.Advance(owner.token, cleaning.revision, PvpStage::Cleaning));
    CHECK_EQ(ledger.Get(owner.token)->revision, cleaning.revision + 1);
    CHECK(ledger.Get(owner.token)->reason.empty());
    CHECK(!ledger.CanRelease(proof, true));
}

LEEROY_TEST(RealmContinuations_PreDisableControllerNeverBecomesFreshByRetry)
{
    ContinuationLedger ledger(Nonce);
    auto owner = Prepare(ledger);
    auto old = Cleanup(ledger, owner, "old-controller");
    ledger.FenceAll("realm_off");
    CHECK(!ledger.Commit(old));
    std::string error;
    CHECK(!ledger.RequestCleanup(owner.token, "old-controller", {101, 102}, true, 30, 1, error));
    CHECK_EQ(error, std::string("busy:duplicate_operation"));
    CHECK(ledger.CommandIdReserved("old-controller"));
    CHECK(ledger.RequestCleanup(owner.token, "fresh-controller", {101, 102}, true, 30, 1, error));
    CHECK(!ledger.Commit(old));
    CHECK(ledger.Get(owner.token)->cleanupId == "fresh-controller");
}

LEEROY_TEST(RealmContinuations_CanceledControllerHistoryExhaustionRetainsUnknown)
{
    ContinuationLedger ledger(Nonce);
    auto owner = Prepare(ledger);
    for (unsigned boundary = 0; boundary < 17; ++boundary)
    {
        Cleanup(ledger, owner, "cleanup-" + std::to_string(boundary));
        ledger.FenceAll("realm_off");
    }
    CHECK(ledger.Get(owner.token)->stage == PvpStage::Unknown);
    CHECK_EQ(ledger.Get(owner.token)->reason, std::string("cleanup_history_exhausted"));
    CHECK_EQ(ledger.Size(), std::size_t{1});
}

LEEROY_TEST(RealmContinuations_ObservationAndFullClosureMutationBothRemainFair)
{
    for (std::size_t count : {std::size_t{3}, std::size_t{43}, ActionQueue::Capacity})
    {
        ContinuationLedger ledger(Nonce);
        for (std::size_t index = 0; index < count; ++index)
        { auto owner = Prepare(ledger, "queue-" + std::to_string(index), index + 1); ledger.Detach(owner.token); }
        std::set<std::string> observed, mutated;
        for (std::size_t tick = 0; tick < count; ++tick)
        {
            TickBudget budget(5);
            std::set<std::string> thisTick;
            ledger.Visit(1000, [&](auto const& owner)
            {
                CHECK(thisTick.insert(owner.token).second);
                observed.insert(owner.token);
                if (budget.Take(5)) mutated.insert(owner.token);
            });
            CHECK(thisTick.size() <= 20);
            CHECK_EQ(budget.Left(), 0u);
        }
        CHECK_EQ(observed.size(), count); CHECK_EQ(mutated.size(), count);
    }
}

LEEROY_TEST(RealmContinuations_ParkingNeverBlocksTheOrdinaryFifo)
{
    ContinuationLedger ledger(Nonce);
    ActionQueue queue;
    CHECK(queue.Push({"queue-one", "arena_queue", {101, 102}, 0, nlohmann::json::object()}));
    CHECK(queue.Push({"move-next", "realm_move", {103}, 0, nlohmann::json::object()}));
    auto owner = Prepare(ledger, "queue-one", 2);
    TickBudget budget(5);
    std::vector<std::string> ran;
    queue.Drain(budget, [&](auto& entry)
    {
        ran.push_back(entry.id);
        if (entry.id == owner.originId) CHECK(ledger.Detach(owner.token));
        return true;
    });
    CHECK((ran == std::vector<std::string>{"queue-one", "move-next"}));
    CHECK_EQ(queue.Size(), std::size_t{0});
    CHECK_EQ(ledger.Size(), std::size_t{1});
    CHECK(ledger.IsDetached(owner.token));
    unsigned observations = 0;
    ledger.Visit(20, [&](auto const&) { ++observations; });
    CHECK_EQ(observations, 1u); CHECK_EQ(budget.Left(), 3u);
}

LEEROY_TEST(RealmContinuations_OrdinaryLaneCannotStarveEveryOtherDetachedOwner)
{
    ContinuationLedger ledger(Nonce);
    for (unsigned index = 0; index < 8; ++index)
    { auto owner = Prepare(ledger, "queue-" + std::to_string(index), index + 1); ledger.Detach(owner.token); }
    std::set<std::string> mutated;
    for (unsigned tick = 0; tick < 16; ++tick)
    {
        TickBudget budget(5);
        if (tick % 2) CHECK(budget.Take(5)); // A full ordinary-lane tick.
        bool const advance = budget.Left() == budget.Cap();
        ledger.Visit(20, [&](auto const& owner)
        { if (budget.Take(5)) mutated.insert(owner.token); }, advance);
    }
    CHECK_EQ(mutated.size(), std::size_t{8});
}

LEEROY_TEST(RealmContinuations_TerminalReceiptsBoundedWithoutEvictingLiveOwners)
{
    ContinuationLedger ledger(Nonce);
    auto retained = Prepare(ledger, "retained");
    ledger.Unknown(retained.token, "unknown");
    std::string firstToken;
    for (std::size_t index = 0; index <= ActionQueue::Capacity; ++index)
    {
        auto owner = Prepare(ledger, "queue-" + std::to_string(index), 2);
        if (!index) firstToken = owner.token;
        CHECK(ledger.Commit(Cleanup(ledger, owner, "cleanup-" + std::to_string(index))));
    }
    CHECK_EQ(ledger.TerminalSize(), ActionQueue::Capacity);
    CHECK_EQ(ledger.Size(), std::size_t{1});
    CHECK(!ledger.Get(firstToken)); CHECK(ledger.Get(retained.token)->stage == PvpStage::Unknown);
    CHECK(!ledger.Get(Nonce + "-ffffffffffffffff"));
}

LEEROY_TEST(RealmContinuations_AtomicClosureBudgetCannotSpendPartialRemainder)
{
    TickBudget budget(5);
    CHECK(budget.Take(2)); CHECK(!budget.Take(4)); CHECK_EQ(budget.Left(), 3u);
    CHECK(budget.Take(3)); CHECK_EQ(budget.Left(), 0u); CHECK(!budget.Take(1));
    budget.Reset(); CHECK_EQ(budget.Left(), 5u); CHECK(!budget.Take(6)); CHECK_EQ(budget.Left(), 5u);
}

LEEROY_TEST(RealmContinuations_DeclaredScopeNeverAdoptsAReplacementBodyOrLogin)
{
    int playerStorage = 0, replacementPlayer = 0, aiStorage = 0, replacementAi = 0;
    int sessionStorage = 0, replacementSession = 0;
    BodyIdentity saved{20, 30, 571, 0, 0, 8, 1, 0, reinterpret_cast<Player*>(&playerStorage),
        reinterpret_cast<PlayerbotAI*>(&aiStorage), reinterpret_cast<WorldSession*>(&sessionStorage)};
    auto entered = saved; entered.map = 559; entered.instance = 44; entered.group = 0x100000001;
    std::vector<BodyScope> scopes{Scope(entered)};
    CHECK(CheckBodyLease(saved, 9, saved, 9, {}) == LeaseCheck::Exact);
    CHECK(CheckBodyLease(saved, 9, entered, 9, {}) == LeaseCheck::ScopeChanged);
    CHECK(CheckBodyLease(saved, 9, entered, 9, scopes) == LeaseCheck::Exact);
    CHECK(CheckBodyLease(saved, 9, entered, 10, scopes) == LeaseCheck::LifeChanged);
    CHECK(CheckBodyLease(saved, 9, std::nullopt, 9, scopes) == LeaseCheck::LifeChanged);
    auto replaced = entered; replaced.player = reinterpret_cast<Player*>(&replacementPlayer);
    CHECK(CheckBodyLease(saved, 9, replaced, 9, scopes) == LeaseCheck::LifeChanged);
    replaced = entered; replaced.ai = reinterpret_cast<PlayerbotAI*>(&replacementAi);
    CHECK(CheckBodyLease(saved, 9, replaced, 9, scopes) == LeaseCheck::LifeChanged);
    replaced = entered; replaced.session = reinterpret_cast<WorldSession*>(&replacementSession);
    CHECK(CheckBodyLease(saved, 9, replaced, 9, scopes) == LeaseCheck::LifeChanged);
    replaced = entered; ++replaced.account;
    CHECK(CheckBodyLease(saved, 9, replaced, 9, scopes) == LeaseCheck::LifeChanged);
    replaced = entered; ++replaced.guild;
    CHECK(CheckBodyLease(saved, 9, replaced, 9, scopes) == LeaseCheck::LifeChanged);
    replaced = entered; ++replaced.race;
    CHECK(CheckBodyLease(saved, 9, replaced, 9, scopes) == LeaseCheck::LifeChanged);
    replaced = entered; ++replaced.classId;
    CHECK(CheckBodyLease(saved, 9, replaced, 9, scopes) == LeaseCheck::LifeChanged);
    replaced = entered; ++replaced.sex;
    CHECK(CheckBodyLease(saved, 9, replaced, 9, scopes) == LeaseCheck::LifeChanged);
}

LEEROY_TEST(RealmContinuations_ScopeProofUsesFullGroupAndInstanceAndRemainsBounded)
{
    BodyIdentity saved{20, 30, 571, 0, 0};
    auto intended = saved; intended.map = 559; intended.instance = 44; intended.group = 0x100000001;
    std::vector<BodyScope> scopes{Scope(intended)};
    auto foreign = intended; foreign.group = 0x200000001; // Same low counter, different full native GUID.
    CHECK(CheckBodyLease(saved, 1, foreign, 1, scopes) == LeaseCheck::ScopeChanged);
    foreign = intended; ++foreign.instance;
    CHECK(CheckBodyLease(saved, 1, foreign, 1, scopes) == LeaseCheck::ScopeChanged);
    foreign = intended; ++foreign.map;
    CHECK(CheckBodyLease(saved, 1, foreign, 1, scopes) == LeaseCheck::ScopeChanged);
    scopes.resize(5, Scope(intended));
    CHECK(CheckBodyLease(saved, 1, intended, 1, scopes) == LeaseCheck::ScopeChanged);
    CHECK_EQ(saved.group, std::uint64_t{0}); // Declaration never changes original identity.
}
