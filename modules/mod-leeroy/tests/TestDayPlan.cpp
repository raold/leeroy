// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyDayPlan.h"
#include "LeeroyOwnerFight.h"
#include "LeeroyGuildAddPolicy.h"
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


LEEROY_TEST(DayDispatchReadsTheGroupFightAgainAfterAdmission)
{
    struct Body;
    struct Reference
    {
        Body* source;
        Body* GetSource() const { return source; }
        Reference* next() const { return nullptr; }
    };
    struct Party
    {
        Reference ref;
        Reference* GetFirstMember() { return &ref; }
    };
    struct Body
    {
        bool combat;
        int map;
        Party* party;
        bool IsInCombat() const { return combat; }
        int GetMap() const { return map; }
        Party* GetGroup() const { return party; }
    };
    Body friendBody{false, 1, nullptr};
    Party party{{&friendBody}};
    Body owner{false, 1, &party};
    CHECK(!Leeroy::OwnerGroupInCombat(&owner));
    friendBody.combat = true;
    CHECK(Leeroy::OwnerGroupInCombat(&owner));
    friendBody.map = 2;
    CHECK(!Leeroy::OwnerGroupInCombat(&owner));
}

LEEROY_TEST(DayFishingCleaningSuppliesTheTankToThePickerWithoutLosingAnAcceptedAdd)
{
    using namespace Leeroy::Guild;
    ContinuationLedger ledger("00000000000000000000000000000001");
    std::string error;
    auto owner = ledger.Prepare("fish", PvpKind::Fishing, {101}, 3, 8, 1, 1, error, ActivityDomain::Guild);
    CHECK(owner);
    CHECK(ledger.Advance(owner->token, owner->revision, PvpStage::Cleaning, "finish"));
    owner = ledger.Get(owner->token);
    CHECK(DayFishingCleaning(*owner, 3));
    CHECK(!DayFishingCleaning(*owner, 4));
    auto const appFence = ledger.Validate(owner->token, ContinuationMode::Application, true, 3, 8, 1);
    CHECK(!appFence.empty());
    GuildAddBody body;
    body.online = body.cast = true;
    body.fence = ledger.Validate(owner->token, ContinuationMode::Cleanup, true, 3, 8, 1);
    CHECK(GuildAddSkipReason(body).empty());
    std::vector<GuildAddCandidate> eligible{{101, GuildAddRole::Tank, 1, 2, 200}};
    auto ten = PlanGuildAdd(10, 5, {}, eligible);
    CHECK(!ten.picks.empty() && ten.picks.front().guid == 101);
    std::optional<int> waiter;
    ReplaceDayAddWaiter(waiter, ten, std::optional{10});
    CHECK(waiter == 10);
    auto five = PlanGuildAdd(5, 5, {}, eligible);
    ReplaceDayAddWaiter(waiter, five, std::optional<int>{});
    CHECK(waiter == 10);
    body.fence = "busy:training_owner";
    CHECK(!GuildAddSkipReason(body).empty());
    body.fence.clear();
    body.dead = true;
    CHECK(!GuildAddSkipReason(body).empty());
}

LEEROY_TEST(DayPreparedOwnerBindsOriginalTypeArgumentsSerialAndDeadline)
{
    nlohmann::json original{{"type","clear_lockout"},{"id","request"},{"guids",{101,102}},{"map_id",533}};
    CHECK(DayRequestMatches(original, original, 5, 5, 30, 29, false));
    CHECK(!DayRequestMatches(original, original, 5, 6, 30, 29, false));
    CHECK(!DayRequestMatches(original, original, 5, 5, 30, 30, false));
    CHECK(!DayRequestMatches(original, original, 5, 5, 30, 29, true));
    auto altered = original;
    altered["guids"] = {101};
    CHECK(!DayRequestMatches(original, altered, 5, 5, 30, 29, false));
    DayPermit p;
    p.destination = {"north_pond",1,2,1.f,2.f,3.f,0.f,"fish"};
    CHECK(p.LocationMatches(p.destination));
    auto replacement = p.destination;
    replacement.map = 0;
    CHECK(!p.LocationMatches(replacement));
    replacement = p.destination;
    replacement.x += 4.f;
    CHECK(!p.LocationMatches(replacement));
}

LEEROY_TEST(DayOfflineDriverTicketPreservesTheOriginalManifestConnectionAndDeadline)
{
    nlohmann::json binding{{"request_id","original"},{"expires_ms",200},{"origins",{"begin"}}};
    std::set<std::string> tokens{"permit"};
    CHECK(DayDriverTicketMatches(binding, binding, tokens, tokens));
    CHECK(!DayDriverTicketMatches(nullptr, binding, tokens, tokens));
    CHECK(!DayDriverTicketMatches(binding, binding, tokens, {"new-permit"}));
    for (auto const& patch : {nlohmann::json{{"request_id","replacement"}}, nlohmann::json{{"expires_ms",201}},
                              nlohmann::json{{"origins",{"other"}}}})
    {
        auto changed = binding;
        changed.update(patch);
        CHECK(!DayDriverTicketMatches(binding, changed, tokens, tokens));
    }
    nlohmann::json raid{{"id","original"},{"members",{101,102}},{"map_id",533}};
    CHECK(!DayRequestMatches(raid, raid, 1, 2, 200, 190, false));
    CHECK(!DayRequestMatches(raid, raid, 1, 1, 200, 200, false));
    CHECK(!DayRequestMatches(raid, raid, 1, 1, 200, 190, true));
    auto changed = raid;
    changed["members"] = {101,103};
    CHECK(!DayRequestMatches(raid, changed, 1, 1, 200, 190, false));
}
