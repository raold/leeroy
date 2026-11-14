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
// The strategy strings of playerbots' FollowChatShortcutAction, StayChatShortcutAction,
// MoveFromGroupChatShortcutAction and FleeChatShortcutAction.
constexpr std::string_view kFollowNonCombat = "+follow,-passive,-grind,-move from group";
constexpr std::string_view kFollowCombat = "-stay,-follow,-passive,-grind,-move from group";
constexpr std::string_view kStayNonCombat = "+stay,-passive,-move from group";
constexpr std::string_view kStayCombat = "+stay,-follow,-passive,-move from group";
constexpr std::string_view kMoveFromGroup = "+move from group";
constexpr std::string_view kFlee = "+follow,-stay,+passive";
constexpr std::string_view kPassive = "+passive";
constexpr std::string_view kActive = "-passive";
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
    else if (order == "spread" || order == "run_out")
    {
        plan.nonCombat = kMoveFromGroup;
        plan.combat = kMoveFromGroup;
    }
    else if (order == "back_off")
    {
        plan.nonCombat = kFlee;
        plan.combat = kFlee;
        plan.clearPositions = true;
    }
    else if (order == "hold")
    {
        plan.nonCombat = kPassive;
        plan.combat = kPassive;
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
    if (!body.enabled)
        return "raidlead_off";
    if (!body.ownerOnline)
        return "owner_offline";
    if (!body.ownerLeads)
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
    if (!body.alive)
        return "dead";
    if (!body.sameMap)
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
    if (out.action != "assist" && out.action != "unassist" && out.action != "mark")
        return bad("action");
    auto guid = message.find("guid");
    if (guid == message.end() || !guid->is_number_unsigned() || guid->get<std::uint64_t>() == 0
        || guid->get<std::uint64_t>() > std::numeric_limits<std::uint32_t>::max())
        return bad("guid");
    out.guid = guid->get<std::uint32_t>();
    auto icon = message.find("icon");
    if (out.action == "mark")
    {
        if (icon == message.end() || !icon->is_string() || !RaidIconIndex(icon->get<std::string>()))
            return bad("icon");
        out.icon = icon->get<std::string>();
    }
    else if (icon != message.end() && (!icon->is_string() || !icon->get<std::string>().empty()))
        return bad("icon"); // the mind's encoder sends every field: an empty icon is no icon
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
    if (action != "assist" && action != "unassist" && action != "mark")
        return "bad_action";
    if (!facts.enabled)
        return "raidlead_off";
    if (!facts.ownerOnline)
        return "owner_offline";
    // He may set an icon himself; only a cast member ever gets the assistant flag.
    if (facts.memberIsOwner && action != "mark")
        return "owner";
    if (!facts.memberIsOwner && !facts.memberIsCast)
        return "not_cast";
    if (!facts.ownerLeads)
        return "not_leader";
    if (!facts.memberInGroup)
        return "not_in_group";
    if (facts.fenced)
        return "busy:realm_owner";
    if (action == "mark")
    {
        // In a raid only the leader and assistants may set icons (WorldSession::HandleRaidTargetUpdateOpcode).
        if (facts.raid && !facts.memberIsAssistant)
            return "not_assistant";
        if (!facts.targetHostile)
            return "no_target";
        return {};
    }
    if (!facts.raid)
        return "not_raid";
    return {};
}
}
