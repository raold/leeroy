// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyGearUpPolicy.h"
#include "TestHarness.h"

#include <array>
#include <chrono>
#include <cstdint>
#include <string_view>
#include <utility>

using namespace Leeroy::Guild;
using namespace std::chrono_literals;

namespace
{
GearUpState Ready()
{
    GearUpState state;
    state.owned = state.online = state.bot = true;
    return state;
}
}

LEEROY_TEST(gear_up_allows_under_level_owned_bots_without_lowering_higher_levels)
{
    CHECK(GearUpSkipReason(Ready()).empty());
    CHECK_EQ(GearUpLevel(53, 80), 80u);
    CHECK_EQ(GearUpLevel(67, 80), 80u);
    CHECK_EQ(GearUpLevel(80, 53), 80u);
    CHECK_EQ(GearUpLevel(80, 80), 80u);
    CHECK_EQ(GearUpLevel(85, 80), 85u);
}

LEEROY_TEST(gear_up_rechecks_ownership_and_bot_identity_before_mutation)
{
    auto state = Ready();
    CHECK(GearUpSkipReason(state).empty());
    state.owned = false;
    CHECK_EQ(GearUpSkipReason(state), "not_cast");
    state = Ready();
    state.online = false;
    CHECK_EQ(GearUpSkipReason(state), "offline");
    state = Ready();
    state.bot = false;
    CHECK_EQ(GearUpSkipReason(state), "not_bot");
}

LEEROY_TEST(gear_up_rejects_each_live_state_that_makes_inventory_or_level_mutation_unsafe)
{
    using Field = bool GearUpState::*;
    std::pair<Field, std::string_view> const cases[] = {
        {&GearUpState::grouped, "grouped"}, {&GearUpState::inBg, "in_bg"},
        {&GearUpState::inInstance, "in_instance"}, {&GearUpState::teleporting, "teleporting"},
        {&GearUpState::dead, "dead"}, {&GearUpState::inCombat, "in_combat"},
        {&GearUpState::inFlight, "in_flight"}, {&GearUpState::trading, "trading"}
    };
    for (auto const& [field, reason] : cases)
    {
        auto state = Ready();
        CHECK(GearUpSkipReason(state).empty());
        state.*field = true; // Became unsafe after the command was queued.
        CHECK_EQ(GearUpSkipReason(state), reason);
    }
}

LEEROY_TEST(gear_up_ownership_and_presence_failures_take_precedence_over_stale_player_flags)
{
    auto state = Ready();
    state.owned = state.online = state.bot = false;
    state.grouped = state.inBg = state.inInstance = state.teleporting = state.inCombat = true;
    CHECK_EQ(GearUpSkipReason(state), "not_cast");
    state.owned = true;
    CHECK_EQ(GearUpSkipReason(state), "offline");
    state.online = true;
    CHECK_EQ(GearUpSkipReason(state), "not_bot");
}

LEEROY_TEST(gear_up_deadline_is_bounded_and_inclusive_at_five_minutes)
{
    auto const started = std::chrono::steady_clock::time_point{};
    CHECK(!GearUpExpired(started, started));
    CHECK(!GearUpExpired(started, started + 5min - 1ns));
    CHECK(GearUpExpired(started, started + 5min));
    CHECK(GearUpExpired(started, started + 1h));
}

namespace
{
RegearTarget CastBot()
{
    RegearTarget target;
    target.cast = target.randomAccount = true;
    return target;
}

RegearSlotItem At(std::uint32_t entry, std::uint32_t ilvl) { return {entry, ilvl}; }
}

LEEROY_TEST(regear_refuses_the_owner_non_cast_characters_and_player_accounts)
{
    CHECK(RegearRefusal(CastBot()).empty());
    auto target = CastBot();
    target.human = true; // the owner, even were he in the cast and on a bot account
    CHECK_EQ(RegearRefusal(target), "owner");
    target = CastBot();
    target.cast = false; // a random bot outside the guild, or a member under 80
    CHECK_EQ(RegearRefusal(target), "not_cast");
    target = CastBot();
    target.randomAccount = false; // a character on a player's own account
    CHECK_EQ(RegearRefusal(target), "not_bot");
    target = {};
    target.human = true;
    CHECK_EQ(RegearRefusal(target), "owner");
}

LEEROY_TEST(regear_skips_for_identity_then_his_presence_then_body_then_fences)
{
    auto target = CastBot();
    target.human = true;
    auto busy = Ready();
    busy.grouped = true;
    CHECK_EQ(RegearSkipReason(target, true, busy, "busy:realm_owner"), "owner");
    CHECK_EQ(RegearSkipReason(CastBot(), true, busy, "busy:realm_owner"), "owner_online");
    CHECK_EQ(RegearSkipReason(CastBot(), false, busy, "busy:training_owner"), "grouped");
    CHECK_EQ(RegearSkipReason(CastBot(), false, Ready(), "busy:training_cleanup"), "busy:training_cleanup");
    CHECK_EQ(RegearSkipReason(CastBot(), false, Ready(), "busy:realm_owner"), "busy:realm_owner");
    CHECK(RegearSkipReason(CastBot(), false, Ready(), "").empty());
}

LEEROY_TEST(regear_strips_only_items_above_the_cap_and_refills_emptied_slots)
{
    std::uint32_t const cap = 245;
    // Still above the cap after the capped pass: stripped, and its item noted in case nothing replaces it.
    CHECK(RegearSlotStep(0, At(1, 264), At(1, 264), cap) == RegearStep::Strip);
    CHECK_EQ(RegearOldEntry(RegearStep::Strip, At(1, 264), At(1, 264)), 1u);
    CHECK(RegearSlotStep(4, At(2, 264), At(3, 258), cap) == RegearStep::Strip); // a BiS list item above its tier
    // At or under the cap: kept as the pass left it.
    CHECK(RegearSlotStep(4, At(2, 264), At(4, 245), cap) == RegearStep::Leave);
    CHECK(RegearSlotStep(4, At(2, 264), At(4, 232), cap) == RegearStep::Leave);
    // Emptied by the pass (BiS wipes before it gears): refilled, the old item noted.
    CHECK(RegearSlotStep(6, At(5, 264), {}, cap) == RegearStep::Refill);
    CHECK_EQ(RegearOldEntry(RegearStep::Refill, At(5, 264), {}), 5u);
    // Empty before and after (an off hand beside a two-hander): left alone.
    CHECK(RegearSlotStep(16, {}, {}, cap) == RegearStep::Leave);
    CHECK_EQ(RegearOldEntry(RegearStep::Leave, At(5, 264), At(6, 245)), 0u);
}

LEEROY_TEST(regear_never_touches_the_shirt_or_tabard_or_a_slot_past_the_equipment)
{
    for (std::uint8_t slot : {kRegearSlotBody, kRegearSlotTabard, kRegearSlots, std::uint8_t(40)})
    {
        CHECK(RegearSlotStep(slot, At(1, 300), At(1, 300), 245) == RegearStep::Leave);
        CHECK(RegearSlotStep(slot, At(1, 300), {}, 245) == RegearStep::Leave);
    }
}

LEEROY_TEST(regear_never_leaves_a_slot_empty_when_its_old_item_can_return)
{
    CHECK_EQ(RegearRestoreEntry({}, 7), 7u);          // nothing capped fits: the old item comes back (kept)
    CHECK_EQ(RegearRestoreEntry(At(8, 245), 7), 0u);  // refilled from the capped search
    CHECK_EQ(RegearRestoreEntry({}, 0), 0u);          // a slot that was empty stays empty
}

LEEROY_TEST(regear_destroys_only_weapons_and_armour_above_the_cap_in_the_bags)
{
    CHECK(RegearDestroysBagItem(true, 264, 245));
    CHECK(RegearDestroysBagItem(true, 246, 245));
    CHECK(!RegearDestroysBagItem(true, 245, 245));
    CHECK(!RegearDestroysBagItem(true, 200, 245));
    CHECK(!RegearDestroysBagItem(false, 264, 245)); // a bag, a trade good, a consumable: never
}

LEEROY_TEST(regear_counts_kept_slots_above_the_cap_outside_shirt_and_tabard)
{
    std::array<RegearSlotItem, kRegearSlots> slots{};
    CHECK_EQ(RegearKept(slots, 245), 0u);
    slots[0] = At(1, 264);
    slots[4] = At(2, 245);
    slots[15] = At(3, 277);
    slots[kRegearSlotBody] = At(4, 300);
    slots[kRegearSlotTabard] = At(5, 300);
    CHECK_EQ(RegearKept(slots, 245), 2u);
    CHECK_EQ(RegearKept(slots, 277), 0u);
}

LEEROY_TEST(gear_up_skips_every_fence_in_every_mode_after_the_body_checks)
{
    CHECK(GearUpFencedSkipReason(Ready(), "").empty());
    CHECK_EQ(GearUpFencedSkipReason(Ready(), "busy:training_owner"), "busy:training_owner");
    CHECK_EQ(GearUpFencedSkipReason(Ready(), "busy:training_cleanup"), "busy:training_cleanup");
    CHECK_EQ(GearUpFencedSkipReason(Ready(), "busy:training_state"), "busy:training_state");
    CHECK_EQ(GearUpFencedSkipReason(Ready(), "busy:realm_owner"), "busy:realm_owner");
    auto state = Ready();
    state.inCombat = true;
    CHECK_EQ(GearUpFencedSkipReason(state, "busy:realm_owner"), "in_combat");
}

LEEROY_TEST(ordinary_gear_up_returns_loot_at_or_above_its_cap_and_never_strips_what_the_bot_had)
{
    std::uint32_t const cap = 245;
    // DKP loot (ICC 264, or exactly the cap) the capped pass swapped for a capped item goes back.
    CHECK(GearUpSlotStep(0, At(1, 264), At(2, 245), cap) == GearUpStep::Return);
    CHECK(GearUpSlotStep(0, At(1, 245), At(2, 245), cap) == GearUpStep::Return);
    CHECK(GearUpSlotStep(0, At(1, 264), {}, cap) == GearUpStep::Return);
    // Loot the pass left alone stays, however high.
    CHECK(GearUpSlotStep(0, At(1, 277), At(1, 277), cap) == GearUpStep::Leave);
    // Under the cap: filled or upgraded as the pass chose.
    CHECK(GearUpSlotStep(4, At(3, 200), At(4, 245), cap) == GearUpStep::Leave);
    CHECK(GearUpSlotStep(4, {}, At(4, 232), cap) == GearUpStep::Leave);
    // Something this gear-up put on above its cap comes off.
    CHECK(GearUpSlotStep(4, At(3, 200), At(5, 264), cap) == GearUpStep::Strip);
    CHECK(GearUpSlotStep(4, {}, At(5, 251), cap) == GearUpStep::Strip);
    for (std::uint8_t slot : {kRegearSlotBody, kRegearSlotTabard, kRegearSlots})
        CHECK(GearUpSlotStep(slot, At(1, 264), At(2, 300), cap) == GearUpStep::Leave);
}
