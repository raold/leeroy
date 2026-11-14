// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmFailure.h"
#include "LeeroyStrings.h"
#include "TestHarness.h"

#include <nlohmann/json.hpp>

using namespace Leeroy::Guild;
using json = nlohmann::json;

namespace
{
// Him in a capital where duels are allowed, her five yards away in the same zone, nobody grouped.
StringFacts Free()
{
    StringFacts f;
    f.enabled = true;
    f.humanOnline = f.humanAlive = true;
    f.humanArea = kAreaCapital | kAreaAllowDuels;
    f.cast = f.online = true;
    f.area = kAreaAllowDuels;
    f.sameMap = f.sameZone = true;
    f.distance = 5.0f;
    f.emote = "wave";
    return f;
}

StringFacts InHisParty()
{
    StringFacts f = Free();
    f.hisGroup = true;
    f.groupMembers = 3;
    return f;
}

constexpr StringKind kAll[] = {StringKind::ComeToMe, StringKind::JoinGroup, StringKind::Follow, StringKind::DuelMe,
    StringKind::Emote, StringKind::Sit};

// The facts each kind is carried out from: follow needs her in his party, the rest are taken free.
StringFacts Ready(StringKind kind) { return kind == StringKind::Follow ? InHisParty() : Free(); }
}

LEEROY_TEST(Strings_KindsRoundTripByName)
{
    for (auto kind : kAll)
        CHECK(ParseStringKind(StringKindName(kind)) == kind);
    CHECK(!ParseStringKind("queue_arena").has_value());
    CHECK(!ParseStringKind("Come_To_Me").has_value());
}

LEEROY_TEST(Strings_ParseTakesGuidKindTargetAndOnlyAListedEmote)
{
    std::string error;
    auto cmd = ParseStringDo(json{{"guid", 102u}, {"kind", "come_to_me"}}, error);
    CHECK(cmd.has_value());
    CHECK_EQ(cmd->guid, 102u);
    CHECK(cmd->kind == StringKind::ComeToMe);
    CHECK_EQ(cmd->targetGuid, 0u);
    cmd = ParseStringDo(json{{"guid", 102u}, {"kind", "duel_me"}, {"target_guid", 1u}, {"emote", ""}}, error);
    CHECK(cmd.has_value() && cmd->targetGuid == 1u);
    cmd = ParseStringDo(json{{"guid", 103u}, {"kind", "emote"}, {"emote", "wave"}}, error);
    CHECK(cmd.has_value() && cmd->emote == "wave");

    CHECK(!ParseStringDo(json{{"guid", 0u}, {"kind", "sit"}}, error));
    CHECK_EQ(error, std::string("bad field: guid"));
    CHECK(!ParseStringDo(json{{"guid", 102u}, {"kind", "queue_arena"}}, error));
    CHECK_EQ(error, std::string("bad field: kind"));
    CHECK(!ParseStringDo(json{{"guid", 102u}, {"kind", "sit"}, {"target_guid", -1}}, error));
    CHECK_EQ(error, std::string("bad field: target_guid"));
    CHECK(!ParseStringDo(json{{"guid", 102u}, {"kind", "emote"}, {"emote", "spit"}}, error));
    CHECK_EQ(error, std::string("bad field: emote"));
    CHECK(!ParseStringDo(json{{"guid", 102u}, {"kind", "emote"}}, error));
    CHECK_EQ(error, std::string("bad field: emote"));
    CHECK(!ParseStringDo(json{{"guid", 102u}, {"kind", "sit"}, {"emote", "wave"}}, error));
    CHECK_EQ(error, std::string("bad field: emote"));
    CHECK(!ParseStringDo(json::array(), error));
}

LEEROY_TEST(Strings_EveryKindIsCarriedOutFromItsReadyFacts)
{
    for (auto kind : kAll)
        CHECK_EQ(StringRefusal(kind, Ready(kind)), std::string());
    auto party = InHisParty();
    CHECK_EQ(StringRefusal(StringKind::ComeToMe, party), std::string());   // his own ask, in his group
    CHECK_EQ(StringRefusal(StringKind::Emote, party), std::string());
    CHECK_EQ(StringRefusal(StringKind::Sit, party), std::string());
    auto resting = Free();
    resting.humanArea = resting.humanZone = 0;
    resting.humanResting = true;   // an inn in the open world
    CHECK_EQ(StringRefusal(StringKind::ComeToMe, resting), std::string());
    for (auto flag : {kAreaSlaveCapital, kAreaSlaveCapital2, kAreaCity, kAreaTown, kAreaRestHorde, kAreaRestAlliance})
    {
        auto f = Free();
        f.humanArea = 0;
        f.humanZone = flag;
        CHECK_EQ(StringRefusal(StringKind::ComeToMe, f), std::string());
    }
}

LEEROY_TEST(Strings_CommonRefusalsComeFirstForEveryKind)
{
    struct Case { void (*set)(StringFacts&); char const* code; };
    Case const cases[] = {
        {[](StringFacts& f) { f.enabled = false; }, "off"},
        {[](StringFacts& f) { f.humanOnline = false; }, "human_offline"},
        {[](StringFacts& f) { f.cast = false; }, "not_cast"},
        {[](StringFacts& f) { f.online = false; }, "not_online"},
        {[](StringFacts& f) { f.fence = "busy:realm_owner"; }, "busy:realm_owner"},
        {[](StringFacts& f) { f.fence = "busy:training_owner"; }, "busy:training_owner"},
        {[](StringFacts& f) { f.dead = true; }, "dead"},
        {[](StringFacts& f) { f.combat = true; }, "combat"},
        {[](StringFacts& f) { f.humanCombat = true; }, "combat"},
        {[](StringFacts& f) { f.instance = true; }, "in_instance"},
        {[](StringFacts& f) { f.humanInstance = true; }, "in_instance"},
        {[](StringFacts& f) { f.battleground = true; }, "in_bg"},
        {[](StringFacts& f) { f.humanBattleground = true; }, "in_bg"},
        {[](StringFacts& f) { f.queued = true; }, "queued"},
        {[](StringFacts& f) { f.travelling = true; }, "travelling"},
        {[](StringFacts& f) { f.humanTravelling = true; }, "travelling"},
        {[](StringFacts& f) { f.humanAlive = false; }, "human_dead"},
    };
    for (auto kind : kAll)
        for (auto const& c : cases)
        {
            auto f = Ready(kind);
            c.set(f);
            CHECK_EQ(StringRefusal(kind, f), std::string(c.code));
        }
    // Off wins over everything; the fence over a fight.
    auto f = Free();
    f.enabled = false; f.humanOnline = false; f.combat = true;
    CHECK_EQ(StringRefusal(StringKind::Sit, f), std::string("off"));
    f = Free();
    f.fence = "busy:realm_owner"; f.combat = true;
    CHECK_EQ(StringRefusal(StringKind::Sit, f), std::string("busy:realm_owner"));
}

LEEROY_TEST(Strings_RefusedInHisFightAndHisCopy)
{
    // Review focus: never inside his fight, never onto or inside his instance copy.
    for (auto kind : kAll)
    {
        auto fight = Ready(kind);
        fight.humanCombat = true;
        CHECK_EQ(StringRefusal(kind, fight), std::string("combat"));
        auto copy = Ready(kind);
        copy.humanInstance = true;
        CHECK_EQ(StringRefusal(kind, copy), std::string("in_instance"));
        auto inside = Ready(kind);
        inside.instance = inside.humanInstance = true;
        CHECK_EQ(StringRefusal(kind, inside), std::string("in_instance"));
    }
}

LEEROY_TEST(Strings_ComeToMeOnlyToACityOrRestAreaAndNeverOutOfAnotherGroup)
{
    auto f = Free();
    f.humanArea = kAreaAllowDuels;
    CHECK_EQ(StringRefusal(StringKind::ComeToMe, f), std::string("bad_area"));
    f.humanArea = kAreaSanctuary;   // a sanctuary alone is no city
    CHECK_EQ(StringRefusal(StringKind::ComeToMe, f), std::string("bad_area"));
    f = Free();
    f.otherGroup = true;
    CHECK_EQ(StringRefusal(StringKind::ComeToMe, f), std::string("grouped"));
}

LEEROY_TEST(Strings_JoinGroupOnlyIntoHisOwnPartyWithRoomAndUnderGuildAddsFences)
{
    auto f = Free();
    f.groupMembers = 4;
    CHECK_EQ(StringRefusal(StringKind::JoinGroup, f), std::string());
    f.groupMembers = 5;
    CHECK_EQ(StringRefusal(StringKind::JoinGroup, f), std::string("group_full"));
    f = Free();
    f.groupMembers = 6; f.groupRaid = true;   // never converts, never joins a raid
    CHECK_EQ(StringRefusal(StringKind::JoinGroup, f), std::string("in_raid"));
    f = Free();
    f.leader = false; f.groupMembers = 2;
    CHECK_EQ(StringRefusal(StringKind::JoinGroup, f), std::string("not_leader"));
    f = Free();
    f.groupSpecial = true; f.groupMembers = 3;
    CHECK_EQ(StringRefusal(StringKind::JoinGroup, f), std::string("lfg_group"));
    CHECK_EQ(StringRefusal(StringKind::JoinGroup, InHisParty()), std::string("in_group"));
    f = Free();
    f.joinRefusal = "grouped";
    CHECK_EQ(StringRefusal(StringKind::JoinGroup, f), std::string("grouped"));
    f.joinRefusal = "locked";
    CHECK_EQ(StringRefusal(StringKind::JoinGroup, f), std::string("locked"));
}

LEEROY_TEST(Strings_FollowOnlyInHisPartyNeverInHisRaid)
{
    CHECK_EQ(StringRefusal(StringKind::Follow, Free()), std::string("not_in_group"));
    auto f = Free();
    f.otherGroup = true;
    CHECK_EQ(StringRefusal(StringKind::Follow, f), std::string("not_in_group"));
    f = InHisParty();
    f.groupRaid = true; f.groupMembers = 10;
    CHECK_EQ(StringRefusal(StringKind::Follow, f), std::string("busy:raid_owner"));
}

LEEROY_TEST(Strings_DuelMeOnlyUngroupedNearbyAndWhereTheCoreAllowsDuels)
{
    CHECK_EQ(StringRefusal(StringKind::DuelMe, InHisParty()), std::string("in_group"));
    auto f = Free();
    f.duel = true;
    CHECK_EQ(StringRefusal(StringKind::DuelMe, f), std::string("busy:duel"));
    f = Free();
    f.humanDuel = true;
    CHECK_EQ(StringRefusal(StringKind::DuelMe, f), std::string("busy:duel"));
    f = Free();
    f.sameZone = false;
    CHECK_EQ(StringRefusal(StringKind::DuelMe, f), std::string("too_far"));
    f = Free();
    f.distance = kStringDuelYards + 1.0f;
    CHECK_EQ(StringRefusal(StringKind::DuelMe, f), std::string("too_far"));
    f = Free();
    f.sameMap = false;
    CHECK_EQ(StringRefusal(StringKind::DuelMe, f), std::string("too_far"));
    f = Free();
    f.area = 0;
    CHECK_EQ(StringRefusal(StringKind::DuelMe, f), std::string("bad_area"));
    f = Free();
    f.humanArea = kAreaCapital;
    CHECK_EQ(StringRefusal(StringKind::DuelMe, f), std::string("bad_area"));
    f = Free();
    f.humanArea |= kAreaSanctuary;
    CHECK_EQ(StringRefusal(StringKind::DuelMe, f), std::string("bad_area"));
    f = Free();
    f.otherGroup = true;   // her own group is no bar to a duel with him
    CHECK_EQ(StringRefusal(StringKind::DuelMe, f), std::string());
}

LEEROY_TEST(Strings_EmoteAndSitOnlyWhereHeSeesThem)
{
    auto f = Free();
    f.emote = "spit";
    CHECK_EQ(StringRefusal(StringKind::Emote, f), std::string("bad_emote"));
    for (auto name : kStringEmotes)
    {
        f.emote = std::string(name);
        CHECK_EQ(StringRefusal(StringKind::Emote, f), std::string());
    }
    f = Free();
    f.distance = kStringEmoteYards + 0.5f;
    CHECK_EQ(StringRefusal(StringKind::Emote, f), std::string("too_far"));
    f = Free();
    f.sameMap = false;
    CHECK_EQ(StringRefusal(StringKind::Emote, f), std::string("too_far"));
    CHECK_EQ(StringRefusal(StringKind::Sit, f), std::string("too_far"));
    f = Free();
    f.distance = kStringSitYards + 0.5f;
    CHECK_EQ(StringRefusal(StringKind::Sit, f), std::string("too_far"));
    f.distance = kStringSitYards;
    CHECK_EQ(StringRefusal(StringKind::Sit, f), std::string());
}

LEEROY_TEST(Strings_ListedEmotesAreTheCoresNamesWithNoRudeOnes)
{
    for (auto rude : {"spit", "slap", "rude", "insult", "moon", "fart"})
        CHECK(!StringEmoteAllowed(rude));
    CHECK(StringEmoteAllowed("wave"));
    CHECK(StringEmoteAllowed("kiss"));
    CHECK(!StringEmoteAllowed(""));
    CHECK(!StringEmoteAllowed("Wave"));
}

namespace
{
StringHoldState ComeHold(std::uint64_t now)
{
    StringHoldState h;
    h.until = now + kStringStayMs;
    h.arriveBy = now + 30000;
    return h;
}

StringHoldFacts Tick(std::uint64_t now, std::uint64_t group = 0, bool raid = false)
{
    StringHoldFacts f;
    f.now = now;
    f.on = f.humanOnline = true;
    f.group = group;
    f.raid = raid;
    return f;
}
}

LEEROY_TEST(Strings_ComeToMeStayFencesHerForItsWholeWindowOnly)
{
    // Review P2-1: from the string until its ten minutes end, busy:string keeps every other owner out.
    auto h = ComeHold(1000);
    CHECK(StringHoldFences(h));                 // on her way
    h.arriveBy = 0; h.stay = true;
    CHECK(StringHoldFences(h));                 // landed, stay on
    h.stay = false;
    CHECK(StringHoldFences(h));                 // landed beside a stay someone else gave: still his ask
    StringHoldState follow;
    follow.group = 77; follow.follow = true;
    CHECK(!StringHoldFences(follow));           // in his group: the Realm refuses her there anyway
    follow.follow = false; follow.stay = true;  // a stopped follow
    CHECK(!StringHoldFences(follow));
    CHECK(!StringHoldFences(StringHoldState{}));
}

LEEROY_TEST(Strings_ComeToMeHoldEndsWithItsStayHisLogoutTheKeyOrAGroup)
{
    auto h = ComeHold(1000);
    CHECK(DecideStringHold(h, Tick(2000)) == StringHoldStep::Keep);
    CHECK(DecideStringHold(h, Tick(1000 + 30000)) == StringHoldStep::Lift);   // never landed
    h.arriveBy = 0; h.stay = true;
    CHECK(DecideStringHold(h, Tick(1000 + kStringStayMs - 1)) == StringHoldStep::Keep);
    CHECK(DecideStringHold(h, Tick(1000 + kStringStayMs)) == StringHoldStep::Lift);
    auto off = Tick(2000);
    off.on = false;
    CHECK(DecideStringHold(h, off) == StringHoldStep::Lift);
    auto gone = Tick(2000);
    gone.humanOnline = false;
    CHECK(DecideStringHold(h, gone) == StringHoldStep::Lift);
    CHECK(DecideStringHold(h, Tick(2000, 55)) == StringHoldStep::Drop);   // a join reset her strategies
}

LEEROY_TEST(Strings_FollowHoldEndsWithItsGroupAndGivesFollowBackInARaid)
{
    StringHoldState h;
    h.group = 77; h.follow = true;
    CHECK(DecideStringHold(h, Tick(5, 77)) == StringHoldStep::Keep);
    CHECK(DecideStringHold(h, Tick(5, 0)) == StringHoldStep::Lift);
    CHECK(DecideStringHold(h, Tick(5, 78)) == StringHoldStep::Lift);
    // Review P3-3: a stop in a party that became a raid is lifted natively, her follow given back.
    h.follow = false; h.stay = true; h.unfollowed = true;
    CHECK(DecideStringHold(h, Tick(5, 77)) == StringHoldStep::Keep);
    CHECK(DecideStringHold(h, Tick(5, 77, true)) == StringHoldStep::LiftFollow);
    auto off = Tick(5, 77);
    off.on = false;
    CHECK(DecideStringHold(h, off) == StringHoldStep::LiftFollow);
    CHECK(DecideStringHold(h, Tick(5, 0)) == StringHoldStep::Lift);   // the group is gone: no follow given back
    h.unfollowed = false;
    CHECK(DecideStringHold(h, Tick(5, 77, true)) == StringHoldStep::Lift);
}

LEEROY_TEST(Strings_ComeToMeFenceIsOneMembersOwnForARearm)
{
    // A rearm leaves her out at its ack instead of refusing every member with her.
    CHECK(Leeroy::Realm::MemberSkipReason("busy:string"));
}
