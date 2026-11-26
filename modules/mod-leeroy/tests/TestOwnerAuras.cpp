// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include <cstdint>
#include <functional>
#include <map>
#include <set>
#include <string>
#include <string_view>
#include <utility>
#include <vector>
// The worldserver includes windows.h before this header; its macros must not break it.
#define near
#define far
#define small char
#define IN
#define OUT
#define OPTIONAL
#define ERROR 0
#define DELETE (0x00010000L)
#define interface struct
#include "LeeroyOwnerAuras.h"
#undef near
#undef far
#undef small
#undef IN
#undef OUT
#undef OPTIONAL
#undef ERROR
#undef DELETE
#undef interface
#include "TestHarness.h"

using namespace Leeroy::Guild;

namespace
{
OwnerAuraState Free()
{
    OwnerAuraState state;
    state.online = true;
    state.alive = true;
    return state;
}

std::vector<std::uint32_t> Ids(std::initializer_list<std::uint32_t> ids)
{
    return ids;
}

OwnerPetState Summoned()
{
    OwnerPetState pet;
    pet.present = true;
    pet.his = true;
    pet.alive = true;
    return pet;
}

std::vector<std::uint64_t> Units(std::initializer_list<std::uint64_t> units)
{
    return units;
}
}

LEEROY_TEST(OwnerAuras_ParsesCommaListsSkippingRepeatsAndJunk)
{
    auto list = ParseOwnerAuras("6560, 1234 ,6560,,  99");
    CHECK(list.spells == Ids({6560, 1234, 99}));
    CHECK(list.rejected.empty());

    list = ParseOwnerAuras("");
    CHECK(list.spells.empty());
    CHECK(list.rejected.empty());
    CHECK(ParseOwnerAuras("  , ,").spells.empty());

    list = ParseOwnerAuras("6560,abc,0,-5,12x,4294967295,4294967296,99999999999");
    CHECK(list.spells == Ids({6560, 4294967295u}));
    CHECK(list.rejected == (std::vector<std::string>{"abc", "0", "-5", "12x", "4294967296", "99999999999"}));
}

LEEROY_TEST(OwnerAuras_CheckKeepsKnownAurasOnly)
{
    auto const checked = CheckOwnerAuras(Ids({6560, 1, 2}), [](std::uint32_t spell) {
        return spell == 6560 ? OwnerAuraSpell::Ok : spell == 1 ? OwnerAuraSpell::Unknown : OwnerAuraSpell::NotAnAura;
    });
    CHECK(checked.spells == Ids({6560}));
    CHECK_EQ(checked.invalid.size(), 2u);
    CHECK_EQ(checked.invalid[0].first, 1u);
    CHECK_EQ(std::string(OwnerAuraSpellName(checked.invalid[0].second)), std::string("unknown_spell"));
    CHECK_EQ(std::string(OwnerAuraSpellName(checked.invalid[1].second)), std::string("not_an_aura"));
}

LEEROY_TEST(OwnerAuras_AppliesOnlyWhatHeLacksWhileFree)
{
    std::set<std::uint32_t> has{1234};
    auto const holds = [&](std::uint32_t spell) { return has.contains(spell); };
    CHECK(OwnerAurasToApply(Ids({6560, 1234, 99}), Free(), holds) == Ids({6560, 99}));
    has = {6560, 1234, 99};
    CHECK(OwnerAurasToApply(Ids({6560, 1234, 99}), Free(), holds).empty());
    CHECK(OwnerAurasToApply({}, Free(), holds).empty());
}

LEEROY_TEST(OwnerAuras_EveryBlockStopsThePass)
{
    auto const none = [](std::uint32_t) { return false; };
    CHECK_EQ(std::string(OwnerAuraBlock(Free())), std::string());

    std::vector<std::pair<std::function<void(OwnerAuraState&)>, std::string>> const cases{
        {[](OwnerAuraState& s) { s.online = false; }, "offline"},
        {[](OwnerAuraState& s) { s.bot = true; }, "bot"},
        {[](OwnerAuraState& s) { s.alive = false; }, "dead"},
        {[](OwnerAuraState& s) { s.alive = false; s.ghost = true; }, "ghost"},
        {[](OwnerAuraState& s) { s.teleporting = true; }, "teleporting"},
        {[](OwnerAuraState& s) { s.taxi = true; }, "taxi"},
        {[](OwnerAuraState& s) { s.vehicle = true; }, "vehicle"},
        {[](OwnerAuraState& s) { s.battleground = true; }, "battleground"},
    };
    for (auto const& [change, code] : cases)
    {
        OwnerAuraState state = Free();
        change(state);
        CHECK_EQ(std::string(OwnerAuraBlock(state)), code);
        CHECK(OwnerAurasToApply(Ids({6560}), state, none).empty());
    }
    // An offline owner reads offline whatever else the snapshot says.
    OwnerAuraState gone;
    gone.taxi = true;
    gone.bot = true;
    CHECK_EQ(std::string(OwnerAuraBlock(gone)), std::string("offline"));
}

LEEROY_TEST(OwnerAuras_ReasonsKeepTheLastRemovalUntilForgotten)
{
    OwnerAuraReasons reasons;
    CHECK(reasons.Peek(6560) == OwnerAuraWhy::Unknown);
    reasons.Note(6560, OwnerAuraWhy::Dispelled);
    reasons.Note(99, OwnerAuraWhy::Expired);
    CHECK(reasons.Peek(6560) == OwnerAuraWhy::Dispelled);
    reasons.Note(6560, OwnerAuraWhy::Death);
    CHECK(reasons.Peek(6560) == OwnerAuraWhy::Death);
    CHECK(reasons.Peek(6560) == OwnerAuraWhy::Death);   // a refused try keeps it
    reasons.Forget(6560);
    CHECK(reasons.Peek(6560) == OwnerAuraWhy::Unknown);
    CHECK(reasons.Peek(99) == OwnerAuraWhy::Expired);
    reasons.Clear();
    CHECK(reasons.Peek(99) == OwnerAuraWhy::Unknown);

    CHECK_EQ(std::string(OwnerAuraWhyName(OwnerAuraWhy::Unknown)), std::string("unknown"));
    CHECK_EQ(std::string(OwnerAuraWhyName(OwnerAuraWhy::Dispelled)), std::string("dispelled"));
    CHECK_EQ(std::string(OwnerAuraWhyName(OwnerAuraWhy::Death)), std::string("death"));
    CHECK_EQ(std::string(OwnerAuraWhyName(OwnerAuraWhy::Expired)), std::string("expired"));
    CHECK_EQ(std::string(OwnerAuraWhyName(OwnerAuraWhy::Cancelled)), std::string("cancelled"));
}

LEEROY_TEST(OwnerAuras_LogsOnceAMinutePerSpellAndOutcome)
{
    OwnerAuraLogGate gate;
    CHECK(gate.Allow(6560, false, 1000));
    CHECK(!gate.Allow(6560, false, 1000 + kOwnerAuraLogMs - 1));
    CHECK(gate.Allow(6560, true, 2000));          // a refusal has its own minute
    CHECK(gate.Allow(99, false, 2000));           // and so does another spell
    CHECK(gate.Allow(6560, false, 1000 + kOwnerAuraLogMs));
    CHECK(!gate.Allow(6560, false, 1000 + kOwnerAuraLogMs + 5));
    CHECK(gate.Allow(6560, false, 10));           // the clock went back
}

LEEROY_TEST(OwnerAuras_PassRunsEveryTwoSecondsOrSoonAfterAWake)
{
    OwnerAuraPass pass;
    CHECK(pass.Due(0, false));                    // the first tick
    CHECK(!pass.Due(1000, false));
    CHECK(!pass.Due(999, false));
    CHECK(pass.Due(1, false));
    CHECK(!pass.Due(100, true));                  // woken inside the gap: waits for it
    CHECK(!pass.Due(399, false));
    CHECK(pass.Due(1, false));                    // the wake was kept
    CHECK(!pass.Due(1999, false));                // and spent
    CHECK(pass.Due(1, false));
    CHECK(pass.Due(kOwnerAuraWakeGapMs, true));
    CHECK(pass.Due(0xFFFFFFFFu, false));          // a stalled tick saturates, no overflow
    CHECK(!pass.Due(0, false));
}

LEEROY_TEST(OwnerPetAuras_TheSuggestedListParsesAndAnInlineCommentIsRejected)
{
    auto list = ParseOwnerAuras("6560, 51040, 31750");
    CHECK(list.spells == Ids({6560, 51040, 31750}));
    CHECK(list.rejected.empty());

    // Config values take no inline comments: the comment's words are entries, each rejected.
    list = ParseOwnerAuras("6560, 51040 # haste");
    CHECK(list.spells == Ids({6560, 51040}));
    CHECK(list.rejected == (std::vector<std::string>{"#", "haste"}));
}

LEEROY_TEST(OwnerPetAuras_AppliesOnlyWhatHisLivingPetLacks)
{
    std::set<std::uint32_t> has{51040};
    auto const holds = [&](std::uint32_t spell) { return has.contains(spell); };
    CHECK_EQ(std::string(OwnerPetAuraBlock(Free(), Summoned())), std::string());
    CHECK(OwnerPetAurasToApply(Ids({6560, 51040, 31750}), Free(), Summoned(), holds) == Ids({6560, 31750}));
    has = {6560, 51040, 31750};
    CHECK(OwnerPetAurasToApply(Ids({6560, 51040, 31750}), Free(), Summoned(), holds).empty());
    CHECK(OwnerPetAurasToApply({}, Free(), Summoned(), holds).empty());

    // The generic form takes any block.
    CHECK(AurasToApply(Ids({6560}), "", [](std::uint32_t) { return false; }) == Ids({6560}));
    CHECK(AurasToApply(Ids({6560}), "no_pet", [](std::uint32_t) { return false; }).empty());
}

LEEROY_TEST(OwnerPetAuras_HisBlocksComeFirstThenThePets)
{
    auto const none = [](std::uint32_t) { return false; };
    // Whatever stops his own auras stops his pet's: offline, a bot, dead, a ghost, a teleport, a taxi,
    // a vehicle, a battleground.
    std::vector<std::pair<std::function<void(OwnerAuraState&)>, std::string>> const owner{
        {[](OwnerAuraState& s) { s.online = false; }, "offline"},
        {[](OwnerAuraState& s) { s.bot = true; }, "bot"},
        {[](OwnerAuraState& s) { s.alive = false; }, "dead"},
        {[](OwnerAuraState& s) { s.alive = false; s.ghost = true; }, "ghost"},
        {[](OwnerAuraState& s) { s.teleporting = true; }, "teleporting"},
        {[](OwnerAuraState& s) { s.taxi = true; }, "taxi"},
        {[](OwnerAuraState& s) { s.vehicle = true; }, "vehicle"},
        {[](OwnerAuraState& s) { s.battleground = true; }, "battleground"},
    };
    for (auto const& [change, code] : owner)
    {
        OwnerAuraState state = Free();
        change(state);
        CHECK_EQ(std::string(OwnerPetAuraBlock(state, Summoned())), code);
        CHECK(OwnerPetAurasToApply(Ids({51040}), state, Summoned(), none).empty());
    }

    std::vector<std::pair<std::function<void(OwnerPetState&)>, std::string>> const pet{
        {[](OwnerPetState& p) { p.present = false; }, "no_pet"},
        {[](OwnerPetState& p) { p.his = false; }, "not_his"},
        {[](OwnerPetState& p) { p.alive = false; }, "pet_dead"},
        {[](OwnerPetState& p) { p.charmed = true; }, "charmed"},
    };
    for (auto const& [change, code] : pet)
    {
        OwnerPetState state = Summoned();
        change(state);
        CHECK_EQ(std::string(OwnerPetAuraBlock(Free(), state)), code);
        CHECK(OwnerPetAurasToApply(Ids({51040}), Free(), state, none).empty());
    }

    // Someone else's pet never reads as his, however alive it is; a missing one reads missing first.
    OwnerPetState theirs = Summoned();
    theirs.his = false;
    theirs.charmed = true;
    CHECK_EQ(std::string(OwnerPetAuraBlock(Free(), theirs)), std::string("not_his"));
    CHECK_EQ(std::string(OwnerPetAuraBlock(Free(), OwnerPetState{})), std::string("no_pet"));
    // An offline owner reads offline even with a pet in the snapshot.
    CHECK_EQ(std::string(OwnerPetAuraBlock(OwnerAuraState{}, Summoned())), std::string("offline"));
}

LEEROY_TEST(OwnerPetAuras_ThePetComesFirstThenEachGuardianOnce)
{
    CHECK(OwnerPetTargets(7, Units({9, 7, 8, 9, 0})) == Units({7, 9, 8}));
    CHECK(OwnerPetTargets(0, Units({9, 8})) == Units({9, 8}));
    CHECK(OwnerPetTargets(7, {}) == Units({7}));
    CHECK(OwnerPetTargets(0, Units({0})).empty());
}
