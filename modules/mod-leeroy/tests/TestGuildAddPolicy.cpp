// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyGuildAddPolicy.h"
#include "LeeroyText.h"
#include "TestHarness.h"

#include <algorithm>
#include <initializer_list>
#include <string>
#include <tuple>
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

std::vector<std::uint32_t> Guids(GuildAddPlan const& plan, GuildAddRole role)
{
    std::vector<std::uint32_t> guids;
    for (auto const& pick : plan.picks)
        if (pick.role == role)
            guids.push_back(pick.guid);
    return guids;
}

// "1 2 3", so a failed check shows the order it got.
std::string List(std::vector<std::uint32_t> const& guids)
{
    std::string list;
    for (auto guid : guids)
        list += (list.empty() ? "" : " ") + std::to_string(guid);
    return list;
}

using B = GuildAddBuff;
GuildAddBuffs Bits(std::initializer_list<GuildAddBuff> buffs)
{
    GuildAddBuffs bits = 0;
    for (auto buff : buffs)
        bits |= GuildAddBuffBit(buff);
    return bits;
}

// What his group brings once every pick has joined.
GuildAddBuffs After(GuildAddPlan const& plan)
{
    GuildAddBuffs covered = plan.covered;
    for (auto const& pick : plan.picks)
        covered |= pick.buffs;
    return covered;
}

constexpr std::uint8_t DeathKnight = 6;
// Talent trees in the client's order, per class.
constexpr std::int8_t Arms = 0, Fury = 1, ProtWarrior = 2;
constexpr std::int8_t HolyPaladin = 0, ProtPaladin = 1, Ret = 2;
constexpr std::int8_t BeastMastery = 0, Marksmanship = 1, Survival = 2;
constexpr std::int8_t Assassination = 0, Combat = 1, Subtlety = 2;
constexpr std::int8_t Discipline = 0, HolyPriest = 1, Shadow = 2;
constexpr std::int8_t Blood = 0, FrostDk = 1, Unholy = 2;
constexpr std::int8_t Elemental = 0, Enhancement = 1, RestoShaman = 2;
constexpr std::int8_t Arcane = 0, Fire = 1, FrostMage = 2;
constexpr std::int8_t Affliction = 0, Demonology = 1, Destruction = 2;
constexpr std::int8_t Balance = 0, Feral = 1, RestoDruid = 2;
constexpr std::uint8_t Classes[] = {Warrior, Paladin, Hunter, Rogue, Priest, DeathKnight, Shaman, Mage, Warlock, Druid};

// Healers of every kind, gear spread so each kind's best is clear: guid 200+ Holy paladins, 210+ Discipline, 220+
// Holy priests, 230+ Restoration shamans, 240+ Restoration druids.
std::vector<GuildAddCandidate> Healers(bool tankHealers = true, bool absorbs = true)
{
    std::vector<GuildAddCandidate> healers;
    if (tankHealers)
        healers.insert(healers.end(), {{200, Healer, Paladin, HolyPaladin, 240},
            {201, Healer, Paladin, HolyPaladin, 230}, {202, Healer, Paladin, HolyPaladin, 220}});
    if (absorbs)
        healers.insert(healers.end(), {{210, Healer, Priest, Discipline, 235}, {211, Healer, Priest, Discipline, 225}});
    healers.insert(healers.end(), {{220, Healer, Priest, HolyPriest, 250}, {221, Healer, Priest, HolyPriest, 215},
        {230, Healer, Shaman, RestoShaman, 245}, {231, Healer, Shaman, RestoShaman, 212},
        {240, Healer, Druid, RestoDruid, 248}, {241, Healer, Druid, RestoDruid, 210}});
    return healers;
}

// A 25-man pool: four tanks, six healers with no shaman among them, and damage whose best gear brings neither
// Bloodlust nor Replenishment.
std::vector<GuildAddCandidate> RaidPool()
{
    return {{100, Tank, Warrior, ProtWarrior, 245}, {101, Tank, Paladin, ProtPaladin, 240},
        {102, Tank, DeathKnight, Blood, 235}, {103, Tank, Warrior, ProtWarrior, 250},
        {200, Healer, Paladin, HolyPaladin, 240}, {201, Healer, Paladin, HolyPaladin, 235},
        {202, Healer, Priest, Discipline, 238}, {203, Healer, Priest, HolyPriest, 230},
        {204, Healer, Druid, RestoDruid, 232}, {205, Healer, Druid, RestoDruid, 228},
        {300, Damage, Mage, Arcane, 250}, {301, Damage, Mage, Fire, 249}, {302, Damage, Hunter, BeastMastery, 248},
        {303, Damage, Hunter, Marksmanship, 247}, {304, Damage, Warlock, Affliction, 246},
        {305, Damage, Rogue, Assassination, 245}, {306, Damage, Shaman, Elemental, 200},
        {307, Damage, Priest, Shadow, 201}};
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

LEEROY_TEST(Guild_AddRefusesASizeHisCopyCannotHold)
{
    // Outside an instance every size goes ahead.
    for (std::uint32_t size : {5u, 10u, 25u, 40u})
        CHECK(GuildAddSizeRefusal(size, {}).empty());

    // In a 5-man (normal or heroic) only add5 fits: a raid size is refused before his group changes.
    GuildAddPlace const dungeon{5, false};
    CHECK(GuildAddSizeRefusal(5, dungeon).empty());
    for (std::uint32_t size : {10u, 25u, 40u})
        CHECK_EQ(GuildAddSizeRefusal(size, dungeon), "You are in a 5-man dungeon: add5 tops this party up; add10, "
            "add25 and add40 work outside or in a raid");

    // A raid holds its own size: smaller ones go ahead, bigger ones are refused.
    GuildAddPlace const ten{10, true};
    CHECK(GuildAddSizeRefusal(5, ten).empty());
    CHECK(GuildAddSizeRefusal(10, ten).empty());
    CHECK_EQ(GuildAddSizeRefusal(25, ten), "You are in a 10-man raid: add10 fits here; add25 and add40 work outside "
        "or in a bigger raid");
    CHECK_EQ(GuildAddSizeRefusal(40, ten), GuildAddSizeRefusal(25, ten));
    GuildAddPlace const twentyFive{25, true};
    CHECK(GuildAddSizeRefusal(25, twentyFive).empty());
    CHECK_EQ(GuildAddSizeRefusal(40, twentyFive), "You are in a 25-man raid: add25 fits here; add40 works outside or "
        "in a bigger raid");
    CHECK_EQ(GuildAddSizeRefusal(25, {20, true}), "You are in a 20-man raid: add10 fits here; add25 and add40 work "
        "outside or in a bigger raid");
    for (std::uint32_t size : {5u, 10u, 25u, 40u})
        CHECK(GuildAddSizeRefusal(size, {40, true}).empty());

    // A copy smaller than any size, and a size the command does not know.
    CHECK_EQ(GuildAddSizeRefusal(5, {3, false}), "You are in a 3-man dungeon: no add command fits here; add5, add10, "
        "add25 and add40 work outside or in a raid");
    CHECK(GuildAddSizeRefusal(6, dungeon).empty());
}

LEEROY_TEST(Guild_AddBringsNobodyIntoACopyTooSmallForHisGroup)
{
    // Outside an instance, and any group that fits its copy, is brought in as before.
    CHECK(GuildAddEntryRefusal(40, {}).empty());
    CHECK(GuildAddEntryRefusal(1, {5, false}).empty());
    CHECK(GuildAddEntryRefusal(5, {5, false}).empty());
    CHECK(GuildAddEntryRefusal(25, {25, true}).empty());
    // A group he built bigger than the copy: the core would turn all but a few away and send them home.
    CHECK_EQ(GuildAddEntryRefusal(25, {5, false}), "Your group of 25 does not fit this 5-man dungeon, so nobody was "
        "brought in");
    CHECK_EQ(GuildAddEntryRefusal(6, {5, false}), "Your group of 6 does not fit this 5-man dungeon, so nobody was "
        "brought in");
    CHECK_EQ(GuildAddEntryRefusal(24, {10, true}), "Your group of 24 does not fit this 10-man raid, so nobody was "
        "brought in");
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

LEEROY_TEST(Guild_AddSpreadsClassesWhenNothingElseDecides)
{
    // Coverage, gear and the melee/ranged split all tie (he is a mage, so Arcane Intellect is his already): other
    // classes come first, then a second mage, lowest guid first on ties.
    std::vector<GuildAddCandidate> const bench{{301, Damage, Mage}, {302, Damage, Mage}, {303, Damage, Hunter},
        {304, Damage, Hunter}};
    auto const plan = PlanGuildAdd(5, 2, {{Damage, Mage}, {Tank, Warrior}}, bench);
    CHECK(plan.open == GuildAddCounts({0, 1, 2}));
    CHECK_EQ(List(Guids(plan)), "303 301");
    auto const more = PlanGuildAdd(10, 2, {{Damage, Mage}, {Tank, Warrior}}, bench);
    CHECK_EQ(List(Guids(more)), "303 301 304 302");

    // Classes picked for one role count for the next: a paladin tank makes a paladin healer the last choice. The mage
    // comes before the hunter for the Arcane Intellect nobody brings yet.
    auto const crossed = PlanGuildAdd(5, 1, {{Damage, Rogue}}, {{100, Tank, Paladin}, {200, Healer, Paladin},
        {201, Healer, Druid}, {300, Damage, Hunter}, {301, Damage, Mage}});
    CHECK_EQ(List(Guids(crossed)), "100 201 301 300");
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

LEEROY_TEST(Guild_AddSpecFactsFollowWotlkTalents)
{
    // Exact facts for the specs the picks lean on.
    std::vector<std::tuple<std::uint8_t, std::int8_t, GuildAddBuffs, bool>> const specs{
        {Warrior, Arms, Bits({B::BleedDamage, B::PhysicalDamageTaken, B::ArmorReduction}), true},
        {Warrior, Fury, Bits({B::PhysicalCrit, B::ArmorReduction}), true},
        {Warrior, ProtWarrior, Bits({B::ArmorReduction}), true},
        {Paladin, ProtPaladin, Bits({B::Kings}), true},
        {Paladin, Ret, Bits({B::Replenishment, B::Damage, B::CritTaken, B::Kings}), true},
        {Hunter, BeastMastery, Bits({B::Damage}), false},
        {Hunter, Marksmanship, Bits({B::AttackPower}), false},
        {Hunter, Survival, Bits({B::Replenishment}), false},
        {Rogue, Assassination, Bits({B::CritTaken, B::ArmorReduction}), true},
        {Rogue, Combat, Bits({B::PhysicalDamageTaken, B::ArmorReduction}), true},
        {Rogue, Subtlety, Bits({B::ArmorReduction}), true},
        {Priest, Shadow, Bits({B::Replenishment, B::SpellHit, B::Fortitude}), false},
        {DeathKnight, Blood, Bits({B::AttackPower}), true},
        {DeathKnight, FrostDk, Bits({B::MeleeHaste}), true},
        {DeathKnight, Unholy, Bits({B::SpellDamageTaken}), true},
        {Shaman, Elemental, Bits({B::Bloodlust, B::SpellCrit, B::CritTaken, B::SpellPower}), false},
        {Shaman, Enhancement, Bits({B::Bloodlust, B::MeleeHaste, B::AttackPower}), true},
        {Mage, Arcane, Bits({B::Damage, B::ArcaneIntellect}), false},
        {Mage, Fire, Bits({B::ArcaneIntellect}), false},
        {Mage, FrostMage, Bits({B::Replenishment, B::ArcaneIntellect}), false},
        {Warlock, Affliction, Bits({B::SpellDamageTaken}), false},
        {Warlock, Demonology, Bits({B::SpellDamageTaken, B::SpellPower}), false},
        {Warlock, Destruction, Bits({B::Replenishment, B::SpellDamageTaken}), false},
        {Druid, Balance, Bits({B::SpellCrit, B::SpellDamageTaken, B::SpellHit, B::MarkOfTheWild, B::BattleRes}), false},
        {Druid, Feral, Bits({B::PhysicalCrit, B::BleedDamage, B::MarkOfTheWild, B::BattleRes}), true}};
    for (auto const& [cls, tab, buffs, melee] : specs)
    {
        auto const facts = GuildAddSpecFacts(cls, tab, Damage);
        CHECK_EQ(facts.buffs, buffs);
        CHECK_EQ(facts.melee, melee);
        CHECK(facts.healer == GuildAddHealer::None);
    }

    // An unknown tree brings only what the whole class brings.
    std::vector<std::pair<std::uint8_t, GuildAddBuffs>> const classWide{{Warrior, Bits({B::ArmorReduction})},
        {Paladin, Bits({B::Kings})}, {Hunter, 0}, {Rogue, Bits({B::ArmorReduction})}, {Priest, Bits({B::Fortitude})},
        {DeathKnight, 0}, {Shaman, Bits({B::Bloodlust})}, {Mage, Bits({B::ArcaneIntellect})},
        {Warlock, Bits({B::SpellDamageTaken})}, {Druid, Bits({B::MarkOfTheWild, B::BattleRes})}};
    for (auto const& [cls, buffs] : classWide)
    {
        CHECK_EQ(GuildAddSpecFacts(cls, -1, Damage).buffs, buffs);
        CHECK_EQ(GuildAddSpecFacts(cls, 3, Damage).buffs, buffs);
    }

    // Healers: what each healing tree is for, and they stand at range; a tree that cannot heal is no healer kind.
    std::vector<std::tuple<std::uint8_t, std::int8_t, GuildAddHealer, GuildAddBuffs>> const healers{
        {Paladin, HolyPaladin, GuildAddHealer::Tank, Bits({B::Kings})},
        {Priest, Discipline, GuildAddHealer::Absorb, Bits({B::Fortitude})},
        {Priest, HolyPriest, GuildAddHealer::RaidPriest, Bits({B::Fortitude})},
        {Shaman, RestoShaman, GuildAddHealer::RaidShaman, Bits({B::Bloodlust})},
        {Druid, RestoDruid, GuildAddHealer::RaidDruid, Bits({B::MarkOfTheWild, B::BattleRes})},
        {Priest, Shadow, GuildAddHealer::None, Bits({B::Replenishment, B::SpellHit, B::Fortitude})},
        {Druid, -1, GuildAddHealer::None, Bits({B::MarkOfTheWild, B::BattleRes})}};
    for (auto const& [cls, tab, kind, buffs] : healers)
    {
        auto const facts = GuildAddSpecFacts(cls, tab, Healer);
        CHECK(facts.healer == kind);
        CHECK_EQ(facts.buffs, buffs);
        CHECK(!facts.melee);
    }
    CHECK(GuildAddSpecFacts(Paladin, HolyPaladin, Damage).healer == GuildAddHealer::None);

    // Tanks stand in melee whatever their tree; a bear and a frost death knight keep their tree's buffs.
    CHECK(GuildAddSpecFacts(Druid, Feral, Tank).melee);
    CHECK_EQ(GuildAddSpecFacts(Druid, Feral, Tank).buffs, GuildAddSpecFacts(Druid, Feral, Damage).buffs);
    CHECK_EQ(GuildAddSpecFacts(DeathKnight, FrostDk, Tank).buffs, Bits({B::MeleeHaste}));

    // Every buff has a source, Replenishment comes from exactly its five talents and a battle res only from druids
    // (a death knight's Raise Ally raises a ghoul in 3.3.5, not the member).
    GuildAddBuffs all = 0;
    std::vector<std::pair<std::uint8_t, std::int8_t>> replenishment, battleRes;
    for (std::uint8_t cls : Classes)
        for (std::int8_t tab = 0; tab < 3; ++tab)
        {
            auto const buffs = GuildAddSpecFacts(cls, tab, Damage).buffs;
            all |= buffs;
            if (buffs & GuildAddBuffBit(B::Replenishment))
                replenishment.emplace_back(cls, tab);
            if (buffs & GuildAddBuffBit(B::BattleRes))
                battleRes.emplace_back(cls, tab);
        }
    CHECK_EQ(all, (GuildAddBuffs(1) << kGuildAddBuffs) - 1);
    CHECK(replenishment == (std::vector<std::pair<std::uint8_t, std::int8_t>>{{Paladin, Ret}, {Hunter, Survival},
        {Priest, Shadow}, {Mage, FrostMage}, {Warlock, Destruction}}));
    CHECK(battleRes == (std::vector<std::pair<std::uint8_t, std::int8_t>>{{Druid, Balance}, {Druid, Feral},
        {Druid, RestoDruid}}));

    // Bloodlust and Replenishment weigh most, then the % buffs and debuffs, then the rest.
    for (std::size_t i = 0; i < kGuildAddBuffs; ++i)
    {
        auto const buff = static_cast<GuildAddBuff>(i);
        std::uint32_t const weight = i < 2 ? 100 : i < 12 ? 10 : 1;
        CHECK_EQ(GuildAddBuffWeight(buff), weight);
    }
}

LEEROY_TEST(Guild_Add25PicksBloodlustAndReplenishmentFirst)
{
    auto const plan = PlanGuildAdd(25, 1, {{Damage, Rogue, Combat}}, RaidPool());
    CHECK(plan.open == GuildAddCounts({3, 6, 15}));
    CHECK_EQ(plan.covered, Bits({B::PhysicalDamageTaken, B::ArmorReduction}));
    // The lowest-geared damage come first: the shaman's Bloodlust, then the shadow priest's Replenishment. Then the
    // % buffs nobody has (the Arcane mage's 3% damage, the warlock's Curse of the Elements), then gear: the
    // assassin's 3% crit taken came with the shaman's Totem of Wrath, the hunter's Trueshot Aura with the tank's
    // Abomination's Might.
    CHECK_EQ(List(Guids(plan, Damage)), "306 307 300 304 301 302 303 305");
    CHECK_EQ(plan.picks.size(), std::size_t(3 + 6 + 8));
    for (auto const& pick : plan.picks)
        if (pick.guid == 306)
            CHECK_EQ(pick.buffs, GuildAddSpecFacts(Shaman, Elemental, Damage).buffs);
}

LEEROY_TEST(Guild_AddTanksPreferGearThenADifferentClass)
{
    // The first tank is the best geared; the next is of a tank class not yet tanking, before a better warrior.
    auto const plan = PlanGuildAdd(25, 1, {{Damage, Rogue, Combat}}, RaidPool());
    CHECK_EQ(List(Guids(plan, Tank)), "103 101 102");
    std::vector<GuildAddCandidate> const tanks{{100, Tank, Warrior, ProtWarrior, 230},
        {101, Tank, Warrior, ProtWarrior, 250}, {102, Tank, Paladin, ProtPaladin, 220}};
    CHECK_EQ(List(Guids(PlanGuildAdd(10, 1, {{Damage, Rogue}}, tanks))), "101 102");
    // His warrior tank counts: a death knight before the better-geared warrior.
    CHECK_EQ(List(Guids(PlanGuildAdd(10, 2, {{Damage, Rogue}, {Tank, Warrior, ProtWarrior}},
        {{100, Tank, Warrior, ProtWarrior, 260}, {101, Tank, DeathKnight, Blood, 230}}))), "101");
}

LEEROY_TEST(Guild_AddHealersFollowTheSizeTemplate)
{
    std::vector<GuildAddMember> const him{{Damage, Rogue, Combat}};
    // 5: a Holy paladin or a Discipline priest, by gear, before the better-geared raid healers.
    CHECK_EQ(List(Guids(PlanGuildAdd(5, 1, him, Healers()), Healer)), "200");
    // 10: Holy paladin, Discipline priest, then the best-geared raid healer.
    CHECK_EQ(List(Guids(PlanGuildAdd(10, 1, him, Healers()), Healer)), "200 210 220");
    // 25: two Holy paladins, one Discipline priest, and one raid healer of each kind.
    CHECK_EQ(List(Guids(PlanGuildAdd(25, 1, him, Healers()), Healer)), "200 210 220 201 240 230");
    // 40: three Holy paladins, two Discipline priests, five raid healers kept mixed.
    CHECK_EQ(List(Guids(PlanGuildAdd(40, 1, him, Healers()), Healer)), "200 210 220 201 240 211 230 202 221 231");
}

LEEROY_TEST(Guild_AddHealersFallBackByGear)
{
    std::vector<GuildAddMember> const him{{Damage, Rogue, Combat}};
    // No Holy paladin: its slot goes to the best-geared healer left, here a second druid.
    std::vector<GuildAddCandidate> const noPaladin{{210, Healer, Priest, Discipline, 230},
        {240, Healer, Druid, RestoDruid, 245}, {241, Healer, Druid, RestoDruid, 210},
        {230, Healer, Shaman, RestoShaman, 200}};
    CHECK_EQ(List(Guids(PlanGuildAdd(10, 1, him, noPaladin), Healer)), "210 240 241");
    // Raid healers only: the three raid slots mix, the Holy paladin and Discipline slots take the rest by gear.
    CHECK_EQ(List(Guids(PlanGuildAdd(25, 1, him, Healers(false, false)), Healer)), "220 240 230 221 231 241");
    // A 5-man with neither preferred kind free takes the best-geared healer.
    CHECK_EQ(List(Guids(PlanGuildAdd(5, 1, him, Healers(false, false)), Healer)), "220");
    // Only a Discipline priest of the preferred kinds: it comes before better gear.
    CHECK_EQ(List(Guids(PlanGuildAdd(5, 1, him, Healers(false, true)), Healer)), "210");
}

LEEROY_TEST(Guild_AddGearBreaksTies)
{
    // Two Arcane mages bring the same: the better geared first.
    std::vector<GuildAddCandidate> const mages{{300, Damage, Mage, Arcane, 220}, {301, Damage, Mage, Arcane, 240}};
    CHECK_EQ(List(Guids(PlanGuildAdd(5, 1, {{Damage, Rogue, Combat}}, mages))), "301 300");
    // Two Holy paladins: the better geared.
    std::vector<GuildAddCandidate> const paladins{{200, Healer, Paladin, HolyPaladin, 210},
        {201, Healer, Paladin, HolyPaladin, 230}};
    CHECK_EQ(List(Guids(PlanGuildAdd(5, 1, {{Damage, Rogue, Combat}}, paladins))), "201");
}

LEEROY_TEST(Guild_AddBalancesMeleeAndRangedOnTies)
{
    // He is a hunter. Nobody brings anything and gear ties: a melee first to even the split, then the classes his
    // group has fewest of and the lowest guid.
    std::vector<GuildAddCandidate> const bench{{300, Damage, Hunter, -1, 200}, {301, Damage, DeathKnight, -1, 200},
        {302, Damage, DeathKnight, -1, 200}};
    CHECK_EQ(List(Guids(PlanGuildAdd(10, 1, {{Damage, Hunter}}, bench))), "301 300 302");
    // Gear comes before the split.
    std::vector<GuildAddCandidate> const geared{{300, Damage, Hunter, -1, 201}, {301, Damage, DeathKnight, -1, 200}};
    CHECK_EQ(List(Guids(PlanGuildAdd(10, 1, {{Damage, Hunter}}, geared))), "300 301");
}

LEEROY_TEST(Guild_AddDoesNotChaseBuffsTwice)
{
    // His group has a shaman: the enhancement shaman's Bloodlust counts for nothing, so Replenishment wins.
    std::vector<GuildAddCandidate> const bench{{300, Damage, Shaman, Enhancement, 250},
        {301, Damage, Hunter, Survival, 190}};
    auto const plan = PlanGuildAdd(5, 2, {{Damage, Rogue, Combat}, {Damage, Shaman, Elemental}}, bench);
    CHECK(plan.open == GuildAddCounts({1, 1, 1}));
    CHECK_EQ(List(Guids(plan)), "301");
    // Without his shaman the enhancement shaman's Bloodlust outweighs it.
    CHECK_EQ(List(Guids(PlanGuildAdd(5, 2, {{Damage, Rogue, Combat}, {Damage, Mage, Fire}}, bench))), "300");

    // A second elemental shaman adds nothing once the first is picked: it goes last.
    std::vector<GuildAddCandidate> const shamans{{300, Damage, Shaman, Elemental, 200},
        {301, Damage, Shaman, Elemental, 199}, {302, Damage, Mage, Fire, 250},
        {303, Damage, Hunter, BeastMastery, 240}};
    CHECK_EQ(List(Guids(PlanGuildAdd(10, 1, {{Damage, Rogue, Combat}}, shamans))), "300 303 302 301");
}

LEEROY_TEST(Guild_AddGapFillCountsWhatHisGroupHas)
{
    // His group has a Holy paladin healing and a warrior tanking: the healers are the Discipline priest and a raid
    // healer, not the best-geared Holy paladin; the tank is not a second warrior.
    std::vector<GuildAddCandidate> const bench{{100, Tank, Warrior, ProtWarrior, 260},
        {101, Tank, DeathKnight, Blood, 230}, {200, Healer, Paladin, HolyPaladin, 250},
        {210, Healer, Priest, Discipline, 230}, {230, Healer, Shaman, RestoShaman, 220}};
    auto const plan = PlanGuildAdd(10, 3, {{Damage, Rogue, Combat}, {Healer, Paladin, HolyPaladin},
        {Tank, Warrior, ProtWarrior}}, bench);
    CHECK(plan.open == GuildAddCounts({1, 2, 4}));
    CHECK_EQ(List(Guids(plan)), "101 210 230");
    CHECK_EQ(plan.covered, Bits({B::PhysicalDamageTaken, B::ArmorReduction, B::Kings}));

    // A raid healer of his: the Holy paladin and Discipline slots are what is left.
    CHECK_EQ(List(Guids(PlanGuildAdd(10, 2, {{Damage, Rogue, Combat}, {Healer, Druid, RestoDruid}}, bench),
        Healer)), "200 210");
    // Two Holy paladins of his: the second has no slot of its kind and takes the last one.
    CHECK_EQ(List(Guids(PlanGuildAdd(10, 3, {{Damage, Rogue, Combat}, {Healer, Paladin, HolyPaladin},
        {Healer, Paladin, HolyPaladin}}, bench), Healer)), "210");
    // A 5-man whose healer slot he filled himself takes no healer.
    CHECK(Guids(PlanGuildAdd(5, 1, {{Healer, Druid, RestoDruid}}, bench), Healer).empty());
}

LEEROY_TEST(Guild_AddMissingLineListsExactlyWhatIsMissing)
{
    GuildAddBuffs const every = (GuildAddBuffs(1) << kGuildAddBuffs) - 1;
    CHECK(GuildAddMissingLine(every).empty());
    // Only the important ones are named: the rest missing is no line.
    CHECK(GuildAddMissingLine(every & ~Bits({B::Kings, B::BattleRes, B::ArmorReduction, B::SpellPower})).empty());
    CHECK_EQ(GuildAddMissingLine(every & ~Bits({B::Bloodlust, B::SpellHit, B::Kings})),
        "Still missing: Bloodlust/Heroism and 3% spell hit");
    CHECK_EQ(GuildAddMissingLine(every & ~Bits({B::Replenishment})), "Still missing: Replenishment");
    // All of them still fits one chat line.
    auto const none = GuildAddMissingLine(0);
    CHECK_EQ(none, "Still missing: Bloodlust/Heroism, Replenishment, 20% melee haste, 5% spell crit, 10% attack "
        "power, 3% damage, 5% physical crit, 13% spell damage taken, 30% bleed damage, 4% physical damage taken, 3% "
        "spell hit and 3% crit taken");
    CHECK(none.size() <= Leeroy::kMaxChatBytes);

    // After the 25-man fill: nobody brings Windfury or Icy Talons, Leader of the Pack or Rampage, Mangle or Trauma.
    auto const plan = PlanGuildAdd(25, 1, {{Damage, Rogue, Combat}}, RaidPool());
    CHECK_EQ(GuildAddMissingLine(After(plan)),
        "Still missing: 20% melee haste, 5% physical crit and 30% bleed damage");
}

LEEROY_TEST(Guild_AddIsDeterministicForAFixedPool)
{
    auto pool = RaidPool();
    auto const healers = Healers();
    pool.insert(pool.end(), healers.begin() + 3, healers.end());
    std::vector<GuildAddMember> const group{{Damage, Rogue, Combat}, {Tank, Warrior, ProtWarrior}};
    auto const plan = PlanGuildAdd(25, 2, group, pool);
    CHECK_EQ(plan.picks.size(), std::size_t(2 + 6 + 8));
    for (std::size_t shift = 1; shift < pool.size(); shift += 5)
    {
        auto rotated = pool;
        std::rotate(rotated.begin(), rotated.begin() + static_cast<std::ptrdiff_t>(shift), rotated.end());
        CHECK_EQ(List(Guids(PlanGuildAdd(25, 2, group, rotated))), List(Guids(plan)));
    }
    std::vector<GuildAddCandidate> const reversed(pool.rbegin(), pool.rend());
    CHECK_EQ(List(Guids(PlanGuildAdd(25, 2, group, reversed))), List(Guids(plan)));
}
