// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_HAZARD_POLICY_H
#define MOD_LEEROY_HAZARD_POLICY_H

#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <string_view>
#include <vector>

namespace Leeroy::Guild
{
// Ground hazards the owner's group keeps out of: the three kinds stock playerbots' avoid aoe knows (a hostile
// area aura's dynamic object, a damaging trap, an unselectable trigger npc with a periodic damage aura), each up
// to Playerbots.MaxAoeAvoidRadius, plus the measured larger ones a player steps out of (LargeHazardRule).

// No hazard counts above this: a bigger one covers the room, and nobody steps out of it.
inline constexpr float kHazardLargeCapYards = 35.0f;
// A hazard more than this far above or below a spot lies on another floor.
inline constexpr float kHazardHeightYards = 8.0f;
// A follow spot, an exit or a detour keeps this clear of a hazard's edge, beyond the bot's own reach.
inline constexpr float kHazardMarginYards = 1.5f;
// The farthest a follow spot or an exit moves: out of the middle of the largest hazard.
inline constexpr float kHazardMaxShiftYards = 40.0f;
inline constexpr float kHazardRingStepYards = 2.0f;
inline constexpr int kHazardRingDirections = 16;
// An exit from which the follow spot is in plain view beats a nearer one unless that saves more than this.
inline constexpr float kHazardSideSlackYards = 4.0f;
// A detour waypoint sits this far beyond the clearance, at most this far round the hazard per step.
inline constexpr float kHazardDetourOutsetYards = 1.0f;
inline constexpr float kHazardDetourMaxDegrees = 90.0f;
inline constexpr float kHazardDetourStepDegrees = 5.0f;
// Stepped out of the same hazard this many times within the window: another mover wants the bot through it.
inline constexpr std::uint32_t kHazardExitsPerWindow = 3;
inline constexpr std::uint64_t kHazardExitWindowMs = 20000;
inline constexpr std::uint64_t kHazardYieldMs = 20000;

enum class LargeHazardRadius : std::uint8_t
{
    Spell,       // the damage effect's radius
    CasterReach, // the trigger npc's combat reach, which its script grows and hits within
};

// The allow-list of larger escapable hazards by damage spell id (a trigger npc's triggered spell), with where
// their radius comes from; nullopt for every other spell.
std::optional<LargeHazardRadius> LargeHazardRule(std::uint32_t spellId);

// The ids a hazard is known by: the damage spell itself, its first (10-man or normal) difficulty version, and
// for a trigger npc the spell the first difficulty version of its aura triggers; 0 where there is none. The
// allow-list matches any of them, so 25-man and heroic versions follow their 10-man entry.
using HazardSpellIds = std::array<std::uint32_t, 3>;
std::optional<LargeHazardRadius> LargeHazardRule(HazardSpellIds const& ids);

// The radius a hazard is kept out of; 0 when it is not avoided: on the stock whitelist, no radius, or a radius
// above the stock cap that is not an allow-listed hazard within kHazardLargeCapYards.
float HazardAvoidRadius(HazardSpellIds const& ids, float spellRadius, float casterReach, float stockCap,
    bool whitelisted);
// A trigger npc's zone: as stock, its own reach counts on top of the spell's radius, unless that radius is the
// reach already (CasterReach).
float HazardTriggerZoneRadius(HazardSpellIds const& ids, float spellRadius, float unitReach, float stockCap,
    bool whitelisted);

struct HazardZone
{
    float x = 0, y = 0, z = 0, radius = 0;
    std::uint64_t id = 0; // the object's guid, so a zone is known again from pass to pass
    bool operator==(HazardZone const&) const = default;
};

struct HazardPoint
{
    float x = 0, y = 0, z = 0;
};

// Within the zone's radius plus the bot's reach, on the zone's floor.
bool InHazard(HazardPoint p, HazardZone const& zone, float reach);
std::optional<std::size_t> HazardAt(HazardPoint p, std::vector<HazardZone> const& zones, float reach);
// kHazardMarginYards clear of every zone's edge (beyond the reach).
bool HazardClear(HazardPoint p, std::vector<HazardZone> const& zones, float reach);
// The straight walk from a to b enters no zone (no point within radius plus reach).
bool HazardSegmentClear(HazardPoint a, HazardPoint b, std::vector<HazardZone> const& zones, float reach);

// Spots near p that are HazardClear, nearest p first and, between equals, nearest `toward`; at most `limit`,
// none farther than kHazardMaxShiftYards. Empty when p is already clear or nothing near is.
std::vector<HazardPoint> HazardSafeSpots(HazardPoint p, HazardPoint toward, std::vector<HazardZone> const& zones,
    float reach, std::size_t limit);

// Exits for a bot standing in a zone. With a follow goal, an exit from which the goal is in plain view (on his
// side) beats a nearer one unless the nearer saves more than kHazardSideSlackYards; ties go toward `toward`.
std::vector<HazardPoint> HazardExitSpots(HazardPoint at, std::optional<HazardPoint> goal, HazardPoint toward,
    std::vector<HazardZone> const& zones, float reach, std::size_t limit);

// Where to walk first when the straight line from a bot outside every zone to its clear goal crosses one: a
// waypoint round the first zone in the way, the shorter way round first, then the other way. Each is outside
// every zone, reached by a clear straight line, and as far round as kHazardDetourMaxDegrees allows. Empty when
// the line is clear, the bot stands in a zone (stepping out comes first), or neither way round is clear.
std::vector<HazardPoint> HazardDetour(HazardPoint from, HazardPoint goal, std::vector<HazardZone> const& zones,
    float reach);

// One bot's recent exits, so an exit never loops with another mover (follow, dungeon-clear's route) that walks
// it back into the same hazard.
struct HazardExitGuard
{
    std::uint64_t zone = 0;
    std::uint32_t exits = 0;
    std::uint64_t since = 0;
    std::uint64_t yieldUntil = 0;
};
// While yielded the bot's own movers take it through that zone.
bool HazardExitYielded(HazardExitGuard const& guard, std::uint64_t zone, std::uint64_t now);
// Notes a fresh exit from `zone`; true when this one is the kHazardExitsPerWindow-th in the window: no exit
// then, and the zone is yielded for kHazardYieldMs.
bool NoteHazardExit(HazardExitGuard& guard, std::uint64_t zone, std::uint64_t now);

// A body the tick looks at: one of the owner's group, or one whose non-combat avoid aoe it added.
struct HazardMemberFacts
{
    bool enabled = false;     // Leeroy.Hazards.Enable and the module on
    bool cast = false;        // a cast member, never the owner
    bool bot = false;         // a playerbot, not a self-bot
    bool ownerGroup = false;  // in the owner's group (no battleground or battlefield group)
    bool ownerMaster = false; // the owner is its master
    bool fenced = false;      // a Realm lease, offline raid, training or pending bot command holds it
};

// What the tick knows of the strategy on that body.
struct HazardHold
{
    bool journaled = false; // the tick added non-combat avoid aoe to this body
    bool present = false;   // non-combat avoid aoe is there now
    bool sameLife = true;   // the journaled body, AI and session are the ones online
};

enum class HazardStep : std::uint8_t
{
    None,   // leave it as it is
    Add,    // add non-combat avoid aoe and journal it
    Remove, // remove the one the tick added and forget it
    Forget, // a reset or a new life dropped it already: forget it
};

bool HazardEligible(HazardMemberFacts const& facts);
// Eligible and not fenced: its follow spots and exits use the zones.
bool HazardAware(HazardMemberFacts const& facts);
// A fenced body is left to its owner; a strategy the tick did not add is never removed.
HazardStep DecideHazardStrategy(HazardMemberFacts const& facts, HazardHold const& hold);
// Why an addition was removed, for the log: off, not_cast, not_bot, not_in_group or master.
std::string_view HazardOffReason(HazardMemberFacts const& facts);
}
#endif
