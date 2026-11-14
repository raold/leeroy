// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_MECHANIC_POLICY_H
#define MOD_LEEROY_MECHANIC_POLICY_H

#include "LeeroyHazardPolicy.h"

#include <cstddef>
#include <cstdint>
#include <functional>
#include <optional>
#include <string>
#include <string_view>
#include <vector>
#include <nlohmann/json.hpp>

namespace Leeroy::Guild
{
// The mechanic responder (Leeroy.Mechanics.Enable): when a boss starts casting a spell the mind's raid playbooks
// list for that instance (raid_mechanics), the cast bots of the owner's group and of a running offline raid it binds
// move per its kind. Only mechanics stock playerbots does not handle reach native (the mind leaves the rest out).
// Pure decisions and geometry; Guild/MechanicJob.cpp applies them.

inline constexpr std::size_t kMechanicMaxRules = 64;  // per map and difficulty
inline constexpr std::size_t kMechanicMaxSpells = 16; // per rule
inline constexpr std::size_t kMechanicMaxTables = 16; // map and difficulty pairs held at once
inline constexpr std::size_t kMechanicMaxIdBytes = 64;
inline constexpr float kMechanicMaxYards = 100.0f;
inline constexpr float kMechanicMaxDurationSeconds = 120.0f;
// A response lasts the spell's cast time plus how long its hazard lingers: the rule's duration, else the spell's own
// up to kMechanicMaxLingerMs, else kMechanicDefaultLingerMs; always within the lifetime bounds.
inline constexpr std::uint32_t kMechanicDefaultLingerMs = 3000;
inline constexpr std::uint32_t kMechanicMaxLingerMs = 120000;
inline constexpr std::uint32_t kMechanicMinLifetimeMs = 1500;
inline constexpr std::uint32_t kMechanicMaxLifetimeMs = 120000;
// A spot keeps this clear of an area's edge (beyond the bot's reach) and beyond the spacing it keeps from others,
// so that a bot that reached it is not asked to move again.
inline constexpr float kMechanicMarginYards = 1.5f;
// A gather spot lies this far inside the stack radius.
inline constexpr float kMechanicGatherSlackYards = 2.0f;
// The farthest a spot lies from the bot, and the rings searched up to it.
inline constexpr float kMechanicMaxShiftYards = 40.0f;
inline constexpr float kMechanicRingStepYards = 2.0f;
inline constexpr int kMechanicRingDirections = 16;
// A cast's destination this near its caster is no place of its own (a spell aimed at the caster's feet).
inline constexpr float kMechanicSelfYards = 0.5f;
// A stack binds only a member this near its point: one farther off fights elsewhere (a horseman's own tank).
inline constexpr float kMechanicGatherReachYards = 20.0f;

enum class MechanicKind : std::uint8_t
{
    AvoidGround,  // leave the area until it ends
    MoveOutCone,  // leave the caster's frontal cone
    Spread,       // keep the rule's distance from every other member
    Stack,        // gather within the radius of the cast's target (or destination)
    RunOutDebuff, // the debuffed member keeps the distance from every other member until it ends
    Los,          // get out of the caster's line of sight
};

enum class MechanicArea : std::uint8_t
{
    Dest,   // a circle around the cast's destination, else its target, where it was cast
    Caster, // a circle around the caster, following it (avoid_ground); the caster to hide from (los)
    Line,   // a band `radius` either side, `range` long, from the caster toward its target (or its facing)
    Cone,   // `angle` degrees wide, `range` (else `radius`) long, from the caster toward its target (or its facing)
    Group,  // spread: the other members
    Target, // run_out_debuff: the member the cast or its aura lands on
    Ring,   // a ring `range` from the cast's destination (else its target), `radius` either side: Infernal Eruption
};

inline constexpr std::uint8_t kMechanicTank = 1;
inline constexpr std::uint8_t kMechanicHealer = 2;
inline constexpr std::uint8_t kMechanicMelee = 4;
inline constexpr std::uint8_t kMechanicRanged = 8;

// One playbook mechanic at one difficulty, as the mind sends it.
struct MechanicRule
{
    std::string id;
    MechanicKind kind = MechanicKind::AvoidGround;
    MechanicArea area = MechanicArea::Dest;
    std::uint8_t roles = 0;
    std::vector<std::uint32_t> spells;
    float radius = 0, range = 0, angle = 0, duration = 0; // yards, degrees (whole cone), seconds
    bool landing = false; // the place is known only when the cast completes (a random offset): nothing binds before
};

// raid_mechanics {map, difficulty, mechanics: [...]}: the instance's whole table, replacing the last one.
struct MechanicTable
{
    std::uint32_t map = 0;
    std::uint8_t difficulty = 0; // 0 10, 1 25, 2 10 heroic, 3 25 heroic
    std::vector<MechanicRule> rules;
};

std::optional<MechanicTable> ParseRaidMechanics(nlohmann::json const& message, std::string& error);
// The rules a spell id starts, in table order.
std::vector<std::size_t> MechanicRulesFor(MechanicTable const& table, std::uint32_t spellId);
std::string_view MechanicKindName(MechanicKind kind);
// The role bit playerbots' reading of a bot's spec gives: tank, else healer, else ranged or melee.
std::uint8_t MechanicRole(bool tank, bool healer, bool ranged);

// One body the world tick looks at.
struct MechanicMemberFacts
{
    bool enabled = false;     // Leeroy.Mechanics.Enable and the module on
    bool cast = false;        // a cast member, never the owner
    bool bot = false;         // a playerbot, not a self-bot
    bool ownerGroup = false;  // in the owner's group (no battleground or battlefield group)
    bool offlineRaid = false; // held in its group by a running offline raid (no benchmark)
    bool fenced = false;      // a Realm lease, training or a pending bot job; an offline raid other than its own
    bool held = false;        // a stay or passive in its combat strategies: his order, the raid lead's or a camp hold
    bool alive = true;
    bool free = true;         // in the world, not teleporting, on no vehicle or transport
    bool covered = false;     // in a raid instance the mind sent a table for
};
// "" when it responds, else why not: off, not_cast, not_bot, not_in_group, fenced, held, dead, busy, no_table.
std::string_view MechanicSkipReason(MechanicMemberFacts const& facts);

// An offline raid's fighter is fenced by any owner but that raid itself. The raid registers its own training DC
// job over its whole group (and its training run journals what it changes on its bodies), so those never count.
struct MechanicOfflineFence
{
    bool realm = false;      // a Realm lease
    bool dummy = false;      // a dummy trial
    bool cleanup = false;    // the global training cleanup fence
    bool pendingJob = false; // a pending bot command
    std::vector<std::uint32_t> dcOwners; // the leaders whose training DC run or job holds the body
    std::uint32_t raidLeader = 0;        // the running offline raid's leader
};
bool MechanicOfflineFenced(MechanicOfflineFence const& fence);

// A stock raid strategy moved the bot with a forced move of its own since the mechanic began: the responder yields
// it for the rest of that mechanic instead of pushing it back out (never fight stock).
bool MechanicYieldsToStock(bool lastMoveForced, bool sinceMechanicBegan, bool lastMoveOurs);

// What the map thread saw when the boss started the cast.
struct MechanicCast
{
    HazardPoint caster;
    float facing = 0;                  // the caster's orientation
    std::optional<HazardPoint> target; // its unit target, the caster itself excluded
    std::optional<HazardPoint> dest;   // its destination
    std::uint32_t castMs = 0;
    std::uint32_t spellDurationMs = 0;
};

std::uint32_t MechanicLifetimeMs(MechanicRule const& rule, std::uint32_t castMs, std::uint32_t spellDurationMs);
// Whether a rule binds a member: its role, and for run_out_debuff only the debuffed member.
bool MechanicBinds(MechanicRule const& rule, std::uint8_t role, std::uint32_t guid, std::uint32_t debuffed);

enum class MechanicShapeType : std::uint8_t
{
    Circle,
    Band,
    Cone,
    Ring,
};

struct MechanicShape
{
    MechanicShapeType type = MechanicShapeType::Circle;
    HazardPoint origin;
    float radius = 0;    // circle radius; half the band's or the ring's width
    float length = 0;    // band and cone; the ring's radius
    float facing = 0;    // band and cone, radians
    float halfAngle = 0; // cone, radians
};

// Within the shape, the bot's reach counted, on its floor (kHazardHeightYards).
bool InMechanicShape(HazardPoint p, MechanicShape const& shape, float reach);
// The area avoid_ground and move_out_cone leave; nullopt for any other kind or when the cast gives no place for it.
// casterNow is where the caster is now: a caster area follows it.
std::optional<MechanicShape> MechanicLeaveArea(MechanicRule const& rule, MechanicCast const& cast,
    HazardPoint casterNow);

// What one rule asks of one bot now.
struct MechanicDuty
{
    std::optional<MechanicShape> leave;
    std::optional<HazardPoint> gather;
    float gatherRadius = 0;
    float spacing = 0;      // keep this far from every other member, plus
    std::uint8_t bodies = 0; // this many bodies' reach: a chain jumps between both (2), a burst reaches one (1)
    bool hide = false;      // out of the caster's sight
};

// The duty a bound rule gives; nullopt when it gives none (no place known, nothing to gather on).
// targetNow: where the cast's target is now (stack follows it).
std::optional<MechanicDuty> MechanicDutyFor(MechanicRule const& rule, MechanicCast const& cast, HazardPoint casterNow,
    std::optional<HazardPoint> targetNow);

// A duty binds a member where it stands: a stack only within kMechanicGatherReachYards of its point.
bool MechanicDutyReaches(MechanicDuty const& duty, HazardPoint at);

// True when the point is hidden from the caster (los duties); a refusal never asks it.
using MechanicSightFn = std::function<bool(HazardPoint)>;

// The bot at `at` keeps every duty and stands in none of the known ground hazards: it need not move. `others`: the
// other members on its map.
bool MechanicKept(HazardPoint at, std::vector<MechanicDuty> const& duties, std::vector<HazardPoint> const& others,
    std::vector<HazardZone> const& hazards, float reach, MechanicSightFn const& hidden);
// Spots that keep every duty with kMechanicMarginYards to spare and lie clear of the known ground hazards, nearest
// the bot first (spreading: away from the others' middle between equals); at most `limit`, none farther than
// kMechanicMaxShiftYards. Empty when the bot keeps them already or nothing near does.
std::vector<HazardPoint> MechanicSpots(HazardPoint at, std::vector<MechanicDuty> const& duties,
    std::vector<HazardPoint> const& others, std::vector<HazardZone> const& hazards, float reach, std::size_t limit,
    MechanicSightFn const& hidden);
// An ordinary move from `at` to `to` would take the bot into an area it leaves. Never while it stands in a known
// ground hazard (stock's own way out comes first), and never for spacing or a gather: the mechanic move serves those.
bool MechanicRefusesMove(HazardPoint at, HazardPoint to, std::vector<MechanicDuty> const& duties,
    std::vector<HazardZone> const& hazards, float reach);
}

#endif
