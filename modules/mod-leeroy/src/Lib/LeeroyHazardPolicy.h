// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_HAZARD_POLICY_H
#define MOD_LEEROY_HAZARD_POLICY_H

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
// A follow spot or an exit keeps this clear of a hazard's edge, beyond the bot's own reach.
inline constexpr float kHazardMarginYards = 1.5f;
// The farthest a follow spot or an exit moves: out of the middle of the largest hazard.
inline constexpr float kHazardMaxShiftYards = 40.0f;
inline constexpr float kHazardRingStepYards = 2.0f;
inline constexpr int kHazardRingDirections = 16;

enum class LargeHazardRadius : std::uint8_t
{
    Spell,       // the damage effect's radius
    CasterReach, // the trigger npc's combat reach, which its script grows and hits within
};

// The allow-list of larger escapable hazards by damage spell id (a trigger npc's triggered spell), with where
// their radius comes from; nullopt for every other spell.
std::optional<LargeHazardRadius> LargeHazardRule(std::uint32_t spellId);

// The radius a hazard is kept out of; 0 when it is not avoided: on the stock whitelist, no radius, or a radius
// above the stock cap that is not an allow-listed hazard within kHazardLargeCapYards.
float HazardAvoidRadius(std::uint32_t spellId, float spellRadius, float casterReach, float stockCap,
    bool whitelisted);

struct HazardZone
{
    float x = 0, y = 0, z = 0, radius = 0;
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

// Spots near p that are HazardClear, nearest p first and, between equals, nearest `toward`; at most `limit`,
// none farther than kHazardMaxShiftYards. Empty when p is already clear or nothing near is.
std::vector<HazardPoint> HazardSafeSpots(HazardPoint p, HazardPoint toward, std::vector<HazardZone> const& zones,
    float reach, std::size_t limit);

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
