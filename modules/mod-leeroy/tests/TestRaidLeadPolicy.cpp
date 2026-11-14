// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyGuildCommands.h"
#include "LeeroyRaidLeadPolicy.h"
#include "TestHarness.h"

#include <nlohmann/json.hpp>
#include <string>

using namespace Leeroy::Guild;
using nlohmann::json;

namespace
{
RaidOrderBody Ready()
{
    RaidOrderBody body;
    body.enabled = body.ownerOnline = body.ownerLeads = body.inHisGroup = body.sameMap = true;
    return body;
}

RaidLeadFacts Facts()
{
    RaidLeadFacts f;
    f.enabled = f.ownerOnline = f.ownerLeads = f.raid = f.memberInGroup = f.memberIsCast = true;
    return f;
}

std::string Error(json const& message)
{
    std::string error = "stale";
    CHECK(!ParseRaidLead(message, error));
    return error;
}
}

LEEROY_TEST(RaidLead_OrderListIsTheBotDoAllowList)
{
    for (auto order : kRaidOrders)
    {
        CHECK(IsRaidOrder(order));
        CHECK(BotDoParamAllowed("order", order));
        CHECK(PlanRaidOrder(order).has_value());
    }
    for (std::string_view bad : {"", "lust", "bloodlust", "dance", "follow ", "FOLLOW", "release,stay", "co +passive"})
    {
        CHECK(!IsRaidOrder(bad));
        CHECK(!BotDoParamAllowed("order", bad));
        CHECK(!PlanRaidOrder(bad));
    }
    // An order is no strategy string: the signed whitelist of co/nc stays as it was.
    CHECK(!BotDoParamAllowed("co", "+passive"));
    CHECK(!BotDoParamAllowed("co", "+move from group"));
}

LEEROY_TEST(RaidLead_PlansUsePlayerbotsOwnShortcutStrategies)
{
    auto follow = *PlanRaidOrder("follow");
    CHECK_EQ(follow.nonCombat, std::string_view("+follow,-passive,-grind,-move from group"));
    CHECK_EQ(follow.combat, std::string_view("-stay,-follow,-passive,-grind,-move from group"));
    CHECK(follow.clearPositions && !follow.stayHere && !follow.attackHisTarget);
    CHECK_EQ(PlanRaidOrder("come_back")->nonCombat, follow.nonCombat);

    auto stay = *PlanRaidOrder("stay");
    CHECK_EQ(stay.combat, std::string_view("+stay,-follow,-passive,-move from group"));
    CHECK(stay.stayHere && !stay.stayAtTarget);
    auto stack = *PlanRaidOrder("stack");
    CHECK(stack.stayAtTarget && !stack.stayHere);
    CHECK_EQ(stack.combat, stay.combat);

    CHECK_EQ(PlanRaidOrder("spread")->combat, std::string_view("+move from group"));
    CHECK_EQ(PlanRaidOrder("run_out")->nonCombat, std::string_view("+move from group"));
    CHECK_EQ(PlanRaidOrder("back_off")->combat, std::string_view("+follow,-stay,+passive"));
    CHECK_EQ(PlanRaidOrder("hold")->combat, std::string_view("+passive"));

    auto attack = *PlanRaidOrder("attack");
    CHECK(attack.attackHisTarget && attack.combat == std::string_view("-passive"));
    auto skull = *PlanRaidOrder("kill_skull");
    CHECK(skull.rtiSkull && !skull.attackHisTarget);
    CHECK(PlanRaidOrder("taunt")->taunt && PlanRaidOrder("taunt")->combat.empty());
    CHECK(PlanRaidOrder("release")->release && PlanRaidOrder("release")->nonCombat.empty());
}

LEEROY_TEST(RaidLead_OrdersReachOnlyHisOwnGroupBehindTheFences)
{
    CHECK(RaidOrderRefusal("follow", Ready()).empty());
    auto body = Ready();
    body.enabled = false;
    CHECK_EQ(RaidOrderRefusal("follow", body), std::string_view("raidlead_off"));
    body = Ready(); body.ownerOnline = false;
    CHECK_EQ(RaidOrderRefusal("follow", body), std::string_view("owner_offline"));
    body = Ready(); body.ownerLeads = false;
    CHECK_EQ(RaidOrderRefusal("stack", body), std::string_view("not_leader"));
    body = Ready(); body.inHisGroup = false;
    CHECK_EQ(RaidOrderRefusal("stack", body), std::string_view("not_in_group"));
    for (std::string fence : {"busy:realm_owner", "busy:training_owner", "busy:training_cleanup", "busy:training_state"})
    {
        body = Ready(); body.fence = fence;
        CHECK_EQ(std::string(RaidOrderRefusal("hold", body)), fence);
        CHECK_EQ(std::string(RaidOrderRefusal("release", body)), fence);
    }
    body = Ready(); body.sameMap = false;
    CHECK_EQ(RaidOrderRefusal("spread", body), std::string_view("elsewhere"));
    body = Ready(); body.alive = false;
    CHECK_EQ(RaidOrderRefusal("attack", body), std::string_view("dead"));
    CHECK(RaidOrderRefusal("bogus", Ready()) == std::string_view("bad_order"));
}

LEEROY_TEST(RaidLead_ReleaseIsForTheDeadWhoHaveNotReleased)
{
    auto body = Ready();
    CHECK_EQ(RaidOrderRefusal("release", body), std::string_view("alive"));
    body.alive = false;
    body.sameMap = false; // a body on another map may still release
    CHECK(RaidOrderRefusal("release", body).empty());
    body.ghost = true;
    CHECK_EQ(RaidOrderRefusal("release", body), std::string_view("released"));
}

LEEROY_TEST(RaidLead_TauntIsASwapNeverARetaunt)
{
    CHECK(TauntRefusal(true, false, true).empty());
    CHECK_EQ(TauntRefusal(true, true, true), std::string_view("has_aggro"));
    CHECK_EQ(TauntRefusal(false, false, true), std::string_view("no_target"));
    CHECK_EQ(TauntRefusal(true, false, false), std::string_view("no_taunt"));
    CHECK_EQ(TauntSpell(1), std::string_view("taunt"));
    CHECK_EQ(TauntSpell(2), std::string_view("hand of reckoning"));
    CHECK_EQ(TauntSpell(6), std::string_view("dark command"));
    CHECK_EQ(TauntSpell(11), std::string_view("growl"));
    for (int cls : {3, 4, 5, 7, 8, 9})
        CHECK(TauntSpell(static_cast<std::uint8_t>(cls)).empty());
}

LEEROY_TEST(RaidLead_IconsCountFromStarToSkull)
{
    CHECK_EQ(int(*RaidIconIndex("skull")), 7);
    CHECK_EQ(int(*RaidIconIndex("cross")), 6);
    CHECK_EQ(int(*RaidIconIndex("star")), 0);
    CHECK(!RaidIconIndex("Skull") && !RaidIconIndex("") && !RaidIconIndex("x"));
}

LEEROY_TEST(RaidLead_AssistantFlagNeverForHimNorOutsideHisRaid)
{
    CHECK(RaidLeadRefusal("assist", Facts()).empty());
    CHECK(RaidLeadRefusal("unassist", Facts()).empty());
    auto f = Facts(); f.enabled = false;
    CHECK_EQ(RaidLeadRefusal("assist", f), std::string_view("raidlead_off"));
    f = Facts(); f.memberIsOwner = true; f.memberIsCast = false;
    CHECK_EQ(RaidLeadRefusal("assist", f), std::string_view("owner"));
    f = Facts(); f.memberIsCast = false;
    CHECK_EQ(RaidLeadRefusal("assist", f), std::string_view("not_cast"));
    f = Facts(); f.memberInGroup = false;
    CHECK_EQ(RaidLeadRefusal("assist", f), std::string_view("not_in_group"));
    f = Facts(); f.ownerLeads = false;
    CHECK_EQ(RaidLeadRefusal("assist", f), std::string_view("not_leader"));
    f = Facts(); f.ownerOnline = false;
    CHECK_EQ(RaidLeadRefusal("assist", f), std::string_view("owner_offline"));
    f = Facts(); f.raid = false;
    CHECK_EQ(RaidLeadRefusal("assist", f), std::string_view("not_raid"));
    f = Facts(); f.fenced = true;
    CHECK_EQ(RaidLeadRefusal("assist", f), std::string_view("busy:realm_owner"));
    CHECK_EQ(RaidLeadRefusal("promote", Facts()), std::string_view("bad_action"));
}

LEEROY_TEST(RaidLead_MarkingNeedsAHostileTargetAndRaidRights)
{
    auto f = Facts();
    CHECK_EQ(RaidLeadRefusal("mark", f), std::string_view("not_assistant"));
    f.memberIsAssistant = true;
    CHECK_EQ(RaidLeadRefusal("mark", f), std::string_view("no_target"));
    f.targetHostile = true;
    CHECK(RaidLeadRefusal("mark", f).empty());
    // In a party anyone may set an icon; he may always set one himself.
    f = Facts(); f.raid = false; f.targetHostile = true;
    CHECK(RaidLeadRefusal("mark", f).empty());
    f = Facts(); f.memberIsOwner = true; f.memberIsCast = false; f.memberIsAssistant = true; f.targetHostile = true;
    CHECK(RaidLeadRefusal("mark", f).empty());
}

LEEROY_TEST(RaidLead_CommandFieldsAreExact)
{
    std::string error;
    auto assist = ParseRaidLead(json{{"action", "assist"}, {"guid", 101u}}, error);
    CHECK(assist && assist->action == "assist" && assist->guid == 101u && assist->icon.empty() && error.empty());
    auto mark = ParseRaidLead(json{{"action", "mark"}, {"guid", 101u}, {"icon", "skull"}}, error);
    CHECK(mark && mark->icon == "skull");
    CHECK_EQ(Error(json{{"action", "promote"}, {"guid", 101u}}), std::string("bad field: action"));
    CHECK_EQ(Error(json{{"guid", 101u}}), std::string("bad field: action"));
    CHECK_EQ(Error(json{{"action", "assist"}}), std::string("bad field: guid"));
    CHECK_EQ(Error(json{{"action", "assist"}, {"guid", 0}}), std::string("bad field: guid"));
    CHECK_EQ(Error(json{{"action", "assist"}, {"guid", -1}}), std::string("bad field: guid"));
    CHECK_EQ(Error(json{{"action", "assist"}, {"guid", 4294967296ull}}), std::string("bad field: guid"));
    CHECK_EQ(Error(json{{"action", "assist"}, {"guid", 101u}, {"icon", "skull"}}), std::string("bad field: icon"));
    CHECK_EQ(Error(json{{"action", "assist"}, {"guid", 101u}, {"icon", 7}}), std::string("bad field: icon"));
    CHECK(ParseRaidLead(json{{"action", "unassist"}, {"guid", 101u}, {"icon", ""}}, error));
    CHECK_EQ(Error(json{{"action", "mark"}, {"guid", 101u}}), std::string("bad field: icon"));
    CHECK_EQ(Error(json{{"action", "mark"}, {"guid", 101u}, {"icon", "moonbeam"}}), std::string("bad field: icon"));
    CHECK_EQ(Error(json::array()), std::string("bad field: action"));
}
