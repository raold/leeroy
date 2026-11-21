// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyDayPlan.h"
#include "TestHarness.h"
using namespace Leeroy::Realm;

LEEROY_TEST(DayPermitCohortRequiresBothMovesAndSealsAdmittedSteps)
{
    DayPermit p;
    p.activity = "duel_practice";
    p.members[1] = {};
    p.members[2] = {};
    CHECK(p.Allows("realm_move", {1}));
    CHECK(!p.Allows("realm_move", {3}));
    p.steps["first"] = {"realm_move", {1}, true, true};
    CHECK(!p.Allows("realm_move", {1}));
    CHECK(!p.Allows("duel", {1, 2}));
    p.steps["second"] = {"realm_move", {2}, true, true};
    CHECK(p.Allows("duel", {1, 2}));
    CHECK(!p.Allows("duel", {1, 1}));
    p.steps["duel"] = {"duel", {1, 2}};
    p.reason = "guild_add";
    CHECK(!p.Released());
    CHECK(!p.Allows("duel", {1, 2}));
    p.steps.at("duel").released = true;
    CHECK(p.Released());
}

LEEROY_TEST(DayPermitFishingRequiresAnArrivalAndOneTypedStart)
{
    DayPermit p;
    p.activity = "fishing";
    p.members[4] = {};
    CHECK(!p.Allows("guild_fishing", {4}));
    p.steps["arrival"] = {"realm_move", {4}, true, true};
    CHECK(p.Allows("guild_fishing", {4}));
    CHECK(!p.Allows("rpg_status", {4}));
    CHECK(DayPlaceMatches("fishing", "fish"));
    CHECK(!DayPlaceMatches("fishing", "inn"));
    p.steps["cast"] = {"guild_fishing", {4}};
    CHECK(!p.Allows("guild_fishing", {4}));
    p.unresolved = true;
    p.reason = "action_unproved";
    CHECK(!p.Released());
}
