// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_HAZARD_POLICY_H
#define MOD_LEEROY_HAZARD_POLICY_H

#include <array>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <map>
#include <optional>
#include <string_view>
#include <vector>

namespace Leeroy::Guild
{
// Ground hazards the owner's group keeps out of: the three kinds stock playerbots' avoid aoe knows (a hostile
// area aura's dynamic object, a damaging trap, an unselectable trigger npc with a periodic damage aura), each up
// to Playerbots.MaxAoeAvoidRadius, plus the measured larger ones a player steps out of (LargeHazardRule) and the
// measured adds whose own aura burns round them (HeldAuraHazard), which a tank holds in their fire, and the measured
// npcs that go off when a player comes near (ProximityHazardRule), which nobody holds.

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
// A waypoint nearer the bot than this is no progress (follow would not even start): skip it.
inline constexpr float kHazardDetourMinStepYards = 2.5f;
// How far a detour waypoint must lie, and how near the bot gets before the next: beyond the distance at which
// follow stops walking, which playerbots measures from the bot's edge (its reach) past FollowDistance.
float HazardDetourStep(float followDistance, float reach);
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

// Adds whose own periodic aura burns everyone near them (a selectable, attackable unit, so stock's trigger npc case
// never sees it): the allow-list by damage spell id, matched as LargeHazardRule matches. A tank holds such an add
// and stands in its fire; everyone else keeps out (HazardZone::held).
bool HeldAuraHazard(HazardSpellIds const& ids);
// An allow-listed add's zone: the damage spell's radius (up to kHazardLargeCapYards) plus the add's own reach; 0 for
// any other spell, one on the stock whitelist, or no radius.
float HazardHeldAuraRadius(HazardSpellIds const& ids, float spellRadius, float unitReach, bool whitelisted);

// Npcs that go off when a player comes near (a slime that explodes, a mine, a cloud that wakes an add, a trap):
// the allow-list by creature entry. Nobody holds one, so tanks keep out too, and the moving ones are scanned where
// they are each pass.
struct ProximityHazardRule
{
    std::uint32_t entry = 0;
    float trigger = 0;       // how near sets it off, centre to centre
    bool edges = false;      // its check counts its own reach on top (SelectNearbyTarget), not centre to centre
    std::uint32_t blast = 0; // the spell it then casts, whose radius counts when larger; 0: the trigger alone
};
std::optional<ProximityHazardRule> ProximityHazardFor(std::uint32_t entry);
// Its zone: the trigger distance (plus its own reach for an `edges` check), or the blast's radius when larger, up
// to kHazardLargeCapYards; 0 for a rule with nothing to measure.
float HazardProximityRadius(ProximityHazardRule const& rule, float blastRadius, float unitReach);

struct HazardPoint
{
    float x = 0, y = 0, z = 0;
};

struct HazardZone
{
    float x = 0, y = 0, z = 0, radius = 0;
    std::uint64_t id = 0;    // the object's guid, so a zone is known again from pass to pass
    std::uint32_t phase = 0; // the object's phase mask: a body in no shared phase never meets it (0: every phase)
    bool held = false;       // an add's own aura (HeldAuraHazard): tanks hold the add in it
    bool moving = false;     // it walks, and is led along its path (HazardLeadZone)
    // moving: where it is, its own radius before the lead, its velocity (yards a second) and how long it keeps it
    float px = 0, py = 0, core = 0, vx = 0, vy = 0, until = 0;
    bool operator==(HazardZone const&) const = default;
};

// A moving hazard is kept out of where it will be this soon as well as where it is: the zone becomes the circle
// round both (its centre half that way ahead, its radius half that much larger), toward `next`, the point its
// path makes for, at `speed` yards a second, never past that point. Unchanged when it stands still.
inline constexpr float kHazardLeadSeconds = 1.0f;
HazardZone HazardLeadZone(HazardZone zone, std::optional<HazardPoint> next, float speed);
// A walk meets a moving npc as both move: the bot at kHazardWalkSpeed from start to end, then standing
// kHazardStandAfter at the end (until its next leg), the npc on its way from a pass up to kHazardScanAge old. The
// walk crosses its zone when the two come within the npc's own radius plus the bot's reach at any moment; passing
// behind it is clear, stepping into its way is not.
inline constexpr float kHazardWalkSpeed = 7.0f;
// A bot waiting for the way to clear must be able to stand where it waits this long: no moving npc comes within
// its own radius plus the bot's reach plus kHazardMarginYards (HazardStandClear). Else it steps aside, at most
// kHazardAsideYards, to the nearest spot where it may.
inline constexpr float kHazardStandSeconds = 2.0f;
inline constexpr float kHazardAsideYards = 10.0f;
inline constexpr float kHazardStandAfter = 1.0f;
inline constexpr float kHazardScanAge = 0.5f;

// Whether a zone concerns a body in these phases: a zone in another phase never does (Halion's two realms), nor an
// add's own aura a tank (it holds the add there).
bool HazardApplies(HazardZone const& zone, std::uint32_t phaseMask, bool tank);
// The zones that concern one body, in order.
std::vector<HazardZone> HazardZonesFor(std::vector<HazardZone> const& zones, std::uint32_t phaseMask, bool tank);
// What an answer worked out from one body's share of the zones is memoised against: the snapshot's zones version
// with the phases and the tank reading that chose the share, so a body that changes realm or role asks again.
std::uint64_t HazardSelectionKey(std::uint64_t zonesVersion, std::uint32_t phaseMask, bool tank);

// Within the zone's radius plus the bot's reach, on the zone's floor.
bool InHazard(HazardPoint p, HazardZone const& zone, float reach);
std::optional<std::size_t> HazardAt(HazardPoint p, std::vector<HazardZone> const& zones, float reach);
// kHazardMarginYards clear of every zone's edge (beyond the reach).
bool HazardClear(HazardPoint p, std::vector<HazardZone> const& zones, float reach);
// HazardClear, and no moving npc comes that near within kHazardStandSeconds: a spot to wait on.
bool HazardStandClear(HazardPoint p, std::vector<HazardZone> const& zones, float reach);
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
// every zone, reached by a clear straight line, at least `minStep` from the bot (no stalling on its own spot)
// and as far round as kHazardDetourMaxDegrees allows. Empty when the line is clear, the bot stands in a zone
// (stepping out comes first), or neither way round makes progress: then it walks straight, as stock.
std::vector<HazardPoint> HazardDetour(HazardPoint from, HazardPoint goal, std::vector<HazardZone> const& zones,
    float reach, float minStep = kHazardDetourMinStepYards);

// Detours toward one follow goal (it counts as the same until it moves kHazardDetourGoalMoveYards). A new
// waypoint that does not bring the bot kHazardDetourGainYards nearer the goal than its best is an idle leg; past
// kHazardDetourIdleLegs it gives up and walks straight, as stock (a pocket between hazards has no way round, and
// greedy steps would circle in it).
inline constexpr float kHazardDetourGoalMoveYards = 3.0f;
inline constexpr float kHazardDetourGainYards = 1.0f;
inline constexpr std::uint32_t kHazardDetourIdleLegs = 6;
struct HazardDetourTrack
{
    HazardPoint goal;
    float best = 0;
    std::uint32_t idleLegs = 0;
    bool started = false;
    bool givenUp = false;
};
// Called before taking a new waypoint; false once detours toward this goal stopped making progress.
bool HazardDetourAllowed(HazardDetourTrack& track, HazardPoint at, HazardPoint goal);

// Follow commits a bot to the leg it sets out on (playerbots plans again only once that leg's walk time, up to
// MaxWaitForMove, has passed), so near a moving hazard a long leg is walked blind into a slime's path. While a
// moving zone lies within kHazardMovingNearYards of the bot, follow walks legs of at most kHazardMovingLegYards
// (about half a second at run speed) and so plans again from each fresh pass, and where a moving npc shuts the
// way it waits for it to pass, where it may stand (HazardStandClear) or a step aside.
inline constexpr float kHazardMovingNearYards = 20.0f;
inline constexpr float kHazardMovingLegYards = 4.0f;
// The first zone across the straight walk from `from` to `goal` moves: it passes, so no pocket is given up on
// (HazardDetourAllowed is for zones that stay) and with no way round the bot waits.
bool HazardFirstCrossingMoves(HazardPoint from, HazardPoint goal, std::vector<HazardZone> const& zones, float reach);
// Where follow walks next near moving zones: a leg toward `waypoint` (a detour) or else `goal`, cut to
// kHazardMovingLegYards but never shorter than `minLeg` (HazardDetourStep: a nearer spot would stop follow), when
// the walk is clear; else the bot's own spot to wait, if HazardStandClear, or the nearest such spot to step aside
// to. nullopt when no moving zone is near, when the bot stands in a zone (the exit's job), when a zone that stays
// shuts the way (follow as before), or when no spot to wait on is in reach. With
// a reachability filter, candidates must pass it; failure holds the current spot
// instead of returning an unchecked follow goal.
std::optional<HazardPoint> HazardMovingLeg(HazardPoint at, HazardPoint goal, std::optional<HazardPoint> waypoint,
    std::vector<HazardZone> const& zones, float reach, float minLeg,
    std::function<bool(HazardPoint const&)> const& reachable = {});

// One bot's recent exits, counted per hazard, so an exit never loops with another mover (follow, dungeon-clear's
// route) that walks it back into the same hazards, overlapping ones included.
struct HazardExitCount
{
    std::uint32_t exits = 0;
    std::uint64_t since = 0;
    std::uint64_t yieldUntil = 0;
};
struct HazardExitGuard
{
    std::map<std::uint64_t, HazardExitCount> zones;
    std::vector<std::uint64_t> standing; // the zones the bot stood in at its last answer (none once it stood clear)
};
// Every zone the bot stands in is yielded: its own movers take it through.
bool HazardExitYielded(HazardExitGuard const& guard, std::vector<std::uint64_t> const& zones, std::uint64_t now);
enum class HazardEntry : std::uint8_t
{
    Exit,        // step out
    Yielded,     // every zone it stands in is yielded already
    YieldStarts, // this entry was the kHazardExitsPerWindow-th into the last zone not yet yielded
};
// Notes a fresh entry into the zones the bot stands in, each counted on its own; a zone's
// kHazardExitsPerWindow-th entry in the window yields it for kHazardYieldMs.
HazardEntry NoteHazardEntry(HazardExitGuard& guard, std::vector<std::uint64_t> const& zones, std::uint64_t now);
// The exit's answer for a bot standing in `inside` now (the caller clears guard.standing once it stands in none):
// an entry counts for each zone it did not stand in at its last answer, so an exit planned again inside the same
// zones counts nothing, and a bot walked from one overlapping zone into the other counts each. A zone that moves
// walked onto the bot (no mover walked the bot back into it): never counted, never yielded, and Exit whenever the
// bot stands in one. Otherwise Yielded / YieldStarts once every zone it stands in is yielded, as NoteHazardEntry.
HazardEntry NoteHazardZoneEntry(HazardExitGuard& guard, std::vector<HazardZone> const& inside, std::uint64_t now);

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
