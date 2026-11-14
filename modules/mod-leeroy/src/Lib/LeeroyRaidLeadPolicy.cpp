// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyRaidLeadPolicy.h"
#include "LeeroyGuildCommands.h"
#include "LeeroyProtocol.h"

#include <algorithm>
#include <limits>

namespace Leeroy::Guild
{
namespace
{
// The strategy strings of playerbots' FollowChatShortcutAction and StayChatShortcutAction.
constexpr std::string_view kFollowNonCombat = "+follow,-passive,-grind,-move from group";
constexpr std::string_view kFollowCombat = "-stay,-follow,-passive,-grind,-move from group";
constexpr std::string_view kStayNonCombat = "+stay,-passive,-move from group";
constexpr std::string_view kStayCombat = "+stay,-follow,-passive,-move from group";
constexpr std::string_view kPassive = "+passive";
constexpr std::string_view kActive = "-passive";
// One "move from group" of this many yards: spread out, or run out of the group.
constexpr float kSpreadYards = 10.0f;
constexpr float kRunOutYards = 30.0f;
}

bool IsRaidOrder(std::string_view order)
{
    return std::find(kRaidOrders.begin(), kRaidOrders.end(), order) != kRaidOrders.end();
}

std::optional<RaidOrderPlan> PlanRaidOrder(std::string_view order)
{
    RaidOrderPlan plan;
    if (order == "follow" || order == "come_back")
    {
        plan.nonCombat = kFollowNonCombat;
        plan.combat = kFollowCombat;
        plan.clearPositions = true;
    }
    else if (order == "stay")
    {
        plan.nonCombat = kStayNonCombat;
        plan.combat = kStayCombat;
        plan.stayHere = true;
    }
    else if (order == "stack")
    {
        plan.nonCombat = kStayNonCombat;
        plan.combat = kStayCombat;
        plan.stayAtTarget = true;
    }
    else if (order == "spread")
        plan.moveAway = kSpreadYards;
    else if (order == "run_out")
        plan.moveAway = kRunOutYards;
    else if (order == "back_off")
        plan.toHisSide = true;
    else if (order == "hold")
    {
        plan.nonCombat = kPassive;
        plan.combat = kPassive;
        plan.hold = true;
    }
    else if (order == "lift")
    {
        plan.nonCombat = kActive;
        plan.combat = kActive;
    }
    else if (order == "attack")
    {
        plan.nonCombat = kActive;
        plan.combat = kActive;
        plan.attackHisTarget = true;
    }
    else if (order == "kill_skull")
    {
        plan.nonCombat = kActive;
        plan.combat = kActive;
        plan.rtiSkull = true;
    }
    else if (order == "taunt")
        plan.taunt = true;
    else if (order == "lust")
        plan.lust = true;
    else if (order == "release")
        plan.release = true;
    else
        return std::nullopt;
    return plan;
}

std::string_view RaidOrderRefusal(std::string_view order, RaidOrderBody const& body)
{
    auto const plan = PlanRaidOrder(order);
    if (!plan)
        return "bad_order";
    bool const lift = order == "lift";
    if (!body.enabled && !lift)
        return "raidlead_off";
    if (!body.ownerOnline)
        return "owner_offline";
    if (!body.ownerLeads && !lift)
        return "not_leader";
    if (!body.inHisGroup)
        return "not_in_group";
    if (!body.fence.empty())
        return body.fence;
    if (plan->release)
    {
        if (body.alive)
            return "alive";
        if (body.ghost)
            return "released";
        return {};
    }
    if (!body.alive && !lift)
        return "dead";
    if (plan->hold && body.tankOrHealer)
        return "not_damage"; // tanks and healers never go passive
    if (!body.sameMap && !lift)
        return "elsewhere";
    return {};
}

std::string_view TauntRefusal(bool hostileTarget, bool holdsAggro, bool knowsTaunt)
{
    if (!hostileTarget)
        return "no_target";
    if (holdsAggro)
        return "has_aggro";
    if (!knowsTaunt)
        return "no_taunt";
    return {};
}

std::string_view TauntSpell(std::uint8_t playerClass)
{
    switch (playerClass)
    {
        case 1: return "taunt";             // warrior
        case 2: return "hand of reckoning"; // paladin
        case 6: return "dark command";      // death knight
        case 11: return "growl";            // druid, in bear form
        default: return {};
    }
}

std::string_view LustSpell(bool horde)
{
    return horde ? "bloodlust" : "heroism";
}

bool AttackCounts(bool attacked, bool alreadyOnIt, bool inSight)
{
    return attacked || alreadyOnIt || !inSight;
}

HoldStep DecideHold(RaidHold const& hold, std::uint64_t now, bool groupFighting, bool inWorld, bool inHisGroup,
    bool enabled)
{
    if (!inWorld)
        return HoldStep::Keep;
    if (!inHisGroup || !enabled || now >= hold.until || groupFighting != hold.groupFought)
        return HoldStep::Lift;
    return HoldStep::Keep;
}

std::optional<RaidLeadCmd> ParseRaidLead(nlohmann::json const& message, std::string& error)
{
    error.clear();
    auto bad = [&](char const* key) -> std::optional<RaidLeadCmd>
    {
        error = std::string(Err::BadField) + key;
        return std::nullopt;
    };
    if (!message.is_object())
        return bad("action");
    RaidLeadCmd out;
    auto action = message.find("action");
    if (action == message.end() || !action->is_string())
        return bad("action");
    out.action = action->get<std::string>();
    if (out.action != "mark" && out.action != "probe" && out.action != "sync")
        return bad("action");
    bool const mark = out.action == "mark";
    auto guid = message.find("guid");
    if (guid != message.end())
    {
        if (!guid->is_number_unsigned() || guid->get<std::uint64_t>() > std::numeric_limits<std::uint32_t>::max())
            return bad("guid");
        out.guid = guid->get<std::uint32_t>();
    }
    if (mark ? out.guid == 0 : out.guid != 0)
        return bad("guid");
    auto icon = message.find("icon");
    if (icon != message.end())
    {
        if (!icon->is_string())
            return bad("icon");
        out.icon = icon->get<std::string>();
    }
    if (mark ? !RaidIconIndex(out.icon) : !out.icon.empty())
        return bad("icon");
    return out;
}

std::optional<std::uint8_t> RaidIconIndex(std::string_view icon)
{
    // kRtiIcons runs skull first; the client counts star 0 to skull 7.
    for (std::size_t i = 0; i < kRtiIcons.size(); ++i)
        if (kRtiIcons[i] == icon)
            return static_cast<std::uint8_t>(kRtiIcons.size() - 1 - i);
    return std::nullopt;
}

std::string_view RaidLeadRefusal(std::string_view action, RaidLeadFacts const& facts)
{
    if (action != "mark" && action != "probe" && action != "sync")
        return "bad_action";
    if (!facts.enabled)
        return "raidlead_off";
    if (action == "probe")
        return {};
    if (!facts.ownerOnline)
        return "owner_offline";
    if (action == "sync")
        return {};
    if (!facts.memberIsOwner && !facts.memberIsCast)
        return "not_cast";
    if (!facts.ownerLeads)
        return "not_leader";
    if (!facts.memberInGroup)
        return "not_in_group";
    if (facts.fenced)
        return "busy:realm_owner";
    if (!facts.targetHostile)
        return "no_target";
    return {};
}
}
