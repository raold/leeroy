// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmLfgReceipts.h"
#include "LeeroyRealmCleanup.h"
#include "LeeroyRealmQueue.h"
#include "TestHarness.h"

using namespace Leeroy::Realm;
namespace
{
constexpr std::uint64_t Group = 0x1F50000000000101ull;
LfgObservedLife Life(std::uint32_t guid) { return {guid, 0x100000u + guid, 0x200000u + guid}; }
LfgRoleReceipt Packet(bool queued = false)
{
    return {queued ? LfgRoleReceipt::State::Finished : LfgRoleReceipt::State::Initial, 501, {502, 501}};
}
void Born(LfgReceiptBook& book, std::uint64_t group = Group, std::uint32_t first = 501)
{
    book.Born(group, group + 1, Life(first));
    book.Added(group, group + 1, Life(first + 1));
    CHECK(book.Join(group, group + 1, Life(first)));
}
void Complete(LfgReceiptBook& book, bool queued = false)
{
    book.Packet(Life(501), Packet(queued)); book.Packet(Life(502), Packet(queued));
}
LfgApplicantReceipt Ready(LfgReceiptBook& book, bool queued = false)
{
    book.SetEpoch(7); Born(book); Complete(book, queued);
    auto receipt = book.Proof(Group, queued); CHECK(receipt); return *receipt;
}
}

LEEROY_TEST(RealmLfgReceipts_RequiresBirthJoinAndFullActualProducerPackets)
{
    LfgReceiptBook book; book.SetEpoch(7);
    CHECK(book.Join(Group, Group + 1, Life(501)));
    Complete(book); CHECK(!book.Proof(Group, false));
    book.Born(Group, Group + 1, Life(501)); book.Added(Group, Group + 1, Life(502));
    Complete(book); CHECK(!book.Proof(Group, false));
    // Uncorrelated earlier packets retire that birth, so a fresh native group
    // is required. Packet recipients alone cannot manufacture Group lineage.
    Born(book); Complete(book); CHECK(book.Proof(Group, false));
}

LEEROY_TEST(RealmLfgReceipts_MissingOrRepeatedRecipientCannotComplete)
{
    LfgReceiptBook book; book.SetEpoch(7); Born(book);
    book.Packet(Life(501), Packet()); book.Packet(Life(501), Packet());
    CHECK(!book.Proof(Group, false));
    book.Packet(Life(502), Packet());
    auto proof = book.Proof(Group, false); CHECK(proof);
    CHECK(LfgReceiptGuids(*proof) == std::vector<std::uint32_t>({501, 502}));
}

LEEROY_TEST(RealmLfgReceipts_QueueNeedsEveryFinishedProducerPacket)
{
    LfgReceiptBook book; auto initial = Ready(book);
    CHECK(!book.Proof(Group, true));
    book.Packet(Life(501), Packet(true));
    CHECK(!book.Proof(Group, false)); CHECK(!book.Proof(Group, true));
    book.Packet(Life(502), Packet(true));
    auto queued = book.Proof(Group, true); CHECK(queued);
    CHECK_EQ(initial.join, queued->join); CHECK(queued->queued);
}

LEEROY_TEST(RealmLfgReceipts_RejectsForeignMissingDuplicateAndWrongLeaderLists)
{
    for (auto const& members : std::vector<std::vector<std::uint32_t>>{{501}, {501, 503}, {501, 501}, {}, {0, 502}})
    {
        LfgReceiptBook book; Ready(book);
        auto packet = Packet(); packet.members = members;
        book.Packet(Life(501), packet); CHECK(!book.Proof(Group, false));
        Complete(book); CHECK(!book.Proof(Group, false));
    }
    LfgReceiptBook book; Ready(book);
    auto packet = Packet(); packet.leader = 502;
    book.Packet(Life(501), packet); CHECK(!book.Proof(Group, false));
}

LEEROY_TEST(RealmLfgReceipts_ReusedBodyOrSessionAndLifecycleLoseProvenance)
{
    for (unsigned change = 0; change < 3; ++change)
    {
        LfgReceiptBook book; Ready(book);
        auto life = Life(501);
        if (change == 0) ++life.player;
        if (change == 1) ++life.session;
        if (change == 2) book.Lifecycle(501);
        book.Packet(life, Packet()); Complete(book);
        CHECK(!book.Proof(Group, false));
    }
}

LEEROY_TEST(RealmLfgReceipts_RejoinAndGroupObjectReuseCannotAdoptOldPackets)
{
    for (bool rejoin : {false, true})
    {
        LfgReceiptBook book;
        if (rejoin) Ready(book);
        else { book.SetEpoch(7); book.Born(Group, Group + 1, Life(501)); book.Added(Group, Group + 1, Life(502)); }
        CHECK(book.Join(Group, rejoin ? Group + 1 : Group + 2, Life(501)));
        Complete(book); CHECK(!book.Proof(Group, false));
    }
}

LEEROY_TEST(RealmLfgReceipts_GroupChangesAndInvitationsInvalidateWholeReceipt)
{
    for (unsigned change = 0; change < 3; ++change)
    {
        LfgReceiptBook book; Ready(book);
        if (change == 0) book.Added(Group, Group + 1, Life(503));
        if (change == 1) book.Changed(Group);
        if (change == 2) book.Invited(Group);
        Complete(book); CHECK(!book.Proof(Group, false));
    }
    LfgReceiptBook book; book.SetEpoch(7); book.Born(Group, Group + 1, Life(501));
    book.Invited(Group); // Native invitations before first Join do not add LFG players.
    book.Added(Group, Group + 1, Life(502)); CHECK(book.Join(Group, Group + 1, Life(501)));
    Complete(book); CHECK(book.Proof(Group, false));
}

LEEROY_TEST(RealmLfgReceipts_ProposalBeforeFenceInvalidatesWithoutGuessingRoster)
{
    LfgReceiptBook book; Ready(book, true);
    CHECK(book.Proposal({Group})); CHECK(!book.Proof(Group, true));
    Complete(book, true); CHECK(!book.Proof(Group, true));
}

LEEROY_TEST(RealmLfgReceipts_ExactFenceVetoesJoinAndProposalThroughHandback)
{
    LfgReceiptBook book; auto receipt = Ready(book, true);
    CHECK(book.Fence("cleanup-one", receipt));
    CHECK(!book.Join(Group, Group + 1, Life(501)));
    CHECK(!book.Join(Group + 9, Group + 10, Life(502)));
    CHECK(!book.Proposal({Group})); CHECK(!book.Proposal({}, {502}));
    CHECK(book.Verify("cleanup-one", receipt)); // Vetoed proposals never ran.
    CHECK(book.Proposal({Group + 5}, {504}));
    CHECK(book.Verify("cleanup-one", receipt));
    CHECK(!book.Fence("foreign-cleanup", receipt));
}

LEEROY_TEST(RealmLfgReceipts_AbortCapabilityMustMatchExactSerialAndRoster)
{
    LfgReceiptBook book; auto receipt = Ready(book); CHECK(book.Fence("cleanup-one", receipt));
    auto packet = Packet(); packet.state = LfgRoleReceipt::State::Aborted;
    book.Packet(Life(501), packet, receipt.join); book.Packet(Life(502), packet, receipt.join);
    CHECK(book.Verify("cleanup-one", receipt));
    book.Packet(Life(501), packet, receipt.join + 1); CHECK(!book.Verify("cleanup-one", receipt));
    CHECK(book.Fenced(Group, {}));
    LfgReceiptBook second; receipt = Ready(second); CHECK(second.Fence("cleanup-two", receipt));
    packet.members = {501, 503}; second.Packet(Life(501), packet, receipt.join);
    CHECK(!second.Verify("cleanup-two", receipt));
}

LEEROY_TEST(RealmLfgReceipts_UnownedAbortOrUnexpectedNestedPacketPoisons)
{
    for (bool nested : {false, true})
    {
        LfgReceiptBook book; auto receipt = Ready(book); CHECK(book.Fence("cleanup-one", receipt));
        auto packet = Packet();
        if (!nested) packet.state = LfgRoleReceipt::State::Aborted;
        book.Packet(Life(501), packet, nested ? receipt.join : 0);
        CHECK(!book.Verify("cleanup-one", receipt)); CHECK(book.Fenced(Group, {}));
    }
}

LEEROY_TEST(RealmLfgReceipts_OffEpochAndLifecycleKeepPoisonedFence)
{
    for (bool epoch : {false, true})
    {
        LfgReceiptBook book; auto receipt = Ready(book); CHECK(book.Fence("cleanup-one", receipt));
        if (epoch) book.SetEpoch(8); else book.Lifecycle(501);
        CHECK(!book.Verify("cleanup-one", receipt)); CHECK(book.Fenced(Group, {}));
        CHECK(!book.Join(Group, Group + 1, Life(501))); CHECK(!book.Proposal({Group}));
        CHECK(!book.Held("cleanup-one", Group, receipt.join));
    }
}

LEEROY_TEST(RealmLfgReceipts_ForeignRetirementCannotClearAnotherOperation)
{
    LfgReceiptBook book; auto receipt = Ready(book); CHECK(book.Fence("cleanup-one", receipt));
    book.Retire("cleanup-two"); CHECK(book.Fenced(Group, {})); CHECK(book.Verify("cleanup-one", receipt));
    book.Retire("cleanup-one"); CHECK(!book.Fenced(Group, {})); CHECK(!book.Proof(Group, false));
    CHECK(!book.Verify("cleanup-one", receipt));
}

LEEROY_TEST(RealmLfgReceipts_ExactReceiptCannotBeReplacedOrGuessedByOperation)
{
    LfgReceiptBook book; auto receipt = Ready(book);
    auto guessed = receipt; ++guessed.join; CHECK(!book.Fence("cleanup-one", guessed));
    CHECK(book.Fence("cleanup-one", receipt));
    CHECK(book.Held("cleanup-one", Group, receipt.join));
    CHECK(!book.Held("cleanup-two", Group, receipt.join));
    CHECK(!book.Held("cleanup-one", Group, receipt.join + 1));
    guessed.members[0].session += 1; CHECK(!book.Verify("cleanup-one", guessed));
}

LEEROY_TEST(RealmLfgReceipts_NewGroupContainingHeldMemberRemainsFenced)
{
    LfgReceiptBook book; auto receipt = Ready(book); CHECK(book.Fence("cleanup-one", receipt));
    book.ObserveGroup(Group + 7, {502, 507});
    CHECK(!book.Verify("cleanup-one", receipt)); CHECK(!book.Proposal({Group + 7}));
    CHECK(!book.Join(Group + 7, Group + 8, Life(507)));
    book.Retire("cleanup-one"); CHECK(book.Proposal({Group + 7}));
}

LEEROY_TEST(RealmLfgReceipts_RecordOverflowLosesCoverageAndKeepsHolders)
{
    LfgReceiptBook book; auto receipt = Ready(book); CHECK(book.Fence("cleanup-one", receipt));
    for (std::uint64_t i = 1; i <= LfgReceiptBook::Capacity; ++i)
        book.Born(Group + i, Group + 1000 + i, Life(static_cast<std::uint32_t>(600 + i)));
    CHECK(!book.Proof(Group, false)); CHECK(!book.Verify("cleanup-one", receipt));
    CHECK(book.Fenced(Group, {})); CHECK(!book.Proposal({Group}));
    // Exhaustion cannot opportunistically recover preexisting groups.
    book.Born(Group + 500, Group + 501, Life(900));
    CHECK(book.Join(Group + 500, Group + 501, Life(900)));
    CHECK(!book.Proof(Group + 500, false));
}

LEEROY_TEST(RealmLfgReceipts_AliasOverflowRemainsBoundedAndRetainsGuard)
{
    LfgReceiptBook book; auto receipt = Ready(book); CHECK(book.Fence("cleanup-one", receipt));
    for (std::uint64_t i = 1; i <= LfgReceiptBook::MemberCapacity + 1; ++i) book.ObserveGroup(Group + i, {501});
    CHECK(!book.Proposal({Group + 500})); CHECK(book.Fenced(Group, {}));
    book.SetEpoch(8); CHECK(!book.Proposal({Group + 500}));
    book.Retire("cleanup-one"); CHECK(book.Proposal({Group + 500}));
}

LEEROY_TEST(RealmLfgReceipts_OneOperationMayFenceMultipleDisjointProducerGroups)
{
    LfgReceiptBook book; auto first = Ready(book); CHECK(book.Fence("cleanup-one", first));
    Born(book, Group + 10, 601);
    LfgRoleReceipt secondPacket{LfgRoleReceipt::State::Initial, 601, {601, 602}};
    book.Packet(Life(601), secondPacket); book.Packet(Life(602), secondPacket);
    auto second = book.Proof(Group + 10, false); CHECK(second); CHECK(book.Fence("cleanup-one", *second));
    CHECK_EQ(book.Owners().size(), 2u);
    CHECK(book.Verify("cleanup-one", first)); CHECK(book.Verify("cleanup-one", *second));
    book.Retire("cleanup-one"); CHECK(!book.Fenced(Group, {})); CHECK(!book.Fenced(Group + 10, {}));
}

LEEROY_TEST(RealmLfgReceipts_AmbiguousRecipientAcrossProducerLineagesIsUnavailable)
{
    LfgReceiptBook book; book.SetEpoch(7); Born(book);
    book.Born(Group + 10, Group + 11, Life(501));
    book.Added(Group + 10, Group + 11, Life(502)); CHECK(book.Join(Group + 10, Group + 11, Life(501)));
    Complete(book); CHECK(!book.Proof(Group, false)); CHECK(!book.Proof(Group + 10, false));
}

LEEROY_TEST(RealmLfgReceipts_ProducerClosureMustBeAdmittedAndChargedAtomically)
{
    LfgReceiptBook book; auto receipt = Ready(book, true);
    auto const actual = LfgReceiptGuids(receipt);
    unsigned charges = 0;
    auto validate = [](auto guid) { return guid == 502 ? std::string("foreign_owner") : std::string{}; };
    CHECK_EQ(PrepareCleanupMutation(actual, {501, 502}, validate, [&](auto const&)
        { ++charges; return std::string{}; }), std::string("foreign_owner"));
    CHECK_EQ(PrepareCleanupMutation(actual, {501}, [](auto) { return std::string{}; }, [&](auto const&)
        { ++charges; return std::string{}; }), std::string("cleanup_member_not_admitted"));
    CHECK_EQ(charges, 0u);
    TickBudget budget(2); CHECK(budget.Take());
    auto charge = [&](auto const& members)
    {
        ++charges;
        return budget.Take(static_cast<std::uint32_t>(members.size())) ? std::string{} : std::string("busy:tick_budget");
    };
    CHECK_EQ(PrepareCleanupMutation(actual, {501, 502}, [](auto) { return std::string{}; }, charge), std::string("busy:tick_budget"));
    CHECK(!book.Fenced(Group, {})); CHECK(book.Proof(Group, true));
    CHECK_EQ(charges, 1u);
}
