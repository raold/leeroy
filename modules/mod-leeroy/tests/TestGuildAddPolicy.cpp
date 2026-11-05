// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyGuildAddPolicy.h"
#include "TestHarness.h"

#include <string>
#include <utility>
#include <vector>

using namespace Leeroy::Guild;

namespace
{
constexpr std::uint8_t Warrior = 1, Paladin = 2, Hunter = 3, Rogue = 4, Priest = 5, Shaman = 7, Mage = 8,
    Warlock = 9, Druid = 11;
constexpr auto Tank = GuildAddRole::Tank, Healer = GuildAddRole::Healer, Damage = GuildAddRole::Damage;

GuildAddCounts Picked(GuildAddPlan const& plan)
{
    GuildAddCounts counts{};
    for (auto const& pick : plan.picks)
        ++counts[static_cast<std::size_t>(pick.role)];
    return counts;
}

std::vector<std::uint32_t> Guids(GuildAddPlan const& plan)
{
    std::vector<std::uint32_t> guids;
    for (auto const& pick : plan.picks)
        guids.push_back(pick.guid);
    return guids;
}

// A deep bench: guid 100+ tanks, 200+ healers, 300+ damage, each role cycling through its classes.
std::vector<GuildAddCandidate> Bench()
{
    std::vector<GuildAddCandidate> bench;
    std::vector<std::uint8_t> const tanks{Warrior, Paladin, Druid}, healers{Priest, Shaman, Druid, Paladin},
        damage{Mage, Hunter, Warlock, Rogue, Priest, Shaman};
    for (std::uint32_t i = 0; i < 12; ++i)
        bench.push_back({100 + i, Tank, tanks[i % tanks.size()]});
    for (std::uint32_t i = 0; i < 20; ++i)
        bench.push_back({200 + i, Healer, healers[i % healers.size()]});
    for (std::uint32_t i = 0; i < 40; ++i)
        bench.push_back({300 + i, Damage, damage[i % damage.size()]});
    return bench;
}

GuildAddGate OwnerGate()
{
    GuildAddGate gate;
    gate.enabled = true;
    gate.invoker = gate.human = 1;
    gate.guild = 21;
    gate.cast = 106;
    return gate;
}

GuildAddBody Free()
{
    GuildAddBody body;
    body.online = body.cast = true;
    return body;
}
}

LEEROY_TEST(Guild_AddCompositionMatchesThePlayerbotsPatch)
{
    CHECK(GuildAddComposition(5) == GuildAddCounts({1, 1, 3}));
    CHECK(GuildAddComposition(10) == GuildAddCounts({2, 3, 5}));
    CHECK(GuildAddComposition(25) == GuildAddCounts({3, 6, 16}));
    CHECK(GuildAddComposition(40) == GuildAddCounts({4, 10, 26}));
    for (std::uint32_t size : {0u, 1u, 4u, 6u, 20u, 41u})
        CHECK(!GuildAddComposition(size));
}

LEEROY_TEST(Guild_AddParsesOnlyTheCommandTheCoreResolved)
{
    CHECK_EQ(ParseGuildAddCommand({"playerbots bot add5"}), 5u);
    CHECK_EQ(ParseGuildAddCommand({"playerbots bot add10"}), 10u);
    CHECK_EQ(ParseGuildAddCommand({"playerbots bot add25"}), 25u);
    CHECK_EQ(ParseGuildAddCommand({"playerbots bot add40"}), 40u);
    // Playerbots ignores words after the subcommand, so they do not change what it would have done.
    CHECK_EQ(ParseGuildAddCommand({"playerbots bot add25 warrior"}), 25u);
    CHECK_EQ(ParseGuildAddCommand({"playerbots bot  add40"}), 40u);
    // The core matched the command path case-insensitively; its canonical spelling may differ in case.
    CHECK_EQ(ParseGuildAddCommand({"PlayerBots Bot add5"}), 5u);
    // Playerbots compares the subcommand with strcmp: anything else is not its add command.
    CHECK_EQ(ParseGuildAddCommand({"playerbots bot ADD5"}), 0u);
    CHECK_EQ(ParseGuildAddCommand({"playerbots bot Add10"}), 0u);
    CHECK_EQ(ParseGuildAddCommand({"playerbots bot add5x"}), 0u);
    CHECK_EQ(ParseGuildAddCommand({"playerbots bot add6"}), 0u);
    CHECK_EQ(ParseGuildAddCommand({"playerbots bot add50"}), 0u);
    CHECK_EQ(ParseGuildAddCommand({"playerbots bot add"}), 0u);
    CHECK_EQ(ParseGuildAddCommand({"playerbots bot add\t5"}), 0u);
    CHECK_EQ(ParseGuildAddCommand({"playerbots bot addclass warrior"}), 0u);
    CHECK_EQ(ParseGuildAddCommand({"playerbots bot add Leeroy"}), 0u);
    CHECK_EQ(ParseGuildAddCommand({"playerbots bot list add5"}), 0u);
    // Other commands, even with the same words further on.
    CHECK_EQ(ParseGuildAddCommand({"playerbots rndbot add5"}), 0u);
    CHECK_EQ(ParseGuildAddCommand({"playerbots botx add5"}), 0u);
    CHECK_EQ(ParseGuildAddCommand({"playerbot bot add5"}), 0u);
    CHECK_EQ(ParseGuildAddCommand({"bot add5"}), 0u);
    CHECK_EQ(ParseGuildAddCommand({"add5"}), 0u);
    // Nothing resolved, or an ambiguous abbreviation the core never invokes.
    CHECK_EQ(ParseGuildAddCommand({}), 0u);
    CHECK_EQ(ParseGuildAddCommand({""}), 0u);
    CHECK_EQ(ParseGuildAddCommand({"playerbots bot add5", "playerbots bot add5"}), 0u);
}

LEEROY_TEST(Guild_AddLeavesEveryoneButTheOwnerToPlayerbots)
{
    CHECK(GuildAddIntercepts(OwnerGate()));
    auto gate = OwnerGate();
    gate.invoker = 2;
    CHECK(!GuildAddIntercepts(gate));
    gate = OwnerGate();
    gate.console = true;
    CHECK(!GuildAddIntercepts(gate));
    gate = OwnerGate();
    gate.human = gate.invoker = 0;
    CHECK(!GuildAddIntercepts(gate));
    gate = OwnerGate();
    gate.enabled = false;
    CHECK(!GuildAddIntercepts(gate));
    gate = OwnerGate();
    gate.guild = 0;
    CHECK(!GuildAddIntercepts(gate));
    gate = OwnerGate();
    gate.cast = 0;
    CHECK(!GuildAddIntercepts(gate));
}

LEEROY_TEST(Guild_AddSkipsEveryBusyOrUnsuitableMember)
{
    CHECK(GuildAddSkipReason(Free()).empty());
    auto body = Free();
    body.online = false;
    body.dead = true;
    CHECK_EQ(GuildAddSkipReason(body), "offline");
    body = Free();
    body.cast = false;
    CHECK_EQ(GuildAddSkipReason(body), "not_cast");
    body = Free();
    body.fence = "busy:training_owner";
    body.grouped = true;
    CHECK_EQ(GuildAddSkipReason(body), "busy:training_owner");
    std::vector<std::pair<bool GuildAddBody::*, char const*>> const flags{
        {&GuildAddBody::otherTeam, "other_team"}, {&GuildAddBody::dead, "dead"},
        {&GuildAddBody::inCombat, "in_combat"}, {&GuildAddBody::grouped, "grouped"},
        {&GuildAddBody::inBattleground, "in_bg"}, {&GuildAddBody::queued, "queued"},
        {&GuildAddBody::teleporting, "teleporting"}, {&GuildAddBody::inFlight, "in_flight"},
        {&GuildAddBody::elsewhere, "in_instance"}, {&GuildAddBody::locked, "locked"}};
    for (auto const& [flag, reason] : flags)
    {
        body = Free();
        body.*flag = true;
        CHECK_EQ(GuildAddSkipReason(body), std::string(reason));
    }
}

LEEROY_TEST(Guild_AddFillsEachSizeToItsComposition)
{
    // He is alone and plays damage: he takes one damage slot of every size.
    for (auto const& [size, open] : std::vector<std::pair<std::uint32_t, GuildAddCounts>>{
             {5, {1, 1, 2}}, {10, {2, 3, 4}}, {25, {3, 6, 15}}, {40, {4, 10, 25}}})
    {
        auto const plan = PlanGuildAdd(size, 1, {{Damage, Rogue}}, Bench());
        CHECK(!plan.full);
        CHECK(plan.open == open);
        CHECK(Picked(plan) == open);
        auto const messages = GuildAddMessages(plan, open, 0, {});
        CHECK_EQ(messages.size(), std::size_t(1));
        CHECK_EQ(messages.front(), "Adding " + std::to_string(open[0]) + " tanks, " + std::to_string(open[1])
            + " healers and " + std::to_string(open[2]) + " damage from the guild for a " + std::to_string(size)
            + "-man group");
    }
    // A tank owner leaves no tank slot in a 5-man.
    CHECK(PlanGuildAdd(5, 1, {{Tank, Warrior}}, Bench()).open == GuildAddCounts({0, 1, 3}));
}

LEEROY_TEST(Guild_AddCountsMembersAlreadyInHisGroup)
{
    std::vector<GuildAddMember> const group{{Damage, Mage}, {Tank, Warrior}, {Healer, Priest}, {Healer, Shaman}};
    auto const plan = PlanGuildAdd(10, 4, group, Bench());
    CHECK(plan.open == GuildAddCounts({1, 1, 4}));
    CHECK(Picked(plan) == plan.open);
    // Offline members fill slots but have no known role: the open slots shrink from damage first.
    auto const offline = PlanGuildAdd(5, 3, {{Damage, Mage}}, Bench());
    CHECK(offline.open == GuildAddCounts({1, 1, 0}));
    // More of a role than the composition wants: the extra one still takes a slot, damage then healers give way.
    auto const extra = PlanGuildAdd(5, 4, {{Damage, Mage}, {Damage, Rogue}, {Damage, Hunter}, {Damage, Warlock}},
        Bench());
    CHECK(extra.open == GuildAddCounts({1, 0, 0}));
    CHECK(Picked(extra) == GuildAddCounts({1, 0, 0}));
}

LEEROY_TEST(Guild_AddRerunFillsOnlyTheGaps)
{
    // First run: no tank was free, so that slot stayed open.
    std::vector<GuildAddCandidate> bench{{200, Healer, Priest}, {300, Damage, Mage}, {301, Damage, Hunter}};
    auto const first = PlanGuildAdd(5, 1, {{Damage, Rogue}}, bench);
    CHECK(first.open == GuildAddCounts({1, 1, 2}));
    CHECK(Guids(first) == std::vector<std::uint32_t>({200, 300, 301}));

    // Second run: those three joined; a tank and more damage are free now. Only the tank is added.
    std::vector<GuildAddMember> const group{{Damage, Rogue}, {Healer, Priest}, {Damage, Mage}, {Damage, Hunter}};
    auto const second = PlanGuildAdd(5, 4, group, {{100, Tank, Warrior}, {302, Damage, Warlock}});
    CHECK(second.open == GuildAddCounts({1, 0, 0}));
    CHECK(Guids(second) == std::vector<std::uint32_t>({100}));

    // Third run: the group is complete.
    std::vector<GuildAddMember> whole = group;
    whole.push_back({Tank, Warrior});
    auto const third = PlanGuildAdd(5, 5, whole, {{302, Damage, Warlock}});
    CHECK(third.full);
    CHECK(third.picks.empty());
}

LEEROY_TEST(Guild_AddSaysWhenHisGroupIsAlreadyAtSize)
{
    auto const plan = PlanGuildAdd(5, 5, {{Damage, Rogue}}, Bench());
    CHECK(plan.full);
    CHECK(plan.picks.empty());
    CHECK(GuildAddMessages(plan, {}, 0, {}) == std::vector<std::string>({"Your group already has 5 members"}));
    auto const bigger = PlanGuildAdd(10, 25, {{Damage, Rogue}}, Bench());
    CHECK(bigger.full);
    CHECK(GuildAddMessages(bigger, {}, 0, {}) == std::vector<std::string>({"Your group already has 25 members"}));
}

LEEROY_TEST(Guild_AddReportsEmptySlotsPerRole)
{
    // No tank and one healer free for a 10-man: the rest of those roles stay empty, never filled by another role.
    std::vector<GuildAddCandidate> bench{{200, Healer, Priest}};
    for (std::uint32_t i = 0; i < 10; ++i)
        bench.push_back({300 + i, Damage, Mage});
    auto const plan = PlanGuildAdd(10, 1, {{Damage, Rogue}}, bench);
    CHECK(plan.open == GuildAddCounts({2, 3, 4}));
    CHECK(Picked(plan) == GuildAddCounts({0, 1, 4}));
    auto const messages = GuildAddMessages(plan, Picked(plan), 0, {{"grouped", 40}, {"in_combat", 2},
        {"busy:realm_owner", 2}, {"dead", 0}});
    CHECK(messages == std::vector<std::string>({
        "Adding 0 tanks, 1 healers and 4 damage from the guild for a 10-man group",
        "4 slots left empty: no free guild member for 2 tanks and 2 healers",
        "Not free: 40 grouped, 2 busy:realm_owner, 2 in_combat"}));

    // A pick that could not join counts as empty in its own role; a member left behind is said too.
    auto const single = PlanGuildAdd(5, 1, {{Damage, Rogue}}, Bench());
    CHECK(GuildAddMessages(single, {1, 1, 1}, 1, {}) == std::vector<std::string>({
        "Adding 1 tanks, 1 healers and 1 damage from the guild for a 5-man group",
        "1 slot left empty: no free guild member for 1 damage",
        "1 member joined but could not be brought to you"}));
    CHECK(GuildAddMessages(single, {0, 0, 0}, 0, {}) == std::vector<std::string>({
        "Adding 0 tanks, 0 healers and 0 damage from the guild for a 5-man group",
        "4 slots left empty: no free guild member for 1 tank, 1 healer and 2 damage"}));
}

LEEROY_TEST(Guild_AddSpreadsClassesWithinARole)
{
    // He is a mage: other classes come first, then a second mage, lowest guid first on ties.
    std::vector<GuildAddCandidate> const bench{{301, Damage, Mage}, {302, Damage, Mage}, {303, Damage, Hunter},
        {304, Damage, Hunter}, {305, Damage, Warlock}};
    auto const plan = PlanGuildAdd(5, 2, {{Damage, Mage}, {Tank, Warrior}}, bench);
    CHECK(plan.open == GuildAddCounts({0, 1, 2}));
    CHECK(Guids(plan) == std::vector<std::uint32_t>({303, 305}));
    auto const more = PlanGuildAdd(10, 2, {{Damage, Mage}, {Tank, Warrior}}, bench);
    CHECK(Guids(more) == std::vector<std::uint32_t>({303, 305, 301, 304}));

    // Classes picked for one role count for the next: a paladin tank makes a paladin healer the last choice.
    auto const crossed = PlanGuildAdd(5, 1, {{Damage, Rogue}}, {{100, Tank, Paladin}, {200, Healer, Paladin},
        {201, Healer, Druid}, {300, Damage, Hunter}, {301, Damage, Mage}});
    CHECK(Guids(crossed) == std::vector<std::uint32_t>({100, 201, 300, 301}));
}

LEEROY_TEST(Guild_AddIsDeterministicAndIgnoresBadCandidates)
{
    std::vector<GuildAddCandidate> bench{{305, Damage, Mage}, {0, Damage, Hunter}, {301, Damage, Mage},
        {301, Damage, Mage}, {303, Damage, Mage}};
    auto const plan = PlanGuildAdd(5, 2, {{Tank, Warrior}, {Healer, Priest}}, bench);
    CHECK(Guids(plan) == std::vector<std::uint32_t>({301, 303, 305}));
    std::vector<GuildAddCandidate> reversed(bench.rbegin(), bench.rend());
    CHECK(Guids(PlanGuildAdd(5, 2, {{Tank, Warrior}, {Healer, Priest}}, reversed)) == Guids(plan));
    CHECK(PlanGuildAdd(6, 1, {{Damage, Mage}}, bench).picks.empty());
}
