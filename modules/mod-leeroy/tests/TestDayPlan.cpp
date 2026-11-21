// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyDayPlan.h"
#include "TestHarness.h"
#include "LeeroyProtocol.h"
#include <fstream>
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

LEEROY_TEST(DayPermitOnlyWaivesItsOriginalMemberStepAndIncarnation)
{
    DayPermit p;
    p.epoch = 3;
    BodyIdentity original;
    original.account = 7;
    p.members[101] = {original, 9};
    p.members[102] = {original, 9};
    p.steps["moving"] = {"realm_move", {101}};
    CHECK(p.Owns(101, "moving", original, 9, 3));
    CHECK(!p.Owns(102, "moving", original, 9, 3));
    CHECK(!p.Owns(101, "other", original, 9, 3));
    CHECK(!p.Owns(101, "moving", original, 10, 3));
    CHECK(!p.Owns(101, "moving", original, 9, 4));
    auto replacement = original;
    replacement.account = 8;
    CHECK(!p.Owns(101, "moving", replacement, 9, 3));
    p.reason = "guild_add";
    CHECK(!p.Released());
    p.steps["moving"].released = true;
    CHECK(!p.Owns(101, "moving", original, 9, 3));
    CHECK(p.Released());
}

LEEROY_TEST(DayClosedReportMatchesMindFixture)
{
    DayPermit p;
    p.origin = "begin";
    p.token.assign(32, 'a');
    p.guild = 50;
    p.human = 1;
    p.serial = 3;
    p.members[101] = {};
    p.reason = "string";
    auto actual = nlohmann::json::parse(Leeroy::EncodeEvent(0, DayStateMessage(p, 100)));
    std::ifstream in(LEEROY_TEST_FIXTURES "/day_protocol.jsonl");
    bool seen = false;
    for (std::string line; std::getline(in, line);)
    {
        auto row = nlohmann::json::parse(line);
        if (row.at("name") != "day_state")
            continue;
        CHECK(actual == row.at("msg"));
        seen = true;
    }
    CHECK(seen);
}


LEEROY_TEST(DayAddWaitsForEveryExactCohortReleaseAndExpiresWithoutReplaying)
{
    DayPermit first, second;
    first.reason = second.reason = "guild_add";
    auto& step = first.steps["move"];
    step.map = 1;
    CHECK(!step.InTransitTo(1, 0, 0, 0));
    step.destination = std::array{1.f, 2.f, 3.f};
    CHECK(step.InTransitTo(1, 1, 2, 3));
    CHECK(!step.InTransitTo(0, 1, 2, 3));
    CHECK(!step.InTransitTo(1, 1, 2, 4));
    std::vector<DayPermit const*> selected{&first, &second};
    CHECK(GuildAddDayProgress(selected, 1, 2) == DayHandoff::Waiting);
    first.unresolved = true;
    CHECK(GuildAddDayProgress(selected, 1, 2) == DayHandoff::Unresolved);
    step.released = true;
    CHECK(GuildAddDayProgress(selected, 1, 2) == DayHandoff::Ready);
    CHECK(GuildAddDayProgress(selected, 2, 2) == DayHandoff::Unresolved);
    selected.push_back(nullptr);
    CHECK(GuildAddDayProgress(selected, 1, 2) == DayHandoff::Unresolved);
}
