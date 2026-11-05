// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_TRANSMOG_H
#define MOD_LEEROY_REALM_TRANSMOG_H

#include "LeeroyRealmIdentity.h"
#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <utility>
#include <vector>

namespace Leeroy::Realm
{
inline constexpr std::size_t kTransmogSlots = 19;
inline constexpr std::uint8_t kTransmogEquipmentBag = 255;
struct TransmogAuthority
{
    bool on = false, castMember = false, mutationAllowed = false;
    std::uint64_t epoch = 0, generation = 0;
    std::uint32_t guild = 0, human = 0;
    std::string holder;
    std::optional<BodyIdentity> body;
    bool operator==(TransmogAuthority const&) const = default;
};
inline bool SameTransmogAuthority(TransmogAuthority const& original, TransmogAuthority const& current,
    std::string const& operation)
{
    return !operation.empty() && original.on && original.castMember && original.mutationAllowed
        && original.guild && original.human && original.holder == operation && original.body
        && original.body->player && original.body->ai && original.body->session && original == current;
}
// These are observations of the native indexes, not a second appearance store.
// A stored zero is deliberately different from an absent record.
struct TransmogAppearance
{
    bool indexed = false, recorded = false;
    std::uint64_t owner = 0;
    std::uint32_t entry = 0, readback = 0;
    bool operator==(TransmogAppearance const&) const = default;
};
struct TransmogItem
{
    std::uintptr_t object = 0, container = 0;
    std::uint64_t guid = 0, owner = 0;
    std::uint32_t entry = 0, updateState = 0, queuePosition = 0;
    std::uint32_t refundRecipient = 0, paidMoney = 0, paidExtendedCost = 0;
    std::uint8_t bag = 0, slot = 0;
    bool equipped = false, inTrade = false;
    std::string text;
    // Every native item update field, including flags/binding, count, charges,
    // enchantments, durability, creator, duration and random properties.
    std::vector<std::uint32_t> fields;
    bool operator==(TransmogItem const&) const = default;
};
struct TransmogSnapshot
{
    std::uint64_t owner = 0;
    std::uint32_t money = 0;
    std::array<TransmogItem, kTransmogSlots> items;
    std::array<TransmogAppearance, kTransmogSlots> appearances;
    std::array<std::uint32_t, kTransmogSlots> visibleEntries{}, visibleEnchants{};
    bool operator==(TransmogSnapshot const&) const = default;
};
inline bool ValidTransmogSnapshot(TransmogSnapshot const& snapshot)
{
    if (!snapshot.owner) return false;
    for (std::size_t slot = 0; slot < kTransmogSlots; ++slot)
    {
        auto const& item = snapshot.items[slot];
        auto const& appearance = snapshot.appearances[slot];
        if (!item.guid)
        {
            if (item != TransmogItem{} || appearance != TransmogAppearance{}) return false;
            continue;
        }
        if (!item.object || !item.entry || item.owner != snapshot.owner || !item.equipped
            || item.slot != slot || item.bag != kTransmogEquipmentBag || item.container
            || item.fields.empty() || item.fields.size() > 128)
            return false;
        for (std::size_t previous = 0; previous < slot; ++previous)
            if (snapshot.items[previous].guid == item.guid || snapshot.items[previous].object == item.object)
                return false;
        if (appearance.indexed || appearance.recorded)
        {
            if (!appearance.indexed || !appearance.recorded || appearance.owner != snapshot.owner
                || appearance.readback != appearance.entry) return false;
        }
        else if (appearance != TransmogAppearance{}) return false;
    }
    return true;
}
inline bool ProveTransmog(TransmogSnapshot const& before, TransmogSnapshot const& after,
    std::uint32_t slot, std::uint32_t entry)
{
    if (slot >= kTransmogSlots || !ValidTransmogSnapshot(before) || !ValidTransmogSnapshot(after)
        || !before.items[slot].guid) return false;
    auto expected = before;
    expected.appearances[slot] = entry ? TransmogAppearance{true, true, before.owner, entry, entry}
        : TransmogAppearance{};
    expected.visibleEntries[slot] = entry ? entry : before.items[slot].entry;
    // Native SetFakeEntry/DeleteFakeEntry change only that appearance and its
    // visible entry. No other equipment/item field or player money may change.
    return expected == after;
}
enum class TransmogState { Complete, Refused, Unknown };
struct TransmogResult
{
    TransmogState state;
    std::string reason;
};

// Shared production policy. Callers retain their original body/session/epoch/
// generation/authority fence in exact(); no successful readback can renew it.
// Unknown is handed back to the existing queue, which retains its sole holder.
template<class Eligible, class Read, class ValidEntry, class Exact, class Apply, class Clear>
TransmogResult MutateTransmog(bool available, std::uint32_t slot, std::uint32_t entry,
    Eligible eligible, Read read, ValidEntry validEntry, Exact exact, Apply apply, Clear clear)
{
    if (!available) return {TransmogState::Refused, "no_transmog"};
    if (slot >= kTransmogSlots) return {TransmogState::Refused, "bad_entry"};
    if (auto why = eligible(); !why.empty()) return {TransmogState::Refused, std::move(why)};
    auto const before = read();
    if (!before || !ValidTransmogSnapshot(*before)) return {TransmogState::Refused, "transmog_state_unproved"};
    if (!before->items[slot].guid) return {TransmogState::Refused, "not_equipped"};
    if (before->items[slot].inTrade) return {TransmogState::Refused, "busy:trade"};
    if (entry && !validEntry()) return {TransmogState::Refused, "bad_entry"};
    if (!exact() || read() != before) return {TransmogState::Refused, "transmog_item_changed"};
    if (entry) apply();
    else clear(); // SetFakeEntry(0) is never a clear operation.
    if (!exact()) return {TransmogState::Unknown, "transmog_identity_unproved"};
    auto const after = read();
    if (!exact() || !after || !ProveTransmog(*before, *after, slot, entry))
        return {TransmogState::Unknown, "transmog_state_unproved"};
    return {TransmogState::Complete, ""};
}
}
#endif
