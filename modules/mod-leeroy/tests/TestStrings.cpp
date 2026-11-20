// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyInviteSpots.h"
#include "LeeroyRealmCommands.h"
#include "LeeroyRealmFailure.h"
#include "LeeroyStrings.h"
#include "TestHarness.h"

#include <cmath>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <set>
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
    f.spotValid = true;   // go_to's spot lies on its map
    f.fishing = true;     // fish: she has the skill
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
    StringKind::Emote, StringKind::Sit, StringKind::GoTo, StringKind::Fish};

// The facts each kind is carried out from: follow needs her in his party, the rest are taken free.
StringFacts Ready(StringKind kind) { return kind == StringKind::Follow ? InHisParty() : Free(); }
}

LEEROY_TEST(Strings_KindsRoundTripByName)
{
    for (auto kind : kAll)
        CHECK(ParseStringKind(StringKindName(kind)) == kind);
    CHECK(!ParseStringKind("queue_arena").has_value());
    CHECK(!ParseStringKind("Come_To_Me").has_value());
    CHECK(InviteKind(StringKind::GoTo) && InviteKind(StringKind::Fish));
    for (auto kind : {StringKind::ComeToMe, StringKind::JoinGroup, StringKind::Follow, StringKind::DuelMe,
             StringKind::Emote, StringKind::Sit})
        CHECK(!InviteKind(kind));
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

// -- his invitations: go_to, fish and the "via": "invite" mark -------------------------------------------------------

LEEROY_TEST(Invites_ParseGoToNeedsAListedSpotAndASlotOfTwo)
{
    std::string error;
    auto cmd = ParseStringDo(json{{"guid", 102u}, {"kind", "go_to"}, {"spot", "dal_fountain"}}, error);
    CHECK(cmd.has_value());
    CHECK(cmd->kind == StringKind::GoTo && cmd->spot == "dal_fountain" && cmd->slot == 0u && cmd->invite);
    cmd = ParseStringDo(json{{"guid", 102u}, {"kind", "go_to"}, {"spot", "org_inn"}, {"slot", 1u}, {"via", "invite"}},
        error);
    CHECK(cmd.has_value() && cmd->slot == 1u && cmd->invite);
    CHECK(!ParseStringDo(json{{"guid", 102u}, {"kind", "go_to"}}, error));
    CHECK_EQ(error, std::string("bad field: spot"));
    CHECK(!ParseStringDo(json{{"guid", 102u}, {"kind", "go_to"}, {"spot", "stormwind_trade"}}, error));
    CHECK_EQ(error, std::string("bad field: spot"));   // a place, but no meeting spot
    CHECK(!ParseStringDo(json{{"guid", 102u}, {"kind", "go_to"}, {"spot", 7}}, error));
    CHECK_EQ(error, std::string("bad field: spot"));
    CHECK(!ParseStringDo(json{{"guid", 102u}, {"kind", "go_to"}, {"spot", "dal_inn"}, {"slot", 2u}}, error));
    CHECK_EQ(error, std::string("bad field: slot"));
    CHECK(!ParseStringDo(json{{"guid", 102u}, {"kind", "go_to"}, {"spot", "dal_inn"}, {"slot", -1}}, error));
    CHECK_EQ(error, std::string("bad field: slot"));
    // Every other kind names no spot and no slot.
    CHECK(!ParseStringDo(json{{"guid", 102u}, {"kind", "come_to_me"}, {"spot", "dal_inn"}}, error));
    CHECK_EQ(error, std::string("bad field: spot"));
    CHECK(!ParseStringDo(json{{"guid", 102u}, {"kind", "fish"}, {"slot", 1u}}, error));
    CHECK_EQ(error, std::string("bad field: slot"));
    cmd = ParseStringDo(json{{"guid", 102u}, {"kind", "fish"}, {"spot", ""}, {"slot", 0u}}, error);
    CHECK(cmd.has_value() && cmd->invite && cmd->spot.empty());
}

LEEROY_TEST(Invites_ViaMarksAnInvitationAndNothingElse)
{
    std::string error;
    auto cmd = ParseStringDo(json{{"guid", 102u}, {"kind", "come_to_me"}, {"via", "invite"}}, error);
    CHECK(cmd.has_value() && cmd->invite);
    cmd = ParseStringDo(json{{"guid", 102u}, {"kind", "come_to_me"}, {"via", ""}}, error);
    CHECK(cmd.has_value() && !cmd->invite);
    cmd = ParseStringDo(json{{"guid", 102u}, {"kind", "sit"}}, error);
    CHECK(cmd.has_value() && !cmd->invite);
    CHECK(!ParseStringDo(json{{"guid", 102u}, {"kind", "sit"}, {"via", "console"}}, error));
    CHECK_EQ(error, std::string("bad field: via"));
    CHECK(!ParseStringDo(json{{"guid", 102u}, {"kind", "sit"}, {"via", true}}, error));
    CHECK_EQ(error, std::string("bad field: via"));
}

LEEROY_TEST(Invites_GoToNeverOutOfAGroupAndOnlyToASpotOnItsMap)
{
    auto f = Free();
    f.humanArea = f.humanZone = 0;   // he need not stand in a town: the spot is one
    CHECK_EQ(StringRefusal(StringKind::GoTo, f), std::string());
    CHECK_EQ(StringRefusal(StringKind::GoTo, InHisParty()), std::string("in_group"));
    f = Free();
    f.otherGroup = true;
    CHECK_EQ(StringRefusal(StringKind::GoTo, f), std::string("grouped"));
    f = Free();
    f.spotValid = false;
    CHECK_EQ(StringRefusal(StringKind::GoTo, f), std::string("bad_spot"));
    f = Free();
    f.sameMap = false;   // he may be anywhere
    CHECK_EQ(StringRefusal(StringKind::GoTo, f), std::string());
}

LEEROY_TEST(Invites_FishBesideHimOrAtHisFishingSpotWithTheSkill)
{
    CHECK_EQ(StringRefusal(StringKind::Fish, Free()), std::string());
    CHECK_EQ(StringRefusal(StringKind::Fish, InHisParty()), std::string());   // fishing with him in his group
    auto f = Free();
    f.fishing = false;
    CHECK_EQ(StringRefusal(StringKind::Fish, f), std::string("no_fishing"));
    f = Free();
    f.otherGroup = true;
    CHECK_EQ(StringRefusal(StringKind::Fish, f), std::string("grouped"));
    f = Free();
    f.distance = kInviteFishYards + 1.0f;
    CHECK_EQ(StringRefusal(StringKind::Fish, f), std::string("too_far"));
    f.atFishSpot = true;   // where he sent her to fish, before he is there
    CHECK_EQ(StringRefusal(StringKind::Fish, f), std::string());
    f = Free();
    f.sameMap = false;
    CHECK_EQ(StringRefusal(StringKind::Fish, f), std::string("too_far"));
    f.atFishSpot = true;
    CHECK_EQ(StringRefusal(StringKind::Fish, f), std::string());
    // Never in a fight, an instance, a battleground, a queue or on the way: the shared codes first.
    f = Free();
    f.humanCombat = true;
    CHECK_EQ(StringRefusal(StringKind::Fish, f), std::string("combat"));
}

namespace
{
StringHoldState FishHold(std::uint64_t now, std::uint64_t group = 0)
{
    StringHoldState h;
    h.group = group;
    h.until = group ? 0 : now + kInviteFishMs;
    h.fish = h.fishAdded = h.invite = true;
    h.fishUntil = now + kInviteFishMs;
    return h;
}

StringHoldFacts Near(StringHoldFacts f)
{
    f.near = true;
    f.left = false;
    return f;
}

StringHoldFacts Away(StringHoldFacts f)
{
    f.near = false;
    f.left = true;
    return f;
}
}

LEEROY_TEST(Invites_FishingAloneEndsWithItsTimeOrOnceHeCameAndWent)
{
    auto h = FishHold(1000);
    CHECK(StringHoldFences(h));   // outside his group she is held for it, as come_to_me holds her
    // At a spot before he comes: his absence is no leaving.
    CHECK(DecideStringHold(h, Away(Tick(2000))) == StringHoldStep::Keep);
    auto near = Near(Tick(3000));
    NoteStringHold(h, near);
    CHECK(h.met);
    CHECK(DecideStringHold(h, near) == StringHoldStep::Keep);
    NoteStringHold(h, Away(Tick(4000)));
    CHECK(h.met);   // the note never forgets that he came
    CHECK(DecideStringHold(h, Away(Tick(4000))) == StringHoldStep::Lift);
    h = FishHold(1000);
    CHECK(DecideStringHold(h, Near(Tick(1000 + kInviteFishMs - 1))) == StringHoldStep::Keep);
    CHECK(DecideStringHold(h, Near(Tick(1000 + kInviteFishMs))) == StringHoldStep::Lift);
    auto off = Near(Tick(2000));
    off.on = false;
    CHECK(DecideStringHold(h, off) == StringHoldStep::Lift);
    CHECK(DecideStringHold(h, Near(Tick(2000, 55))) == StringHoldStep::Drop);   // she joined a group
    StringHoldState plain;
    plain.until = 9999;
    NoteStringHold(plain, Near(Tick(10)));
    CHECK(!plain.met);   // only a fishing hold counts his visits
}

LEEROY_TEST(Invites_FishingInHisGroupLastsInHisRaidAndEndsAloneBesideAFollow)
{
    auto h = FishHold(1000, 77);
    CHECK(!StringHoldFences(h));   // in his group: the Realm refuses her there anyway
    CHECK(DecideStringHold(h, Near(Tick(2000, 77))) == StringHoldStep::Keep);
    CHECK(DecideStringHold(h, Near(Tick(2000, 77, true))) == StringHoldStep::Keep);   // his raid
    CHECK(DecideStringHold(h, Near(Tick(1000 + kInviteFishMs, 77))) == StringHoldStep::Lift);
    CHECK(DecideStringHold(h, Near(Tick(2000, 78))) == StringHoldStep::Lift);
    CHECK(DecideStringHold(h, Near(Tick(2000, 0))) == StringHoldStep::Lift);
    auto off = Near(Tick(2000, 77));
    off.on = false;
    CHECK(DecideStringHold(h, off) == StringHoldStep::Lift);
    // Beside a follow of his: only the fishing ends, the follow goes on (and still lifts in a raid as before).
    h.follow = true;
    CHECK(DecideStringHold(h, Near(Tick(1000 + kInviteFishMs, 77))) == StringHoldStep::EndFish);
    h.fish = false;
    CHECK(DecideStringHold(h, Near(Tick(1000 + kInviteFishMs, 77))) == StringHoldStep::Keep);
    CHECK(DecideStringHold(h, Near(Tick(2000, 77, true))) == StringHoldStep::Lift);
}

LEEROY_TEST(Invites_GoToHoldIsAComeToMeHoldAtTheSpot)
{
    StringHoldState h = ComeHold(1000);
    h.spot = InviteSpotIndex("dal_inn");
    h.invite = true;
    CHECK(StringHoldFences(h));
    CHECK(DecideStringHold(h, Tick(2000)) == StringHoldStep::Keep);
    CHECK(DecideStringHold(h, Tick(1000 + 30000)) == StringHoldStep::Lift);   // never landed
    CHECK(DecideStringHold(h, Tick(2000, 9)) == StringHoldStep::Drop);
}

LEEROY_TEST(Invites_SpotsAreUniqueKnownAndAgreeWithThePlaces)
{
    std::set<std::string_view> names;
    for (auto const& spot : kInviteSpots)
    {
        CHECK(names.insert(spot.name).second);
        CHECK(FindInviteSpot(spot.name) == &spot);
        CHECK(spot.zone != 0);
        CHECK(spot.o >= 0.0f && spot.o <= 6.2832f);
        CHECK(std::isfinite(spot.x) && std::isfinite(spot.y) && std::isfinite(spot.z));
    }
    CHECK(FindInviteSpot("nowhere") == nullptr);
    CHECK_EQ(InviteSpotIndex("nowhere"), -1);
    CHECK(FindInviteSpot("dal_fountain") && FindInviteSpot("dal_fountain")->fish);
    CHECK(FindInviteSpot("dal_krasus") && !FindInviteSpot("dal_krasus")->fish);
    // The places' whitelist and the spots agree where they share a name: one point for one place.
    auto path = std::filesystem::path(LEEROY_TEST_FIXTURES).parent_path().parent_path() / "conf" / "leeroy_places.csv.dist";
    std::ifstream stream(path);
    std::string csv{std::istreambuf_iterator<char>(stream), std::istreambuf_iterator<char>()};
    std::string error;
    auto places = Leeroy::Realm::ParsePlaces(csv, error);
    CHECK(places.has_value());
    if (!places)
        return;
    int shared = 0;
    for (auto const& place : *places)
        if (InviteSpot const* spot = FindInviteSpot(place.name))
        {
            ++shared;
            CHECK_EQ(spot->map, place.map);
            CHECK_EQ(spot->zone, place.zone);
            CHECK(spot->x == place.x && spot->y == place.y && spot->z == place.z && spot->o == place.o);
            CHECK_EQ(spot->fish, place.kind == "fish");
        }
    CHECK_EQ(shared, 8);
}

LEEROY_TEST(Invites_TwoSlotsStandTwoYardsApartInFrontOfTheSpot)
{
    InviteSpot const& spot = *FindInviteSpot("org_inn");
    auto const [x0, y0] = InviteSpotPoint(spot, 0);
    auto const [x1, y1] = InviteSpotPoint(spot, 1);
    CHECK(std::fabs(std::hypot(x0 - spot.x, y0 - spot.y) - kInviteSpotFront) < 0.01f);
    CHECK(std::fabs(std::hypot(x1 - x0, y1 - y0) - kInviteSpotSide) < 0.01f);
    // In front: along the way the spot faces.
    CHECK((x0 - spot.x) * std::cos(spot.o) + (y0 - spot.y) * std::sin(spot.o) > 1.9f);
}
