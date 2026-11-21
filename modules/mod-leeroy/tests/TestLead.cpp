// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyLead.h"
#include "LeeroyProtocol.h"
#include "TestHarness.h"

#include <string>
#include <nlohmann/json.hpp>

using namespace Leeroy::Guild;
using json = nlohmann::json;

namespace
{
// Her in his party, five yards from him in the open world, both free; he leads.
LeadFacts Ready()
{
    LeadFacts f;
    f.enabled = true;
    f.humanOnline = f.humanAlive = true;
    f.cast = f.online = true;
    f.hisGroup = f.leader = true;
    f.sameMap = true;
    f.distance = 5.0f;
    f.spotValid = f.spotSameMap = true;
    f.spotDistance = 900.0f;
    return f;
}

LeadState Leading(LeadActivity activity = LeadActivity::Quest)
{
    LeadState s;
    s.activity = activity;
    s.started = 1000;
    s.until = 1000 + kLeadMs;
    return s;
}

// A tick where everything holds: her, him beside her, his group, her master and strategies.
LeadTickFacts Fine(std::uint64_t now = 2000)
{
    LeadTickFacts f;
    f.now = now;
    f.on = f.leaderHere = f.humanOnline = true;
    f.grouped = f.selfMastered = f.activityOn = true;
    f.sameMap = true;
    f.distance = 8.0f;
    return f;
}

std::string Why(LeadState const& s, LeadTickFacts const& f)
{
    LeadDecision const d = DecideLead(s, f);
    return d.step == LeadStep::End ? std::string(d.why) : std::string();
}
}

LEEROY_TEST(Lead_ActivitiesRoundTripByName)
{
    for (auto activity : {LeadActivity::Quest, LeadActivity::Grind, LeadActivity::Travel})
        CHECK(ParseLeadActivity(LeadActivityName(activity)) == activity);
    CHECK(!ParseLeadActivity("fish").has_value());
    CHECK(!ParseLeadActivity("Quest").has_value());
}

LEEROY_TEST(Lead_ParsesStartsAndEnds)
{
    std::string error;
    auto start = ParseLeadDo(json{{"action", "start"}, {"guid", 101u}, {"activity", "grind"}}, error);
    CHECK(start.has_value() && error.empty());
    CHECK(start->action == LeadAction::Start && start->guid == 101 && start->activity == LeadActivity::Grind);
    auto travel = ParseLeadDo(json{{"action", "start"}, {"guid", 101u}, {"activity", "travel"}, {"spot", "org_inn"}},
        error);
    CHECK(travel.has_value() && travel->spot == "org_inn");
    auto end = ParseLeadDo(json{{"action", "end"}}, error);
    CHECK(end.has_value() && end->action == LeadAction::End && end->guid == 0);
    auto named = ParseLeadDo(json{{"action", "end"}, {"guid", 7u}, {"activity", ""}, {"spot", ""}}, error);
    CHECK(named.has_value() && named->guid == 7);
}

LEEROY_TEST(Lead_RefusesMalformedFieldsByName)
{
    std::string error;
    auto const bad = [&](json const& msg, std::string const& field)
    {
        CHECK(!ParseLeadDo(msg, error).has_value());
        CHECK_EQ(error, std::string(Leeroy::Err::BadField) + field);
    };
    bad(json{{"guid", 101u}, {"activity", "quest"}}, "action");
    bad(json{{"action", "go"}, {"guid", 101u}, {"activity", "quest"}}, "action");
    bad(json{{"action", "start"}, {"activity", "quest"}}, "guid");
    bad(json{{"action", "start"}, {"guid", 0u}, {"activity", "quest"}}, "guid");
    bad(json{{"action", "start"}, {"guid", -3}, {"activity", "quest"}}, "guid");
    bad(json{{"action", "start"}, {"guid", 101u}}, "activity");
    bad(json{{"action", "start"}, {"guid", 101u}, {"activity", "fish"}}, "activity");
    // travel names one of the meeting spots, and nothing else names a spot
    bad(json{{"action", "start"}, {"guid", 101u}, {"activity", "travel"}}, "spot");
    bad(json{{"action", "start"}, {"guid", 101u}, {"activity", "travel"}, {"spot", "stormwind"}}, "spot");
    bad(json{{"action", "start"}, {"guid", 101u}, {"activity", "quest"}, {"spot", "org_inn"}}, "spot");
    // an end says nothing about what or where
    bad(json{{"action", "end"}, {"activity", "quest"}}, "activity");
    bad(json{{"action", "end"}, {"spot", "org_inn"}}, "spot");
    bad(json{{"action", "end"}, {"guid", "x"}}, "guid");
}

LEEROY_TEST(Lead_StartsOnlyFromHisGroupInTheOpenWorld)
{
    CHECK_EQ(LeadRefusal(LeadActivity::Quest, Ready()), std::string());
    CHECK_EQ(LeadRefusal(LeadActivity::Travel, Ready()), std::string());
    auto const refused = [](LeadActivity activity, auto change, std::string const& code)
    {
        LeadFacts f = Ready();
        change(f);
        CHECK_EQ(LeadRefusal(activity, f), code);
    };
    using A = LeadActivity;
    refused(A::Quest, [](LeadFacts& f) { f.enabled = false; }, "off");
    refused(A::Quest, [](LeadFacts& f) { f.humanOnline = false; }, "human_offline");
    refused(A::Quest, [](LeadFacts& f) { f.cast = false; }, "not_cast");
    refused(A::Quest, [](LeadFacts& f) { f.online = false; }, "not_online");
    refused(A::Quest, [](LeadFacts& f) { f.fence = "busy:training_owner"; }, "busy:training_owner");
    refused(A::Quest, [](LeadFacts& f) { f.dead = true; }, "dead");
    refused(A::Quest, [](LeadFacts& f) { f.humanCombat = true; }, "combat");
    refused(A::Grind, [](LeadFacts& f) { f.combat = true; }, "combat");
    refused(A::Quest, [](LeadFacts& f) { f.humanInstance = true; }, "in_instance");
    refused(A::Quest, [](LeadFacts& f) { f.instance = true; }, "in_instance");
    refused(A::Quest, [](LeadFacts& f) { f.battleground = true; }, "in_bg");
    refused(A::Quest, [](LeadFacts& f) { f.humanBattleground = true; }, "in_bg");
    refused(A::Quest, [](LeadFacts& f) { f.queued = true; }, "queued");
    refused(A::Quest, [](LeadFacts& f) { f.humanTravelling = true; }, "travelling");
    refused(A::Quest, [](LeadFacts& f) { f.humanAlive = false; }, "human_dead");
    refused(A::Quest, [](LeadFacts& f) { f.hisGroup = false; }, "not_in_group");
    refused(A::Quest, [](LeadFacts& f) { f.groupSpecial = true; }, "lfg_group");
    refused(A::Quest, [](LeadFacts& f) { f.leader = false; }, "not_leader");
    refused(A::Quest, [](LeadFacts& f) { f.sameMap = false; }, "too_far");
    refused(A::Quest, [](LeadFacts& f) { f.distance = kLeadStartYards + 1.0f; }, "too_far");
    refused(A::Grind, [](LeadFacts& f) { f.supported = false; }, "unsupported");
    // travel: a spot on her map that she does not already stand at
    refused(A::Travel, [](LeadFacts& f) { f.spotValid = false; }, "bad_spot");
    refused(A::Travel, [](LeadFacts& f) { f.spotSameMap = false; }, "other_map");
    refused(A::Travel, [](LeadFacts& f) { f.spotDistance = kLeadArriveYards; }, "here");
    // only travel asks about the spot
    refused(A::Quest, [](LeadFacts& f) { f.spotValid = f.spotSameMap = false; }, "");
}

LEEROY_TEST(Lead_KeepsLeadingWhileEverythingHolds)
{
    LeadState const s = Leading();
    LeadDecision const d = DecideLead(s, Fine());
    CHECK(d.step == LeadStep::Keep);
}

LEEROY_TEST(Lead_EndsForEachReasonInOrder)
{
    LeadState const s = Leading();
    auto const ends = [&](auto change, std::string const& why)
    {
        LeadTickFacts f = Fine();
        change(f);
        CHECK_EQ(Why(s, f), why);
    };
    ends([](LeadTickFacts& f) { f.leaderHere = false; f.on = false; }, "gone");
    ends([](LeadTickFacts& f) { f.on = false; f.humanOnline = false; }, "off");
    ends([](LeadTickFacts& f) { f.humanOnline = false; f.dead = true; }, "logout");
    ends([](LeadTickFacts& f) { f.dead = true; f.instance = true; }, "dead");
    ends([](LeadTickFacts& f) { f.instance = true; f.grouped = false; }, "instance");
    ends([](LeadTickFacts& f) { f.battleground = true; }, "pvp");
    ends([](LeadTickFacts& f) { f.grouped = false; f.selfMastered = false; }, "group");
    ends([](LeadTickFacts& f) { f.selfMastered = false; f.activityOn = false; }, "taken");
    ends([](LeadTickFacts& f) { f.activityOn = false; }, "reset");
    ends([](LeadTickFacts& f) { f.fenced = true; f.teleported = true; }, "fenced");
    ends([](LeadTickFacts& f) { f.teleported = true; }, "teleported");
    ends([&](LeadTickFacts& f) { f.now = s.until; }, "timeout");
}

LEEROY_TEST(Lead_WaitsForHimAndGoesOnWhenHeIsBack)
{
    LeadState s = Leading();
    LeadTickFacts f = Fine(5000);
    f.distance = kLeadWaitYards + 1.0f;
    CHECK(DecideLead(s, f).step == LeadStep::Pause);
    ApplyLead(s, LeadStep::Pause, f.now);
    CHECK(s.paused && s.behindSince == 5000);
    // paused: her strategies are off by our hand, which is no reset
    f.activityOn = false;
    f.distance = kLeadResumeYards + 1.0f;   // between the two: still waiting
    CHECK(DecideLead(s, f).step == LeadStep::Keep);
    f.distance = kLeadResumeYards;
    CHECK(DecideLead(s, f).step == LeadStep::Resume);
    ApplyLead(s, LeadStep::Resume, 9000);
    CHECK(!s.paused && s.behindSince == 0 && s.progressAt == 9000);
    // on another map he is behind too
    LeadTickFacts other = Fine(10000);
    other.sameMap = false;
    CHECK(DecideLead(s, other).step == LeadStep::Pause);
}

LEEROY_TEST(Lead_EndsWhenHeStaysBehindTooLong)
{
    LeadState s = Leading();
    ApplyLead(s, LeadStep::Pause, 5000);
    LeadTickFacts f = Fine(5000 + kLeadBehindMs - 1);
    f.sameMap = false;
    f.activityOn = false;
    CHECK(DecideLead(s, f).step == LeadStep::Keep);
    f.now = 5000 + kLeadBehindMs;
    CHECK_EQ(Why(s, f), std::string("left"));
}

LEEROY_TEST(Lead_NeverStopsOrGoesOnInAFight)
{
    LeadState s = Leading();
    LeadTickFacts f = Fine();
    f.fighting = true;
    f.distance = 200.0f;
    CHECK(DecideLead(s, f).step == LeadStep::Keep);
    ApplyLead(s, LeadStep::Pause, 1500);
    f.distance = 2.0f;
    f.activityOn = false;
    CHECK(DecideLead(s, f).step == LeadStep::Keep);
    // an end still ends in a fight
    f.dead = true;
    CHECK_EQ(Why(s, f), std::string("dead"));
}

LEEROY_TEST(Lead_TravelArrivesOrFindsNoWay)
{
    LeadState s = Leading(LeadActivity::Travel);
    LeadTickFacts f = Fine(2000);
    f.spotDistance = 600.0f;
    NoteLead(s, f);
    CHECK(s.progressAt == 2000 && s.best == 600.0f);
    // a few yards are no progress; five more are
    f.now = 3000;
    f.spotDistance = 597.0f;
    NoteLead(s, f);
    CHECK(s.progressAt == 2000);
    f.now = 4000;
    f.spotDistance = 590.0f;
    NoteLead(s, f);
    CHECK(s.progressAt == 4000 && s.best == 590.0f);
    // no way there: kLeadStuckMs without coming closer
    f.now = 4000 + kLeadStuckMs;
    f.spotDistance = 591.0f;
    NoteLead(s, f);
    CHECK_EQ(Why(s, f), std::string("no_path"));
    // a fight restarts the clock
    LeadState fought = Leading(LeadActivity::Travel);
    fought.best = 590.0f;
    fought.progressAt = 4000;
    LeadTickFacts fight = Fine(4000 + kLeadStuckMs);
    fight.fighting = true;
    fight.spotDistance = 591.0f;
    NoteLead(fought, fight);
    CHECK(fought.progressAt == fight.now);
    CHECK(DecideLead(fought, fight).step == LeadStep::Keep);
    // there
    f.spotDistance = kLeadArriveYards;
    CHECK_EQ(Why(s, f), std::string("arrived"));
    // a spot on another map is never reached, and never counted
    LeadState elsewhere = Leading(LeadActivity::Travel);
    LeadTickFacts away = Fine(2000);
    away.spotDistance = -1.0f;
    NoteLead(elsewhere, away);
    CHECK(DecideLead(elsewhere, away).step == LeadStep::Keep);
}

LEEROY_TEST(Lead_QuestingNeverFliesOffOrGoesToPvp)
{
    CHECK(LeadSteer(LeadActivity::Quest, static_cast<int>(LeadRpg::TravelFlight)) == LeadRpg::WanderRandom);
    CHECK(LeadSteer(LeadActivity::Quest, static_cast<int>(LeadRpg::OutdoorPvp)) == LeadRpg::WanderRandom);
    for (auto keep : {LeadRpg::Idle, LeadRpg::GoGrind, LeadRpg::GoCamp, LeadRpg::WanderRandom, LeadRpg::WanderNpc,
             LeadRpg::DoQuest, LeadRpg::Rest, LeadRpg::DoGather})
        CHECK(!LeadSteer(LeadActivity::Quest, static_cast<int>(keep)).has_value());
}

LEEROY_TEST(Lead_GrindingKeepsToGrinding)
{
    for (auto keep : {LeadRpg::Idle, LeadRpg::GoGrind, LeadRpg::WanderRandom, LeadRpg::Rest})
        CHECK(!LeadSteer(LeadActivity::Grind, static_cast<int>(keep)).has_value());
    for (auto steer : {LeadRpg::GoCamp, LeadRpg::WanderNpc, LeadRpg::DoQuest, LeadRpg::TravelFlight,
             LeadRpg::OutdoorPvp, LeadRpg::DoGather})
        CHECK(LeadSteer(LeadActivity::Grind, static_cast<int>(steer)) == LeadRpg::WanderRandom);
}

LEEROY_TEST(Lead_TravelAlwaysWalksToTheCamp)
{
    CHECK(!LeadSteer(LeadActivity::Travel, static_cast<int>(LeadRpg::GoCamp)).has_value());
    for (auto steer : {LeadRpg::Idle, LeadRpg::WanderNpc, LeadRpg::DoQuest, LeadRpg::TravelFlight})
        CHECK(LeadSteer(LeadActivity::Travel, static_cast<int>(steer)) == LeadRpg::GoCamp);
}

LEEROY_TEST(Lead_HelloListsTheFeature)
{
    bool found = false;
    for (std::string_view feature : Leeroy::kFeatures)
        found = found || feature == "lead";
    CHECK(found);
}
