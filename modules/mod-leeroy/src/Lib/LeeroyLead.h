// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_LEAD_H
#define MOD_LEEROY_LEAD_H

#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <nlohmann/json.hpp>

namespace Leeroy::Guild
{
// The owner says "you lead" in the open world (the mind's [talk] lead, mind/leeroy_mind/lead): one cast member of his
// group leads it and he follows her himself. Native makes her the group's movement leader: she masters herself (a
// self-mastered bot, as an offline raid's driver is, so playerbots keeps her as the others' master) and runs
// playerbots' own strategies for the activity (new rpg's questing day, grind, or new rpg's walk to a camp for travel);
// the other cast members of his group take her as their playerbots master, so their follow and formation follow her.
// Everything it changed is journaled per body and given back when the lead ends. Leeroy.Lead.Enable answers for it.
inline constexpr std::string_view kLeadDoType = "lead_do";
// The job event a lead publishes when it ends by itself (or by his end): kind "lead", state "ended", reason its why.
inline constexpr std::string_view kLeadJobKind = "lead";

enum class LeadAction : std::uint8_t { Start, End };
// quest: new rpg's own day (quests, camps, grinding, wandering; never a flight or outdoor PvP) with grind; grind:
// grind with new rpg kept to its grinding statuses; travel: new rpg's walk to a meeting spot (LeeroyInviteSpots.h).
enum class LeadActivity : std::uint8_t { Quest, Grind, Travel };
std::optional<LeadActivity> ParseLeadActivity(std::string_view name);
std::string_view LeadActivityName(LeadActivity activity);

// She leads for this long at most.
inline constexpr std::uint64_t kLeadMs = 3600000;
// The wait-for-him rule: she stops where she is once he is farther than kLeadWaitYards (or on another map), and goes on
// once he is back within kLeadResumeYards; after kLeadBehindMs of waiting the lead ends.
inline constexpr float kLeadWaitYards = 40.0f;
inline constexpr float kLeadResumeYards = 20.0f;
inline constexpr std::uint64_t kLeadBehindMs = 600000;
// She can take the lead only this close to him, on his map.
inline constexpr float kLeadStartYards = 100.0f;
// Travel: there within kLeadArriveYards of the spot; no kLeadProgressYards closer for kLeadStuckMs (her own walking,
// waits and fights not counted) means no way there.
inline constexpr float kLeadArriveYards = 15.0f;
inline constexpr float kLeadProgressYards = 5.0f;
inline constexpr std::uint64_t kLeadStuckMs = 120000;
// A move of hers farther than this between two ticks on one map is a teleport, never her walking.
inline constexpr float kLeadJumpYards = 250.0f;

// {action: "start", guid, activity, spot (travel only: a kInviteSpots name)} or {action: "end", guid (0: whoever
// leads)}. Fields only: the bridge checked v, id and type.
struct LeadDoCmd
{
    LeadAction action = LeadAction::Start;
    std::uint32_t guid = 0;
    LeadActivity activity = LeadActivity::Quest;
    std::string spot;
};
std::optional<LeadDoCmd> ParseLeadDo(nlohmann::json const& message, std::string& error);

// What the world thread saw when a start arrived.
struct LeadFacts
{
    bool enabled = false;          // Leeroy.Enable and Leeroy.Lead.Enable
    bool supported = true;         // playerbots has the activity's strategies
    // him
    bool humanOnline = false, humanAlive = false;
    bool humanCombat = false;      // his fight: him, or anyone of his group on his map, in combat
    bool humanTravelling = false;  // teleporting, on a taxi, a transport or a vehicle
    bool humanInstance = false, humanBattleground = false;
    // her
    bool cast = false;             // a pinned cast member of his guild, run by playerbots on a random-bot account
    bool online = false;
    std::string fence;             // the first ownership fence (a member of his group: every one but his group's own)
    bool dead = false, combat = false, travelling = false;
    bool battleground = false, queued = false, instance = false;
    // the two of them
    bool hisGroup = false;         // she is in the group he is in
    bool leader = false;           // he leads that group
    bool groupSpecial = false;     // his group is a Dungeon Finder or battleground group
    bool sameMap = false;
    float distance = 0.0f;         // yards, on the same map
    // travel
    bool spotValid = false;        // the spot's point lies on its map, in its zone
    bool spotSameMap = false;      // on her map
    float spotDistance = 0.0f;     // her yards from it, on her map
};

// Empty when she may take the lead now, else its one refusal code: off, human_offline, not_cast, not_online, the
// fence, dead, combat (hers or his fight), in_instance (hers or his: inside an instance dungeon-clear leads), in_bg,
// queued, travelling, human_dead, not_in_group, not_leader, lfg_group, too_far (beyond kLeadStartYards or another
// map), unsupported; travel adds bad_spot, other_map (the spot is not on her map) and here (she stands at it).
std::string LeadRefusal(LeadActivity activity, LeadFacts const& facts);

// What a lead keeps between ticks.
struct LeadState
{
    LeadActivity activity = LeadActivity::Quest;
    std::uint64_t started = 0;
    std::uint64_t until = 0;
    bool paused = false;           // waiting for him: her activity's strategies taken off
    std::uint64_t behindSince = 0; // when the wait began (0: not waiting)
    float best = 0.0f;             // travel: her closest yards to the spot so far
    std::uint64_t progressAt = 0;  // travel: when she last came kLeadProgressYards closer (or went on again)
};

// The world tick's view of the lead.
struct LeadTickFacts
{
    std::uint64_t now = 0;
    bool on = false;               // Leeroy.Enable and Leeroy.Lead.Enable
    bool leaderHere = false;       // the same body, AI and session that took the lead, connected
    bool humanOnline = false;
    bool dead = false;             // hers
    bool instance = false;         // she or he is on an instance map (dungeon-clear's, never ours)
    bool battleground = false;     // she or he is in a battleground or arena
    bool grouped = false;          // she is still in his group and he leads it
    bool selfMastered = false;     // her playerbots master is still herself
    bool activityOn = false;       // her activity's strategy is still in force (asked only while not paused)
    bool fenced = false;           // another owner holds her (a Realm lease, training, an offline raid, a bot job)
    bool teleported = false;       // she is being teleported, or moved farther than kLeadJumpYards since the last tick
    bool fighting = false;         // she, or anyone of his group on her map, is in combat
    bool sameMap = false;          // he is on her map and not being teleported
    float distance = 0.0f;         // his yards from her, on her map
    float spotDistance = -1.0f;    // travel: her yards from the spot on its map (negative: not on that map)
};

enum class LeadStep : std::uint8_t { Keep, Pause, Resume, End };
struct LeadDecision
{
    LeadStep step = LeadStep::Keep;
    std::string_view why;          // End's reason
};
// The end's reasons, first that holds: gone (another life, or she logged out), off, logout (his), dead, instance, pvp,
// group (she left his group, or he no longer leads it), taken (her master is someone else now), reset (playerbots
// reset her strategies), fenced, teleported, timeout, arrived (travel), no_path (travel), left (he stayed behind
// kLeadBehindMs). In a fight she neither stops nor goes on.
LeadDecision DecideLead(LeadState const& state, LeadTickFacts const& facts);
// The tick's note before it decides: travel's progress (her waits and fights restart its clock).
void NoteLead(LeadState& state, LeadTickFacts const& facts);
// A Pause or Resume the tick carried out: the wait begins (its clock), or ends (travel's clock restarts).
void ApplyLead(LeadState& state, LeadStep step, std::uint64_t now);

// new rpg's statuses (playerbots' NewRpgStatus, mirrored here and checked against it where it is used).
enum class LeadRpg : int
{
    Idle = 0, GoGrind = 1, GoCamp = 2, WanderRandom = 3, WanderNpc = 4, DoQuest = 5, TravelFlight = 6, Rest = 7,
    OutdoorPvp = 8, DoGather = 9
};
// The status to put her in instead of the one she is in, or none to leave it. quest: never a flight (she would leave
// him behind) or outdoor PvP, wander nearby instead; grind: only idle, going to grind, wandering and resting; travel:
// always going to the camp (the spot).
std::optional<LeadRpg> LeadSteer(LeadActivity activity, int status);
}

#endif
