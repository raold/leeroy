// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_RAID_LEAD_POLICY_H
#define MOD_LEEROY_RAID_LEAD_POLICY_H

// The raid lead (Leeroy.RaidLead.Enable): the owner's orders to his own group's bots and the raid icon his named
// lead sets on his target. No assistant flag is ever set. Pure decisions only; Guild/RaidLead.cpp applies them.

#include <array>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <nlohmann/json.hpp>

namespace Leeroy::Guild
{
// bot_do action "order", param one of these. The fixed list the mind's order recognition maps his words onto
// (mind/leeroy_mind/raidlead/orders.py ORDERS); "lift" is the mind's own undo of a hold, never his words.
inline constexpr std::array<std::string_view, 14> kRaidOrders = {
    "follow", "come_back", "stay", "stack", "spread", "run_out", "back_off", "hold", "attack", "kill_skull",
    "taunt", "lust", "release", "lift"
};
// A hold ends at the latest this long after it was given.
inline constexpr std::uint64_t kRaidHoldMs = 30000;

bool IsRaidOrder(std::string_view order);

// What an order does to one bot, built only from existing playerbots strategies, values and actions. No order but
// "hold" ever adds "passive", and a hold is for damage dealers only.
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
    bool lust = false;            // Bloodlust (Horde) or Heroism (Alliance) on itself
    bool hold = false;            // passive until his group's next fight starts or ends, or kRaidHoldMs
    bool toHisSide = false;       // one move to his side, nothing else changed
    float moveAway = 0;           // one "move from group" of this many yards, nothing else changed
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
    bool tankOrHealer = false; // playerbots reads its talents as a tank's or a healer's
    std::string fence;         // Realm, training, offline raid or a pending bot job ("" none)
};

// "" when the order may change this bot now, else the job's skip reason. "lift" (undoing a hold) is cleanup: it
// runs with the switch off too, and only a Realm lease fences it (the caller passes that fence alone).
std::string_view RaidOrderRefusal(std::string_view order, RaidOrderBody const& body);

// A taunt is a swap: the tank already holding the target's attention is not asked to taunt it again.
std::string_view TauntRefusal(bool hostileTarget, bool holdsAggro, bool knowsTaunt);

// Each class's taunt by playerbots' spell name; "" for a class without one.
std::string_view TauntSpell(std::uint8_t playerClass);

// The faction's Bloodlust or Heroism by playerbots' spell name.
std::string_view LustSpell(bool horde);

// "attack": a bot already on his target, or one that cannot see it yet (it closes in itself), counts as done.
bool AttackCounts(bool attacked, bool alreadyOnIt, bool inSight);

// A held bot, as RaidLead.cpp's world tick reads it.
struct RaidHold
{
    std::uint64_t until = 0;
    bool groupFought = false; // someone in his group was fighting when the hold was given
};
enum class HoldStep { Keep, Lift };
// Never dropped unlifted: a bot out of the world (between maps, or logged out: playerbots keeps its strategies) keeps
// its hold until it is back. Lift: it left his group (or he is offline), the switch is off, the time is up, or his
// group's fight started (his pull) or ended since the hold.
HoldStep DecideHold(RaidHold const& hold, std::uint64_t now, bool groupFighting, bool inWorld, bool inHisGroup,
    bool enabled);

// raid_lead {action: "mark", guid, icon}: the icon on his hostile target, set by guid (the lead he named, or him).
// {action: "probe"}: whether the switch is on (the ack). {action: "sync"}: publish his group as a group event.
// guid 0 and icon "" are no guid and no icon (the mind's encoder sends every field).
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
    bool memberInGroup = false;
    bool memberIsOwner = false;
    bool memberIsCast = false;
    bool fenced = false;        // a Realm lease holds a member of his group
    bool targetHostile = false; // "mark": his selection is a living hostile creature on his map
};

// "" when the raid_lead action may run, else the refusal code. A mark is set server-side for her, so no raid
// assistant flag is needed; nothing here touches a member flag or the leader.
std::string_view RaidLeadRefusal(std::string_view action, RaidLeadFacts const& facts);
}

#endif
