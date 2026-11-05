// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmBankProof.h"
#include "TestHarness.h"

using namespace Leeroy::Realm;
namespace
{
constexpr std::uint64_t Player = 901;
constexpr BankPosition Bags{false, 255, 23}, OtherBag{false, 255, 24}, Tab{true, 0, 0}, OtherTab{true, 0, 1};
BankSnapshot Before()
{
    return {500000, 10000, 1, {
        {Bags, {1001, Player, 41001, 10, 20, 0}}, {OtherBag, {}}, {Tab, {}}, {OtherTab, {}}}};
}
BankMoveReceipt NativeReceipt()
{
    BankMoveReceipt receipt; receipt.Move(true); receipt.Event(true); return receipt;
}
BankSnapshot Deposited()
{
    auto after = Before(); after.slots[0].item = {}; after.slots[2].item = {1001, 0, 41001, 10, 20, 0}; return after;
}
// Capture every submitted transaction, then replay all completion orders. The
// native tab transaction is separate; bank debit/refund and settled gold must
// share the original lane transaction so four workers cannot restore a loan.
struct TabGoldTransactions
{
    static constexpr std::uint64_t OriginalBank = 500000;
    static constexpr std::uint32_t OriginalMoney = 10000, OriginalTabs = 1, Price = 100000;
    enum class Target { Bank, Gold, Tab };
    struct Write { Target target; std::uint64_t value; };
    using Transaction = std::vector<Write>;
    struct Applied
    {
        std::uint64_t bank = OriginalBank;
        std::uint32_t money = OriginalMoney, tabs = OriginalTabs;
    };
    Transaction original;
    std::vector<Transaction> submitted;
    std::uint64_t bank = OriginalBank;
    std::uint32_t money = OriginalMoney, tabs = OriginalTabs;
    bool exactOfficer = true, nativePurchaseProved = false, cleanReversalProved = false, credited = false;
    bool changeDuringStage = false, changeDuringSubmit = false;
    void Loan(bool creditOfficer = true)
    {
        bank -= Price; original.push_back({Target::Bank, bank});
        if (creditOfficer) { money += Price; credited = true; }
    }
    void NativePurchase()
    {
        money -= Price; ++tabs;
        submitted.push_back({{Target::Tab, tabs}}); nativePurchaseProved = true;
    }
    void Reverse()
    {
        if (credited) money -= Price;
        bank += Price; original.push_back({Target::Bank, bank}); cleanReversalProved = true;
    }
    bool Finalize()
    {
        return SubmitSettledBankTabTransaction(OriginalMoney, original,
            [&](std::uint32_t originalMoney)
            {
                bool const settled = cleanReversalProved
                    ? !nativePurchaseProved && bank == OriginalBank && tabs == OriginalTabs
                    : nativePurchaseProved && bank == OriginalBank - Price && tabs == OriginalTabs + 1;
                return exactOfficer && settled && money == originalMoney;
            },
            [&](Transaction& pending)
            {
                pending.push_back({Target::Gold, money});
                if (changeDuringStage) exactOfficer = false;
            },
            [&](Transaction staged)
            {
                submitted.push_back(std::move(staged));
                if (changeDuringSubmit) exactOfficer = false;
            });
    }
    bool NoTransientGoldSubmitted() const
    {
        for (auto const& transaction : submitted)
            for (auto const& write : transaction)
                if (write.target == Target::Gold && write.value != OriginalMoney) return false;
        return true;
    }
    void CheckEveryCompletionOrder(std::uint64_t expectedBank, std::uint32_t expectedMoney, std::uint32_t expectedTabs) const
    {
        std::vector<std::size_t> order;
        for (std::size_t i = 0; i < submitted.size(); ++i) order.push_back(i);
        do
        {
            Applied actual;
            for (auto index : order)
                for (auto const& write : submitted[index])
                    switch (write.target)
                    {
                        case Target::Bank: actual.bank = write.value; break;
                        case Target::Gold: actual.money = static_cast<std::uint32_t>(write.value); break;
                        case Target::Tab: actual.tabs = static_cast<std::uint32_t>(write.value); break;
                    }
            CHECK_EQ(actual.bank, expectedBank); CHECK_EQ(actual.money, expectedMoney); CHECK_EQ(actual.tabs, expectedTabs);
        } while (std::next_permutation(order.begin(), order.end()));
    }
};
}
LEEROY_TEST(RealmBank_GoldConversionChecksActualBankCapBeforeAdding)
{
    constexpr std::uint64_t cap = 0x7FFFFFFFFFFFF;
    CHECK(BankGoldCreditAllowed(0, 60000, cap));
    CHECK(BankGoldCreditAllowed(cap - 10000, 1, cap));
    CHECK(!BankGoldCreditAllowed(cap - 9999, 1, cap));
    CHECK(!BankGoldCreditAllowed(cap + 1, 1, cap));
    CHECK(!BankGoldCreditAllowed(std::numeric_limits<std::uint64_t>::max(), 60000, cap));
    CHECK(!BankGoldCreditAllowed(0, 0, cap)); CHECK(!BankGoldCreditAllowed(0, 60001, cap));
}
LEEROY_TEST(RealmBank_TabLoanHonorsSignedNativeInputStrictPlayerCapAndTrialCap)
{
    constexpr std::uint32_t cap = 0x7FFFFFFE;
    CHECK(BankTabLoanAllowed(1000000, 1200, 1000000, cap));
    CHECK(!BankTabLoanAllowed(999999, 1200, 1000000, cap));
    CHECK(BankTabLoanAllowed(100, cap - 101, 100, cap));
    CHECK(!BankTabLoanAllowed(100, cap - 100, 100, cap));
    CHECK(!BankTabLoanAllowed(100, cap, 1, cap));
    CHECK(!BankTabLoanAllowed(std::numeric_limits<std::uint64_t>::max(), 0, 0x80000000, cap));
    CHECK(!BankTabLoanAllowed(100, 0, 0, cap));
    CHECK(BankTabLoanAllowed(100, 50, 100, cap, 150));
    CHECK(!BankTabLoanAllowed(100, 51, 100, cap, 150));
}
LEEROY_TEST(RealmBank_TabPurchaseSubmitsFinalOfficerGoldAlongsideBankDebitAndNativeTab)
{
    TabGoldTransactions native;
    native.Loan(); CHECK(native.submitted.empty());
    native.NativePurchase(); CHECK_EQ(native.submitted.size(), 1u);
    CHECK_EQ(native.money, TabGoldTransactions::OriginalMoney);
    CHECK(native.Finalize());
    CHECK(native.NoTransientGoldSubmitted());
    native.CheckEveryCompletionOrder(TabGoldTransactions::OriginalBank - TabGoldTransactions::Price,
        TabGoldTransactions::OriginalMoney, TabGoldTransactions::OriginalTabs + 1);
}
LEEROY_TEST(RealmBank_FinalGoldSubmissionCannotPersistAnUnprovedLoanOrPartialPurchase)
{
    TabGoldTransactions loan; loan.Loan();
    CHECK(!loan.Finalize());
    CHECK(loan.submitted.empty()); CHECK(loan.NoTransientGoldSubmitted());
    for (auto changed : {0, 1, 2, 3})
    {
        TabGoldTransactions partial; partial.Loan(); partial.NativePurchase();
        if (changed == 0) partial.exactOfficer = false;
        if (changed == 1) ++partial.money;
        if (changed == 2) ++partial.bank;
        if (changed == 3) ++partial.tabs;
        CHECK(!partial.Finalize());
        CHECK_EQ(partial.submitted.size(), 1u); CHECK(partial.NoTransientGoldSubmitted());
        partial.CheckEveryCompletionOrder(TabGoldTransactions::OriginalBank,
            TabGoldTransactions::OriginalMoney, TabGoldTransactions::OriginalTabs + 1);
    }
}
LEEROY_TEST(RealmBank_FinalGoldStagingRechecksOfficerBeforeSubmittingCapturedMoney)
{
    TabGoldTransactions native; native.Loan(); native.NativePurchase(); native.changeDuringStage = true;
    CHECK(!native.Finalize());
    CHECK_EQ(native.submitted.size(), 1u); CHECK(native.NoTransientGoldSubmitted());
    native.CheckEveryCompletionOrder(TabGoldTransactions::OriginalBank,
        TabGoldTransactions::OriginalMoney, TabGoldTransactions::OriginalTabs + 1);
}
LEEROY_TEST(RealmBank_FinalGoldPostSubmissionInterferenceRemainsUnknownWithoutReversal)
{
    TabGoldTransactions native; native.Loan(); native.NativePurchase(); native.changeDuringSubmit = true;
    CHECK(!native.Finalize());
    CHECK(native.NoTransientGoldSubmitted());
    native.CheckEveryCompletionOrder(TabGoldTransactions::OriginalBank - TabGoldTransactions::Price,
        TabGoldTransactions::OriginalMoney, TabGoldTransactions::OriginalTabs + 1);
}
LEEROY_TEST(RealmBank_CleanTabRefusalKeepsDebitRefundAndFinalGoldInOneTransaction)
{
    TabGoldTransactions native; native.Loan(); CHECK(native.submitted.empty());
    native.Reverse(); CHECK(native.submitted.empty());
    CHECK(native.Finalize()); CHECK_EQ(native.submitted.size(), 1u); CHECK(native.NoTransientGoldSubmitted());
    native.CheckEveryCompletionOrder(TabGoldTransactions::OriginalBank,
        TabGoldTransactions::OriginalMoney, TabGoldTransactions::OriginalTabs);
}
LEEROY_TEST(RealmBank_FailedOfficerLoanSubmitsOnlyTheCleanReversedTransaction)
{
    TabGoldTransactions native; native.Loan(false); native.Reverse();
    CHECK(native.submitted.empty()); CHECK(native.Finalize()); CHECK(native.NoTransientGoldSubmitted());
    native.CheckEveryCompletionOrder(TabGoldTransactions::OriginalBank,
        TabGoldTransactions::OriginalMoney, TabGoldTransactions::OriginalTabs);
}
LEEROY_TEST(RealmBank_FullDepositRequiresExactNativeCallbacksAndIdentityReadback)
{
    auto const result = ProveBankTransfer(Before(), Deposited(), {Bags, Tab, 10, Player}, NativeReceipt());
    CHECK(result == BankTransferResult::Complete);
    CHECK(ProveBankTransfer(Before(), Deposited(), {Bags, Tab, 10, Player}, {}) == BankTransferResult::Unknown);
    auto missing = NativeReceipt(); missing.events = 0;
    CHECK(ProveBankTransfer(Before(), Deposited(), {Bags, Tab, 10, Player}, missing) == BankTransferResult::Unknown);
}
LEEROY_TEST(RealmBank_OnlyNoCallbacksAndExactUnchangedSlotsProveNoMovement)
{
    CHECK(ProveBankTransfer(Before(), Before(), {Bags, Tab, 10, Player}, {}) == BankTransferResult::Unchanged);
    CHECK(ProveBankTransfer(Before(), Before(), {Bags, Tab, 10, Player}, NativeReceipt()) == BankTransferResult::Unknown);
    auto changed = Before(); changed.slots[0].item.guid = 1002;
    CHECK(ProveBankTransfer(Before(), changed, {Bags, Tab, 10, Player}, {}) == BankTransferResult::Unknown);
}
LEEROY_TEST(RealmBank_SplitRequiresTheOriginalSourceAndFreshDestinationGuid)
{
    auto after = Before(); after.slots[0].item.count = 6; after.slots[2].item = {2001, 0, 41001, 4, 20, 0};
    CHECK(ProveBankTransfer(Before(), after, {Bags, Tab, 4, Player}, NativeReceipt()) == BankTransferResult::Complete);
    after.slots[0].item.guid = 1002;
    CHECK(ProveBankTransfer(Before(), after, {Bags, Tab, 4, Player}, NativeReceipt()) == BankTransferResult::Unknown);
    after.slots[0].item.guid = 1001; after.slots[2].item.guid = 1001;
    CHECK(ProveBankTransfer(Before(), after, {Bags, Tab, 4, Player}, NativeReceipt()) == BankTransferResult::Unknown);
}
LEEROY_TEST(RealmBank_MergingKeepsTheExactDestinationIdentity)
{
    auto before = Before(); before.slots[2].item = {2001, 0, 41001, 7, 20, 0};
    auto after = before; after.slots[0].item.count = 6; after.slots[2].item.count = 11;
    CHECK(ProveBankTransfer(before, after, {Bags, Tab, 4, Player}, NativeReceipt()) == BankTransferResult::Complete);
    after.slots[2].item.guid = 2002;
    CHECK(ProveBankTransfer(before, after, {Bags, Tab, 4, Player}, NativeReceipt()) == BankTransferResult::Unknown);
}
LEEROY_TEST(RealmBank_WithdrawalProvesExactSourceAndDestinationOwner)
{
    auto before = Deposited(); auto after = Before();
    CHECK(ProveBankTransfer(before, after, {Tab, Bags, 10, Player}, NativeReceipt()) == BankTransferResult::Complete);
    after.slots[0].item.owner = Player + 1;
    CHECK(ProveBankTransfer(before, after, {Tab, Bags, 10, Player}, NativeReceipt()) == BankTransferResult::Unknown);
    after = Before(); after.slots[0].item.guid = 3001;
    CHECK(ProveBankTransfer(before, after, {Tab, Bags, 10, Player}, NativeReceipt()) == BankTransferResult::Unknown);
}
LEEROY_TEST(RealmBank_UnrelatedCompensatingCountsCannotSupplyCausalSuccess)
{
    auto before = Before(); before.slots[1].item = {1002, Player, 41001, 10, 20, 0};
    auto after = before; after.slots[1].item = {}; after.slots[2].item = {1002, 0, 41001, 10, 20, 0};
    CHECK(ProveBankTransfer(before, after, {Bags, Tab, 10, Player}, NativeReceipt()) == BankTransferResult::Unknown);
    after = before; after.slots[0].item = {}; after.slots[2].item = {1001, 0, 41001, 10, 20, 0};
    after.slots[1].item.count = 9; after.slots[3].item = {3001, 0, 41001, 1, 20, 0};
    CHECK(ProveBankTransfer(before, after, {Bags, Tab, 10, Player}, NativeReceipt()) == BankTransferResult::Unknown);
}
LEEROY_TEST(RealmBank_PartialNativeEffectNeverCompletesOrClaimsNoMovement)
{
    auto after = Before(); after.slots[0].item.count = 7; after.slots[2].item = {2001, 0, 41001, 3, 20, 0};
    CHECK(ProveBankTransfer(Before(), after, {Bags, Tab, 4, Player}, NativeReceipt()) == BankTransferResult::Unknown);
    after = Before(); after.slots[0].item = {};
    CHECK(ProveBankTransfer(Before(), after, {Bags, Tab, 10, Player}, NativeReceipt()) == BankTransferResult::Unknown);
    after = Deposited(); ++after.bankMoney;
    CHECK(ProveBankTransfer(Before(), after, {Bags, Tab, 10, Player}, NativeReceipt()) == BankTransferResult::Unknown);
}
LEEROY_TEST(RealmBank_NestedOrForeignNativeCallbacksPoisonTheReceipt)
{
    auto receipt = NativeReceipt(); receipt.Move(true);
    CHECK(ProveBankTransfer(Before(), Deposited(), {Bags, Tab, 10, Player}, receipt) == BankTransferResult::Unknown);
    receipt = NativeReceipt(); receipt.Event(false);
    CHECK(ProveBankTransfer(Before(), Deposited(), {Bags, Tab, 10, Player}, receipt) == BankTransferResult::Unknown);
    receipt = NativeReceipt(); receipt.Move(false);
    CHECK(ProveBankTransfer(Before(), Before(), {Bags, Tab, 10, Player}, receipt) == BankTransferResult::Unknown);
}
LEEROY_TEST(RealmBank_StackCapacityPropertyAndCoordinatesRemainExact)
{
    auto before = Before(); before.slots[2].item = {2001, 0, 41001, 18, 20, 0};
    auto after = before; after.slots[0].item.count = 6; after.slots[2].item.count = 22;
    CHECK(ProveBankTransfer(before, after, {Bags, Tab, 4, Player}, NativeReceipt()) == BankTransferResult::Unknown);
    before = Before(); before.slots[2].item = {2001, 0, 41001, 1, 20, 12};
    after = before; after.slots[0].item.count = 6; after.slots[2].item.count = 5;
    CHECK(ProveBankTransfer(before, after, {Bags, Tab, 4, Player}, NativeReceipt()) == BankTransferResult::Unknown);
    after = Deposited(); std::swap(after.slots[2], after.slots[3]);
    CHECK(ProveBankTransfer(Before(), after, {Bags, Tab, 10, Player}, NativeReceipt()) == BankTransferResult::Unknown);
}
LEEROY_TEST(RealmBank_NewStockCreationAndCleanupNeedAnExactFreshFullStack)
{
    auto const before = Before(); auto after = before;
    BankItemFact const created{3001, Player, 41001, 5, 20, 0}; after.slots[1].item = created;
    CHECK(ExactBankCreation(before, after, OtherBag, created));
    CHECK(CanDestroyBankCreation(before, after, OtherBag, created));
    after.slots[1].item.count = 4;
    CHECK(!CanDestroyBankCreation(before, after, OtherBag, created));
    after.slots[1].item = created; after.slots[1].item.guid = 3002;
    CHECK(!CanDestroyBankCreation(before, after, OtherBag, created));
    after.slots[1].item = created; after.slots[0].item.count = 9;
    CHECK(!CanDestroyBankCreation(before, after, OtherBag, created));
}
LEEROY_TEST(RealmBank_NewStockCannotAdoptOrDeleteAPreexistingMergedStack)
{
    auto const before = Before(); auto after = before;
    auto created = before.slots[0].item; created.count += 5; after.slots[0].item = created;
    CHECK(!ExactBankCreation(before, after, Bags, created));
    CHECK(!CanDestroyBankCreation(before, after, Bags, created));
    created = {1001, Player, 41001, 5, 20, 0}; after = before; after.slots[1].item = created;
    CHECK(!ExactBankCreation(before, after, OtherBag, created));
}
LEEROY_TEST(RealmBank_StockPartialDepositAndReplacementNeverAuthorizeCleanup)
{
    auto const before = Before(); auto after = before;
    BankItemFact const created{3001, Player, 41001, 5, 20, 0};
    after.slots[1].item = created; after.slots[1].item.count = 3;
    after.slots[2].item = {4001, 0, 41001, 2, 20, 0};
    CHECK(!CanDestroyBankCreation(before, after, OtherBag, created));
    after = before; after.slots[1].item = created; after.slots[1].item.owner = Player + 1;
    CHECK(!CanDestroyBankCreation(before, after, OtherBag, created));
}
LEEROY_TEST(RealmBank_SnapshotRejectsUnboundedDuplicateOrMalformedFacts)
{
    auto snapshot = Before(); CHECK(ValidBankSnapshot(snapshot));
    snapshot.tabs = 7; CHECK(!ValidBankSnapshot(snapshot));
    snapshot = Before(); snapshot.slots.push_back(snapshot.slots[0]); CHECK(!ValidBankSnapshot(snapshot));
    snapshot = Before(); snapshot.slots[1].item = snapshot.slots[0].item; CHECK(!ValidBankSnapshot(snapshot));
    snapshot = Before(); snapshot.slots[1].item.count = 1; CHECK(!ValidBankSnapshot(snapshot));
    snapshot = Before(); snapshot.slots[2].position.container = 1; CHECK(!ValidBankSnapshot(snapshot));
    snapshot = Before(); snapshot.slots.resize(kBankSnapshotSlots + 1); CHECK(!ValidBankSnapshot(snapshot));
}
LEEROY_TEST(RealmBank_MaximumNativeSnapshotUsesBoundedDistinctCoordinates)
{
    BankSnapshot snapshot; snapshot.tabs = 6;
    for (std::uint8_t slot = 0; slot < 150; ++slot) snapshot.slots.push_back({{false, 255, slot}, {}});
    for (auto bag : {19, 20, 21, 22, 67, 68, 69, 70, 71, 72, 73})
        for (std::uint8_t slot = 0; slot < 36; ++slot)
            snapshot.slots.push_back({{false, static_cast<std::uint8_t>(bag), slot}, {}});
    for (std::uint8_t tab = 0; tab < 6; ++tab)
        for (std::uint8_t slot = 0; slot < 98; ++slot) snapshot.slots.push_back({{true, tab, slot}, {}});
    CHECK_EQ(snapshot.slots.size(), kBankSnapshotSlots); CHECK(ValidBankSnapshot(snapshot));
    for (std::size_t i = 0; i < snapshot.slots.size(); ++i)
        snapshot.slots[i].item = {1000 + i, 0, 41001, 1, 20, 0};
    CHECK(ValidBankSnapshot(snapshot));
    snapshot.slots.back().item.guid = snapshot.slots.front().item.guid; CHECK(!ValidBankSnapshot(snapshot));
}
LEEROY_TEST(RealmBank_AdmissionChecksAnUnreservedBodyUnowned)
{
    // The queue admits before it reserves: a bank command was refused busy:operation_identity
    // at acceptance when its own id was checked against a reservation that did not exist yet.
    CHECK(BankAdmissionOperation("", "op-1").empty());
    CHECK(BankAdmissionOperation("op-1", "op-1") == "op-1");
    CHECK(BankAdmissionOperation("op-2", "op-1").empty());
    CHECK(BankAdmissionOperation("", "").empty());
}
