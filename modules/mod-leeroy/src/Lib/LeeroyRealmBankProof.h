// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_BANK_PROOF_H
#define MOD_LEEROY_REALM_BANK_PROOF_H

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <optional>
#include <utility>
#include <vector>

namespace Leeroy::Realm
{
// Installed 3.3.5 inventory bounds: 150 root slots, eleven bags of at most
// 36 slots, and six guild tabs of 98 slots. Native snapshots also include
// equipment/personal-bank slots so an unrelated compensating change fails.
inline constexpr std::size_t kBankSnapshotSlots = 150 + 11 * 36 + 6 * 98;
inline constexpr std::size_t kBankManifestSlots = 16 + 4 * 36;
struct BankPosition
{
    bool bank = false;
    std::uint8_t container = 0, slot = 0;
    bool operator==(BankPosition const&) const = default;
};
struct BankItemFact
{
    std::uint64_t guid = 0, owner = 0;
    std::uint32_t entry = 0, count = 0, maxStack = 0;
    std::int32_t property = 0;
    bool operator==(BankItemFact const&) const = default;
};
struct BankSlotFact
{
    BankPosition position;
    BankItemFact item;
    bool operator==(BankSlotFact const&) const = default;
};
struct BankSnapshot
{
    std::uint64_t bankMoney = 0;
    std::uint32_t playerMoney = 0, tabs = 0;
    std::vector<BankSlotFact> slots;
    bool operator==(BankSnapshot const&) const = default;
};
inline bool ValidBankSnapshot(BankSnapshot const& snapshot)
{
    if (snapshot.tabs > 6 || snapshot.slots.size() > kBankSnapshotSlots) return false;
    // Validation runs on a native action path. Sort bounded contiguous keys
    // rather than performing a pairwise scan of up to 1,134 copied slots.
    std::vector<std::uint32_t> positions;
    std::vector<std::uint64_t> guids;
    positions.reserve(snapshot.slots.size()); guids.reserve(snapshot.slots.size());
    for (auto const& slot : snapshot.slots)
    {
        if (slot.position.bank && (slot.position.container >= snapshot.tabs || slot.position.slot >= 98)) return false;
        auto const& item = slot.item;
        if ((!item.guid && item != BankItemFact{})
            || (item.guid && (!item.entry || !item.count || !item.maxStack || item.count > item.maxStack))) return false;
        positions.push_back((std::uint32_t(slot.position.bank) << 16)
            | (std::uint32_t(slot.position.container) << 8) | slot.position.slot);
        if (item.guid) guids.push_back(item.guid);
    }
    std::sort(positions.begin(), positions.end()); std::sort(guids.begin(), guids.end());
    return std::adjacent_find(positions.begin(), positions.end()) == positions.end()
        && std::adjacent_find(guids.begin(), guids.end()) == guids.end();
}
inline BankSlotFact const* BankSlot(BankSnapshot const& snapshot, BankPosition position)
{
    auto const found = std::find_if(snapshot.slots.begin(), snapshot.slots.end(),
        [&](BankSlotFact const& slot) { return slot.position == position; });
    return found == snapshot.slots.end() ? nullptr : &*found;
}
struct BankTransfer
{
    BankPosition source, destination;
    std::uint32_t count = 0;
    std::uint64_t player = 0;
};
enum class BankTransferResult { Complete, Unchanged, Unknown };
struct BankMoveReceipt
{
    bool exact = true;
    std::uint32_t moves = 0, events = 0;
    void Move(bool matches) { if (!matches || ++moves != 1) exact = false; }
    void Event(bool matches) { if (!matches || ++events != 1) exact = false; }
};
// A pre-move native callback and its exact native bank event bind the invocation;
// complete slot readback then proves the specific source/destination effect.
// Equal aggregate item counts, alone, never establish either success or failure.
inline BankTransferResult ProveBankTransfer(BankSnapshot const& before, BankSnapshot const& after,
    BankTransfer const& transfer, BankMoveReceipt const& receipt)
{
    if (!ValidBankSnapshot(before) || !ValidBankSnapshot(after) || !receipt.exact
        || before.tabs != after.tabs || before.bankMoney != after.bankMoney
        || before.playerMoney != after.playerMoney || before.slots.size() != after.slots.size())
        return BankTransferResult::Unknown;
    auto const* source = BankSlot(before, transfer.source);
    auto const* destination = BankSlot(before, transfer.destination);
    if (!source || !destination || transfer.source.bank == transfer.destination.bank
        || !transfer.player || !transfer.count || !source->item.guid || transfer.count > source->item.count)
        return BankTransferResult::Unknown;
    auto const& item = source->item;
    auto const& target = destination->item;
    if (source->item.owner != (transfer.source.bank ? 0 : transfer.player)
        || (target.guid && (target.owner != (transfer.destination.bank ? 0 : transfer.player)
            || target.entry != item.entry || target.property != item.property || target.maxStack != item.maxStack))
        || transfer.count > item.maxStack - target.count)
        return BankTransferResult::Unknown;
    // A callback means the core entered _DoItemsMove after all native checks.
    // Even unchanged slots then cannot prove that no other effect occurred.
    if (before == after && !receipt.moves && !receipt.events) return BankTransferResult::Unchanged;
    if (receipt.moves != 1 || receipt.events != 1) return BankTransferResult::Unknown;
    auto const* actualDestination = BankSlot(after, transfer.destination);
    if (!actualDestination) return BankTransferResult::Unknown;
    auto expectedDestination = target.guid ? target : item;
    expectedDestination.owner = transfer.destination.bank ? 0 : transfer.player;
    expectedDestination.count = target.count + transfer.count;
    if (!target.guid && transfer.count != item.count)
    {
        // Native splits clone a fresh item. Its identity must be new to the
        // entire observed inventory/bank, never an existing compensating stack.
        expectedDestination.guid = actualDestination->item.guid;
        if (!expectedDestination.guid || std::any_of(before.slots.begin(), before.slots.end(),
            [&](BankSlotFact const& slot) { return slot.item.guid == expectedDestination.guid; }))
            return BankTransferResult::Unknown;
    }
    auto expectedSource = item;
    if (transfer.count == item.count) expectedSource = {};
    else expectedSource.count -= transfer.count;
    for (std::size_t i = 0; i < before.slots.size(); ++i)
    {
        auto const& old = before.slots[i]; auto const& now = after.slots[i];
        if (old.position != now.position) return BankTransferResult::Unknown;
        auto const expected = old.position == transfer.source ? expectedSource
            : old.position == transfer.destination ? expectedDestination : old.item;
        if (now.item != expected) return BankTransferResult::Unknown;
    }
    return BankTransferResult::Complete;
}
inline bool BankGoldCreditAllowed(std::uint64_t balance, std::uint32_t gold, std::uint64_t limit)
{
    return gold >= 1 && gold <= 60000 && balance <= limit && std::uint64_t(gold) * 10000 <= limit - balance;
}
inline bool BankTabLoanAllowed(std::uint64_t bank, std::uint32_t officer, std::uint32_t price,
    std::uint32_t playerLimit, std::uint32_t trialLimit = 0)
{
    // Installed ModifyMoney rejects equality at MAX_MONEY_AMOUNT. Its input is
    // signed and hooks may change it, so native readback is still mandatory.
    return price && price <= std::uint32_t(std::numeric_limits<std::int32_t>::max()) && bank >= price
        && officer < playerLimit && price < playerLimit - officer
        && (!trialLimit || (officer <= trialLimit && price <= trialLimit - officer));
}
// The debit transaction stays local while officer gold is a transient loan.
// Save appends actual settled gold to that same transaction. A clean reversal
// also appends its bank refund there, so independent DB workers cannot reorder
// a stale debit/loan write after the settled balance. Exact rechecks the full
// native outcome and original officer life/ownership around save/submission.
// False before submission discards only local SQL, never native partial effects;
// false afterward remains an unknown outcome, not a rollback/durability proof.
template <class Transaction, class Exact, class Save, class Submit>
bool SubmitSettledBankTabTransaction(std::uint32_t originalMoney, Transaction& transaction,
    Exact&& exact, Save&& save, Submit&& submit)
{
    if (!exact(originalMoney)) return false;
    save(transaction);
    if (!exact(originalMoney)) return false;
    submit(std::move(transaction));
    return exact(originalMoney);
}
inline bool ExactBankCreation(BankSnapshot const& before, BankSnapshot const& after,
    BankPosition destination, BankItemFact const& created)
{
    if (!ValidBankSnapshot(before) || !ValidBankSnapshot(after) || destination.bank || !created.guid
        || before.tabs != after.tabs || before.bankMoney != after.bankMoney
        || before.playerMoney != after.playerMoney || before.slots.size() != after.slots.size()) return false;
    auto const* empty = BankSlot(before, destination);
    if (!empty || empty->item.guid || !BankSlot(after, destination)) return false;
    for (std::size_t i = 0; i < before.slots.size(); ++i)
    {
        auto const& old = before.slots[i]; auto const& now = after.slots[i];
        if (old.item.guid == created.guid || old.position != now.position
            || now.item != (old.position == destination ? created : old.item)) return false;
    }
    return true;
}
// Cleanup is authorized only for the exact new full stack at its original
// position. Partial movement or replacement can never authorize DestroyItem.
inline bool CanDestroyBankCreation(BankSnapshot const& beforeCreate, BankSnapshot const& now,
    BankPosition destination, BankItemFact const& created)
{
    return ExactBankCreation(beforeCreate, now, destination, created);
}
}
#endif
