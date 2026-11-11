// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmCommands.h"
#include "LeeroyGuildCommands.h"
#include "TestHarness.h"
#include <algorithm>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <map>
#include <set>

using namespace Leeroy::Realm;

LEEROY_TEST(Realm_PlacesDistParses)
{
    auto path = std::filesystem::path(LEEROY_TEST_FIXTURES).parent_path().parent_path() / "conf" / "leeroy_places.csv.dist";
    std::ifstream stream(path);
    CHECK(stream.good());
    std::string csv{std::istreambuf_iterator<char>(stream), std::istreambuf_iterator<char>()};
    std::string error;
    auto places = ParsePlaces(csv, error);
    CHECK(places.has_value());
    if (!places) return;
    CHECK_EQ(places->size(), 20u);
    std::map<std::string, Place> byName;
    for (auto const& place : *places)
    {
        CHECK(byName.emplace(place.name, place).second);
        CHECK(std::find(kPlaceKinds.begin(), kPlaceKinds.end(), place.kind) != kPlaceKinds.end());
        if (place.kind == "home")
            CHECK(std::find(kAllianceCapitals.begin(), kAllianceCapitals.end(), place.zone) != kAllianceCapitals.end());
        if (place.kind == "city")
            CHECK(place.zone == 4395 || std::find(kHordeCapitals.begin(), kHordeCapitals.end(), place.zone) != kHordeCapitals.end());
    }
    std::map<std::string, std::string> const required{
        {"org_bank","city"}, {"org_ah","city"}, {"org_inn","inn"}, {"org_gate","duel"},
        {"org_bonfire","holiday"}, {"show_walk","show"}, {"org_arena_master","arenamaster"},
        {"uc_bank","city"}, {"tb_bank","city"}, {"dal_fountain","fish"}, {"dal_inn","inn"},
        {"dal_bank","city"}, {"booty_bay_docks","fish"}, {"gurubashi_arena","arena"},
        {"sholazar_herbs","herb"}, {"icecrown_ore","ore"}, {"storm_peaks_tlpd","camp"},
        {"brewfest_durotar","holiday"}, {"stormwind_trade","home"}, {"ironforge_bank","home"}};
    for (auto const& [name, kind] : required)
    {
        CHECK(byName.contains(name));
        if (byName.contains(name)) CHECK_EQ(byName.at(name).kind, kind);
    }
    if (byName.contains("uc_bank")) CHECK_EQ(byName.at("uc_bank").zone, 1497u);
    if (byName.contains("org_arena_master")) CHECK_EQ(byName.at("org_arena_master").zone, 1637u);
    // Terrain is Durotar, but the gate is inside Orgrimmar's WMO area override.
    if (byName.contains("org_gate")) CHECK_EQ(byName.at("org_gate").zone, 1637u);
    CHECK(!byName.contains("org_bank_roof"));
}

LEEROY_TEST(Realm_BotControlsRemainNarrow)
{
    for (auto action : {"co", "nc"})
        for (auto param : {"+bg", "-bg", "+lfg", "-lfg"}) CHECK(Leeroy::Guild::BotDoParamAllowed(action, param));
    CHECK(Leeroy::Guild::BotDoParamAllowed("do", "go fishing"));
    for (auto param : {"-avoid aoe", "-survival", "+attack anything", "+go fishing", "-combat"})
        CHECK(!Leeroy::Guild::BotDoParamAllowed("co", param));
    for (auto action : {"leave", "teleport", "equip", "cast anything"})
        CHECK(!Leeroy::Guild::BotDoParamAllowed("do", action));
}
