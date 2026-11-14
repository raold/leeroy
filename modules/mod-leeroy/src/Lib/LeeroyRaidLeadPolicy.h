// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_RAID_LEAD_POLICY_H
#define MOD_LEEROY_RAID_LEAD_POLICY_H

// The raid lead (Leeroy.RaidLead.Enable): the owner's orders to his own group's bots, the assistant flag of the member
// he names to lead, and the raid icon she sets on his target. Pure decisions only; Guild/RaidLead.cpp applies them.

#include <array>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <nlohmann/json.hpp>

namespace Leeroy::Guild
{
// bot_do action "order", param one of these. The fixed list the mind's order recognition maps his words onto
// (mind/leeroy_mind/raidlead/orders.py ORDERS); Bloodlust/Heroism stays the existing bot_do "cast".
inline constexpr std::array<std::string_view, 12> kRaidOrders = {
    "follow", "come_back", "stay", "stack", "spread", "run_out", "back_off", "hold", "attack", "kill_skull",
    "taunt", "release"
};

bool IsRaidOrder(std::string_view order);

// What an order does to one bot, built only from existing playerbots strategies and values: the strategy changes are
// the ones playerbots' own chat shortcuts make (ChatShortcutActions.cpp), without their whisper to the master.
struct RaidOrderPlan
{
    std::string_view nonCombat;   // ChangeStrategy for BOT_STATE_NON_COMBAT ("" none)
    std::string_view combat;      // ChangeStrategy for BOT_STATE_COMBAT ("" none)
    bool clearPositions = false;  // forget the "stay" and "return" positions
    bool stayHere = false;        // "stay" and "return" at the bot's own position
    bool stayAtTarget = false;    // "stay" and "return" at the owner's position, or the named member's
    bool attackHisTarget = false; // playerbots' "attack my target" on the owner's selection
    bool rtiSkull = false;        // the bot's rti value "skull"
    bool taunt = false;           // its class's taunt on the owner's hostile selection
    bool release = false;         // a dead bot releases its spirit (and runs back as playerbots does)
};

// nullopt for anything outside kRaidOrders.
std::optional<RaidOrderPlan> PlanRaidOrder(std::string_view order);

// One bot an order would change, as the world thread reads it at the mutation boundary.
struct RaidOrderBody
{
    bool enabled = false;      // Leeroy.RaidLead.Enable
    bool ownerOnline = false;
    bool ownerLeads = false;   // he leads his group (a bot never becomes its leader here)
    bool inHisGroup = false;   // the bot is in that group, which is no battleground or battlefield group
    bool sameMap = false;
    bool alive = true;
    bool ghost = false;        // released already
    std::string fence;         // Realm, training, offline raid or a pending bot job ("" none)
};

// "" when the order may change this bot now, else the job's skip reason.
std::string_view RaidOrderRefusal(std::string_view order, RaidOrderBody const& body);

// A taunt is a swap: the tank already holding the target's attention is not asked to taunt it again.
std::string_view TauntRefusal(bool hostileTarget, bool holdsAggro, bool knowsTaunt);

// Each class's taunt by playerbots' spell name; "" for a class without one.
std::string_view TauntSpell(std::uint8_t playerClass);

// raid_lead {action: "assist"|"unassist", guid[, icon: ""]} or {action: "mark", guid, icon}: guid is the cast member
// who leads (the marker for "mark", or the owner himself); icon one of LeeroyGuildCommands' kRtiIcons.
struct RaidLeadCmd
{
    std::string action;
    std::uint32_t guid = 0;
    std::string icon;
};
std::optional<RaidLeadCmd> ParseRaidLead(nlohmann::json const& message, std::string& error);

// The client's raid target icon index: star 0 ... skull 7.
std::optional<std::uint8_t> RaidIconIndex(std::string_view icon);

struct RaidLeadFacts
{
    bool enabled = false;
    bool ownerOnline = false;
    bool ownerLeads = false;
    bool raid = false;          // the assistant flag exists only in a raid group
    bool memberInGroup = false;
    bool memberIsOwner = false;
    bool memberIsCast = false;
    bool memberIsAssistant = false; // already an assistant (or, for "mark", allowed to mark)
    bool fenced = false;        // a Realm lease holds a member of his group
    bool targetHostile = false; // "mark": his selection is a living hostile creature on his map
};

// "" when the raid_lead action may run, else the refusal code. Never the owner, never anyone outside his group, and
// the leader flag is never touched: only the member's assistant flag, or an icon a raid assistant may set.
std::string_view RaidLeadRefusal(std::string_view action, RaidLeadFacts const& facts);
}

#endif
