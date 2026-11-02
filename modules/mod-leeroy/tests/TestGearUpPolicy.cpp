// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyGearUpPolicy.h"
#include "TestHarness.h"

#include <chrono>
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
