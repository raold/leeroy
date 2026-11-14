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
    f.enabled = f.ownerOnline = f.ownerLeads = f.memberInGroup = f.memberIsCast = true;
    return f;
}

std::string Error(json const& message)
{
    std::string error = "stale";
    CHECK(!ParseRaidLead(message, error));
    return error;
}

bool Passive(std::string_view changes)
{
    return changes.find("+passive") != std::string_view::npos;
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
    for (std::string_view bad : {"", "bloodlust", "dance", "follow ", "FOLLOW", "release,stay", "co +passive", "assist"})
    {
        CHECK(!IsRaidOrder(bad));
        CHECK(!BotDoParamAllowed("order", bad));
        CHECK(!PlanRaidOrder(bad));
    }
    CHECK(!BotDoParamAllowed("co", "+passive"));
    CHECK(!BotDoParamAllowed("co", "+move from group"));
}

LEEROY_TEST(RaidLead_OnlyAHoldEverMakesABotPassive)
{
    for (auto order : kRaidOrders)
    {
        auto plan = *PlanRaidOrder(order);
        bool const passive = Passive(plan.nonCombat) || Passive(plan.combat);
        CHECK_EQ(passive, order == "hold");
        CHECK_EQ(plan.hold, order == "hold");
        // Nothing leaves playerbots' passive "move from group" strategy behind.
        CHECK(plan.nonCombat.find("+move from group") == std::string_view::npos);
        CHECK(plan.combat.find("+move from group") == std::string_view::npos);
    }
}

LEEROY_TEST(RaidLead_PlansUsePlayerbotsOwnStrategiesAndActions)
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
    CHECK(stack.stayAtTarget && !stack.stayHere && stack.combat == stay.combat);

    auto spread = *PlanRaidOrder("spread");
    CHECK(spread.moveAway > 0 && spread.nonCombat.empty() && spread.combat.empty());
    auto runOut = *PlanRaidOrder("run_out");
    CHECK(runOut.moveAway > spread.moveAway && runOut.combat.empty());
    auto backOff = *PlanRaidOrder("back_off");
    CHECK(backOff.toHisSide && backOff.combat.empty() && backOff.moveAway == 0);

    CHECK_EQ(PlanRaidOrder("hold")->combat, std::string_view("+passive"));
    CHECK_EQ(PlanRaidOrder("lift")->combat, std::string_view("-passive"));
    CHECK_EQ(PlanRaidOrder("lift")->nonCombat, std::string_view("-passive"));
    auto attack = *PlanRaidOrder("attack");
    CHECK(attack.attackHisTarget && attack.combat == std::string_view("-passive"));
    auto skull = *PlanRaidOrder("kill_skull");
    CHECK(skull.rtiSkull && !skull.attackHisTarget && skull.combat == std::string_view("-passive"));
    CHECK(PlanRaidOrder("taunt")->taunt && PlanRaidOrder("taunt")->combat.empty());
    CHECK(PlanRaidOrder("lust")->lust && PlanRaidOrder("lust")->combat.empty());
    CHECK(PlanRaidOrder("release")->release && PlanRaidOrder("release")->nonCombat.empty());
}

LEEROY_TEST(RaidLead_OrdersReachOnlyHisOwnGroupBehindTheFences)
{
    CHECK(RaidOrderRefusal("follow", Ready()).empty());
    auto body = Ready();
    body.enabled = false;
    for (auto order : kRaidOrders)
        if (order != "lift")
            CHECK_EQ(RaidOrderRefusal(order, body), std::string_view("raidlead_off"));
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
        CHECK_EQ(std::string(RaidOrderRefusal("lust", body)), fence);
        CHECK_EQ(std::string(RaidOrderRefusal("release", body)), fence);
    }
    body = Ready(); body.sameMap = false;
    CHECK_EQ(RaidOrderRefusal("spread", body), std::string_view("elsewhere"));
    body = Ready(); body.alive = false;
    CHECK_EQ(RaidOrderRefusal("attack", body), std::string_view("dead"));
    CHECK(RaidOrderRefusal("bogus", Ready()) == std::string_view("bad_order"));
}

LEEROY_TEST(RaidLead_TanksAndHealersNeverHold)
{
    auto body = Ready();
    body.tankOrHealer = true;
    CHECK_EQ(RaidOrderRefusal("hold", body), std::string_view("not_damage"));
    for (std::string_view order : {"spread", "run_out", "back_off", "follow", "stack", "taunt", "lust", "lift"})
        CHECK(RaidOrderRefusal(order, body).empty());
}

LEEROY_TEST(RaidLead_LiftIsCleanupEvenWithTheSwitchOff)
{
    auto body = Ready();
    body.enabled = false;
    body.ownerLeads = false;
    body.alive = false;
    body.sameMap = false;
    CHECK(RaidOrderRefusal("lift", body).empty());
    body.fence = "busy:realm_owner";
    CHECK_EQ(RaidOrderRefusal("lift", body), std::string_view("busy:realm_owner"));
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
    CHECK_EQ(LustSpell(true), std::string_view("bloodlust"));
    CHECK_EQ(LustSpell(false), std::string_view("heroism"));
}

LEEROY_TEST(RaidLead_AttackCountsABotAlreadyOnItOrOutOfSight)
{
    CHECK(AttackCounts(true, false, true));
    CHECK(AttackCounts(false, true, true));
    CHECK(AttackCounts(false, false, false));
    CHECK(!AttackCounts(false, false, true));
}

LEEROY_TEST(RaidLead_AHoldEndsAtHisPullAtFightsEndOrAfterThirtySeconds)
{
    RaidHold before{1000 + kRaidHoldMs, false};
    CHECK(DecideHold(before, 1000, false, true, true, true) == HoldStep::Keep);
    CHECK(DecideHold(before, 1000, true, true, true, true) == HoldStep::Lift);       // his pull
    CHECK(DecideHold(before, 1000 + kRaidHoldMs, false, true, true, true) == HoldStep::Lift);
    RaidHold during{1000 + kRaidHoldMs, true};
    CHECK(DecideHold(during, 1000, true, true, true, true) == HoldStep::Keep);
    CHECK(DecideHold(during, 1000, false, true, true, true) == HoldStep::Lift);      // the fight ended
    CHECK(DecideHold(before, 1000, false, true, true, false) == HoldStep::Lift);     // the switch went off
    CHECK(DecideHold(before, 1000, false, true, false, true) == HoldStep::Lift);     // it left his group
    CHECK(DecideHold(before, 1000, false, false, false, true) == HoldStep::Drop);    // logged out
}

LEEROY_TEST(RaidLead_IconsCountFromStarToSkull)
{
    CHECK_EQ(int(*RaidIconIndex("skull")), 7);
    CHECK_EQ(int(*RaidIconIndex("cross")), 6);
    CHECK_EQ(int(*RaidIconIndex("star")), 0);
    CHECK(!RaidIconIndex("Skull") && !RaidIconIndex("") && !RaidIconIndex("x"));
}

LEEROY_TEST(RaidLead_MarkingNeedsHisGroupAndAHostileTargetButNoFlag)
{
    auto f = Facts();
    CHECK_EQ(RaidLeadRefusal("mark", f), std::string_view("no_target"));
    f.targetHostile = true;
    CHECK(RaidLeadRefusal("mark", f).empty());
    f = Facts(); f.targetHostile = true; f.memberIsOwner = true; f.memberIsCast = false;
    CHECK(RaidLeadRefusal("mark", f).empty());
    f = Facts(); f.targetHostile = true; f.memberIsCast = false;
    CHECK_EQ(RaidLeadRefusal("mark", f), std::string_view("not_cast"));
    f = Facts(); f.targetHostile = true; f.memberInGroup = false;
    CHECK_EQ(RaidLeadRefusal("mark", f), std::string_view("not_in_group"));
    f = Facts(); f.targetHostile = true; f.ownerLeads = false;
    CHECK_EQ(RaidLeadRefusal("mark", f), std::string_view("not_leader"));
    f = Facts(); f.targetHostile = true; f.fenced = true;
    CHECK_EQ(RaidLeadRefusal("mark", f), std::string_view("busy:realm_owner"));
    f = Facts(); f.targetHostile = true; f.enabled = false;
    CHECK_EQ(RaidLeadRefusal("mark", f), std::string_view("raidlead_off"));
    CHECK_EQ(RaidLeadRefusal("assist", Facts()), std::string_view("bad_action"));
}

LEEROY_TEST(RaidLead_ProbeAndSyncAnswerTheSwitch)
{
    RaidLeadFacts off;
    CHECK_EQ(RaidLeadRefusal("probe", off), std::string_view("raidlead_off"));
    CHECK_EQ(RaidLeadRefusal("sync", off), std::string_view("raidlead_off"));
    off.enabled = true;
    CHECK(RaidLeadRefusal("probe", off).empty());
    CHECK_EQ(RaidLeadRefusal("sync", off), std::string_view("owner_offline"));
    off.ownerOnline = true;
    CHECK(RaidLeadRefusal("sync", off).empty());
}

LEEROY_TEST(RaidLead_CommandFieldsAreExact)
{
    std::string error;
    auto mark = ParseRaidLead(json{{"action", "mark"}, {"guid", 101u}, {"icon", "skull"}}, error);
    CHECK(mark && mark->action == "mark" && mark->guid == 101u && mark->icon == "skull" && error.empty());
    auto probe = ParseRaidLead(json{{"action", "probe"}, {"guid", 0u}, {"icon", ""}}, error);
    CHECK(probe && probe->action == "probe" && probe->guid == 0u);
    CHECK(ParseRaidLead(json{{"action", "sync"}}, error));
    CHECK_EQ(Error(json{{"action", "assist"}, {"guid", 101u}}), std::string("bad field: action"));
    CHECK_EQ(Error(json{{"guid", 101u}}), std::string("bad field: action"));
    CHECK_EQ(Error(json{{"action", "mark"}, {"icon", "skull"}}), std::string("bad field: guid"));
    CHECK_EQ(Error(json{{"action", "mark"}, {"guid", 0u}, {"icon", "skull"}}), std::string("bad field: guid"));
    CHECK_EQ(Error(json{{"action", "mark"}, {"guid", -1}, {"icon", "skull"}}), std::string("bad field: guid"));
    CHECK_EQ(Error(json{{"action", "mark"}, {"guid", 4294967296ull}, {"icon", "skull"}}), std::string("bad field: guid"));
    CHECK_EQ(Error(json{{"action", "probe"}, {"guid", 101u}}), std::string("bad field: guid"));
    CHECK_EQ(Error(json{{"action", "probe"}, {"icon", "skull"}}), std::string("bad field: icon"));
    CHECK_EQ(Error(json{{"action", "probe"}, {"icon", 7}}), std::string("bad field: icon"));
    CHECK_EQ(Error(json{{"action", "mark"}, {"guid", 101u}}), std::string("bad field: icon"));
    CHECK_EQ(Error(json{{"action", "mark"}, {"guid", 101u}, {"icon", "moonbeam"}}), std::string("bad field: icon"));
    CHECK_EQ(Error(json::array()), std::string("bad field: action"));
}
