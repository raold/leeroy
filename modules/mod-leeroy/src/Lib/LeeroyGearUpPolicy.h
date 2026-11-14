// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_GEAR_UP_POLICY_H
#define MOD_LEEROY_GEAR_UP_POLICY_H

#include <algorithm>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <string_view>

namespace Leeroy::Guild
{
// Snapshot immediately before mutation, not the state when the command was queued.
// Ownership means current configured-guild membership, excluding the human; level is deliberately absent.
struct GearUpState
{
    bool owned = false, online = false, bot = false;
    bool grouped = false, inBg = false, inInstance = false, teleporting = false;
    bool dead = false, inCombat = false, inFlight = false, trading = false;
};

inline std::string_view GearUpSkipReason(GearUpState const& s)
{
    if (!s.owned) return "not_cast";
    if (!s.online) return "offline";
    if (!s.bot) return "not_bot";
    if (s.grouped) return "grouped";
    if (s.inBg) return "in_bg";
    if (s.inInstance) return "in_instance";
    if (s.teleporting) return "teleporting";
    if (s.dead) return "dead";
    if (s.inCombat) return "in_combat";
    if (s.inFlight) return "in_flight";
    if (s.trading) return "trading";
    return {};
}

inline std::uint32_t GearUpLevel(std::uint32_t current, std::uint32_t requested)
{
    return std::max(current, requested);
}

inline constexpr auto kGearUpTimeout = std::chrono::minutes(5);
inline bool GearUpExpired(std::chrono::steady_clock::time_point started,
    std::chrono::steady_clock::time_point now)
{
    return now - started >= kGearUpTimeout;
}

// The regear (gear_up with "regear": true, the mind's [guild] regear_to): the guild's bots taken down to a cap.
// Only the cast's bots: never the owner (Leeroy.HumanGuid), never a character outside the cast (a random bot outside
// the guild, a member under level 80) and never a character on a player's account.
struct RegearTarget
{
    bool human = false, cast = false, randomAccount = false;
};

inline std::string_view RegearRefusal(RegearTarget const& t)
{
    if (t.human) return "owner";
    if (!t.cast) return "not_cast";
    if (!t.randomAccount) return "not_bot";
    return {};
}

// Before each member: who it is, then his presence (his login ends the job), the gear-up's own body checks, and the
// Realm/training fences (Realm leases, offline raids, training, pending bot jobs: RealmModule::MutationReason).
inline std::string_view RegearSkipReason(RegearTarget const& target, bool ownerOnline, GearUpState const& state,
    std::string_view fence)
{
    if (auto refusal = RegearRefusal(target); !refusal.empty()) return refusal;
    if (ownerOnline) return "owner_online";
    if (auto skip = GearUpSkipReason(state); !skip.empty()) return skip;
    return fence;
}

// Equipment slots as the core numbers them (EquipmentSlots). The shirt and the tabard are never touched.
inline constexpr std::uint8_t kRegearSlots = 19, kRegearSlotBody = 3, kRegearSlotTabard = 18;

struct RegearSlotItem
{
    std::uint32_t entry = 0, ilvl = 0; // entry 0: nothing in the slot
};

enum class RegearStep : std::uint8_t { Leave, Strip, Refill };

// After the capped gear pass, one slot: an item still above the cap (the capped search found nothing for the slot,
// the bags were full, or a BiS list item sat above it) is stripped; a slot the pass emptied is refilled.
inline RegearStep RegearSlotStep(std::uint8_t slot, RegearSlotItem before, RegearSlotItem now, std::uint32_t cap)
{
    if (slot >= kRegearSlots || slot == kRegearSlotBody || slot == kRegearSlotTabard) return RegearStep::Leave;
    if (now.entry) return now.ilvl > cap ? RegearStep::Strip : RegearStep::Leave;
    return before.entry ? RegearStep::Refill : RegearStep::Leave;
}

// The item a stripped or refilled slot held, given back when the capped search cannot fill it: a slot is never left
// empty, and the item it keeps is counted as kept.
inline std::uint32_t RegearOldEntry(RegearStep step, RegearSlotItem before, RegearSlotItem now)
{
    if (step == RegearStep::Strip) return now.entry;
    if (step == RegearStep::Refill) return before.entry;
    return 0;
}

inline std::uint32_t RegearRestoreEntry(RegearSlotItem after, std::uint32_t oldEntry)
{
    return after.entry ? 0 : oldEntry;
}

// Removed higher items left in the bags are destroyed: bots never trade them, and an upgrade pass could equip them
// again. Weapons and armour only; the bank, the mail and the guild bank are never touched.
inline bool RegearDestroysBagItem(bool weaponOrArmor, std::uint32_t ilvl, std::uint32_t cap)
{
    return weaponOrArmor && ilvl > cap;
}

template <typename Slots>
std::uint32_t RegearKept(Slots const& slots, std::uint32_t cap)
{
    std::uint32_t kept = 0;
    for (std::size_t slot = 0; slot < kRegearSlots && slot < slots.size(); ++slot)
        if (slot != kRegearSlotBody && slot != kRegearSlotTabard && slots[slot].entry && slots[slot].ilvl > cap)
            ++kept;
    return kept;
}
}

#endif
