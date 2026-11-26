// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyHazardPolicy.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <limits>
#include <tuple>
#include <utility>

namespace Leeroy::Guild
{
namespace
{
struct LargeHazard
{
    std::uint32_t spell;
    LargeHazardRadius radius;
};

// Measured from Spell.dbc and the core's raid scripts (Naxxramas, Ulduar, Trial of the Crusader, Icecrown
// Citadel, Obsidian Sanctum, Eye of Eternity, Vault of Archavon): the ground hazards between 15 and 35 yards
// that a player walks out of. The others in that band are boss-centred, a vehicle phase, a jet line or a
// sweeping orb (see the README).
constexpr std::array<LargeHazard, 6> kLargeHazards{{
    {28241, LargeHazardRadius::CasterReach}, // Poison, Grobbulus's cloud: hits within the cloud's growing reach
    {54363, LargeHazardRadius::CasterReach}, // its 25-man version (aura 54362, the difficulty twin of 28158)
    {69789, LargeHazardRadius::Spell},       // Ooze Flood, Rotface's flooded corner (25 yards)
    {71215, LargeHazardRadius::Spell},       // its 25-man and heroic versions
    {71587, LargeHazardRadius::Spell},
    {71588, LargeHazardRadius::Spell},
}};

// Measured from Spell.dbc and the core's scripts (Ruby Sanctum): adds that carry a burning aura and are tanked in
// the raid's way. Living Inferno (40681, heroic 25-man Halion): Blazing Aura 75885 triggers 75886 every 2 s, 75887
// on 25-man, 15 yards round the inferno.
constexpr std::array<std::uint32_t, 2> kHeldAuraHazards{{75886, 75887}};

// Measured from the core's scripts, Spell.dbc/SpellRadius.dbc with the core's spell corrections and the world
// database (2026-11-21): npcs that go off when a player comes near (see the README).
constexpr std::array<ProximityHazardRule, 4> kProximityHazards{{
    // Naxxramas, Living Poison: walks across the Construct Quarter's hall in three lanes, one every 5 s in each,
    // and casts Explode on itself (28433: 5 yards) at a player within 1.5 yards (SelectNearestTarget, centre to
    // centre). Its blast does not count: 5 yards round slimes 12.5 yards apart would shut each lane, and a bot
    // that never sets one off is never caught in its own.
    {16027, 1.5f, false, 0},
    // Ulduar, Mimiron's Proximity Mine: explodes (66351, 63009 on 25-man: 3 yards) at a player within 1.9 yards.
    {34362, 1.9f, false, 66351},
    // Ulduar, Yogg-Saron's Ominous Cloud: drifts round the room and wakes a Guardian of Yogg-Saron (63031) for a
    // player within 6 yards of its edge (SelectNearbyTarget).
    {33292, 6.0f, true, 0},
    // Icecrown Citadel, the Lich King's Shadow Trap: its search (74282, 3 yards by the core's correction) sets off
    // 73529, 10 yards of damage and a knockback.
    {39137, 3.0f, false, 73529},
}};

float constexpr kPi = 3.14159265358979f;

float Distance2d(HazardPoint a, float x, float y)
{
    return std::hypot(a.x - x, a.y - y);
}

bool SameFloor(HazardPoint p, HazardZone const& zone)
{
    return std::fabs(p.z - zone.z) <= kHazardHeightYards;
}

// Where along a -> b the zone's centre projects (0..1), and how far the segment passes from it.
std::pair<float, float> Closest(HazardPoint a, HazardPoint b, HazardZone const& zone)
{
    float const dx = b.x - a.x;
    float const dy = b.y - a.y;
    float const length2 = dx * dx + dy * dy;
    float t = length2 > 0.0001f ? ((zone.x - a.x) * dx + (zone.y - a.y) * dy) / length2 : 0.0f;
    t = std::clamp(t, 0.0f, 1.0f);
    return {t, std::hypot(a.x + t * dx - zone.x, a.y + t * dy - zone.y)};
}

// How near a bot walking from a to b (then standing there) comes to a moving npc (HazardZone's motion fields).
float Approach(HazardPoint a, HazardPoint b, HazardZone const& zone, float stand = kHazardStandAfter)
{
    float const walk = Distance2d(a, b.x, b.y) / kHazardWalkSpeed;
    float const moves = std::max(0.0f, zone.until - kHazardScanAge); // it moved for up to a pass already
    float const lag = std::min(zone.until, kHazardScanAge);
    float const nx = zone.px + zone.vx * lag;
    float const ny = zone.py + zone.vy * lag;
    // Both motions are straight between these moments: closest approach on each piece.
    std::array<float, 4> cuts{0.0f, std::min(walk, moves), std::max(walk, moves), walk + stand};
    std::sort(cuts.begin(), cuts.end());
    auto botAt = [&](float t)
    {
        float const f = walk > 0.0001f ? std::min(t, walk) / walk : 1.0f;
        return std::pair<float, float>{a.x + (b.x - a.x) * f, a.y + (b.y - a.y) * f};
    };
    auto npcAt = [&](float t)
    {
        float const m = std::min(t, moves);
        return std::pair<float, float>{nx + zone.vx * m, ny + zone.vy * m};
    };
    float best = std::numeric_limits<float>::max();
    for (std::size_t i = 0; i + 1 < cuts.size(); ++i)
    {
        float const t0 = cuts[i];
        float const t1 = std::min(cuts[i + 1], walk + stand);
        if (t1 < t0)
            continue;
        auto const [bx0, by0] = botAt(t0);
        auto const [bx1, by1] = botAt(t1);
        auto const [nx0, ny0] = npcAt(t0);
        auto const [nx1, ny1] = npcAt(t1);
        float const rx = bx0 - nx0;
        float const ry = by0 - ny0;
        float const dx = (bx1 - nx1) - rx;
        float const dy = (by1 - ny1) - ry;
        float const length2 = dx * dx + dy * dy;
        float const s = length2 > 0.0000001f ? std::clamp(-(rx * dx + ry * dy) / length2, 0.0f, 1.0f) : 0.0f;
        best = std::min(best, std::hypot(rx + dx * s, ry + dy * s));
    }
    return best;
}

bool Crosses(HazardPoint a, HazardPoint b, HazardZone const& zone, float reach, float stand = kHazardStandAfter)
{
    if (!SameFloor(a, zone) && !SameFloor(b, zone))
        return false;
    // Reject distant zones before the one-yard leg search repeats projection and
    // closest-approach calculations. Include the NPC's whole remaining motion,
    // not just its current centre: a distant fast mover can still cross the leg.
    auto const overlaps = [&](float x0, float y0, float x1, float y1, float radius)
    {
        return std::max(a.x, b.x) >= std::min(x0, x1) - radius
            && std::min(a.x, b.x) <= std::max(x0, x1) + radius
            && std::max(a.y, b.y) >= std::min(y0, y1) - radius
            && std::min(a.y, b.y) <= std::max(y0, y1) + radius;
    };
    if (!overlaps(zone.x, zone.y, zone.x, zone.y, zone.radius + reach)
        && (!zone.moving || !overlaps(zone.px, zone.py, zone.px + zone.vx * zone.until,
            zone.py + zone.vy * zone.until, zone.core + reach)))
        return false;
    if (Closest(a, b, zone).second <= zone.radius + reach)
        return true;
    return zone.moving && Approach(a, b, zone, stand) <= zone.core + reach;
}

// The walk alone (no standing at its end) meets no zone.
bool WalkClear(HazardPoint a, HazardPoint b, std::vector<HazardZone> const& zones, float reach)
{
    return std::none_of(zones.begin(), zones.end(), [&](HazardZone const& zone) { return Crosses(a, b, zone, reach, 0.0f); });
}

// Every clear spot near p with its shift, nearest first, then nearest `toward`.
std::vector<std::pair<float, HazardPoint>> ClearCandidates(HazardPoint p, HazardPoint toward,
    std::vector<HazardZone> const& zones, float reach)
{
    std::vector<HazardPoint> candidates;
    // Straight out of each zone p is in, the shortest way: usually the answer.
    for (auto const& zone : zones)
    {
        float const clearance = zone.radius + reach + kHazardMarginYards;
        if (!SameFloor(p, zone) || Distance2d(p, zone.x, zone.y) >= clearance)
            continue;
        float dx = p.x - zone.x;
        float dy = p.y - zone.y;
        float length = std::hypot(dx, dy);
        if (length < 0.01f) // dead centre: out on the side of `toward`
        {
            dx = toward.x - zone.x;
            dy = toward.y - zone.y;
            length = std::hypot(dx, dy);
            if (length < 0.01f)
            {
                dx = 1.0f;
                dy = 0.0f;
                length = 1.0f;
            }
        }
        float const out = clearance + 0.1f;
        candidates.push_back({zone.x + dx / length * out, zone.y + dy / length * out, p.z});
    }
    // Overlapping zones: rings around p.
    for (float ring = kHazardRingStepYards; ring <= kHazardMaxShiftYards + 0.001f; ring += kHazardRingStepYards)
        for (int k = 0; k < kHazardRingDirections; ++k)
        {
            float const angle = 2.0f * kPi * static_cast<float>(k) / static_cast<float>(kHazardRingDirections);
            candidates.push_back({p.x + ring * std::cos(angle), p.y + ring * std::sin(angle), p.z});
        }

    std::vector<std::tuple<float, float, HazardPoint>> clear;
    for (auto const& c : candidates)
    {
        float const shift = Distance2d(p, c.x, c.y);
        if (shift <= kHazardMaxShiftYards + 0.001f && HazardClear(c, zones, reach))
            clear.emplace_back(shift, Distance2d(toward, c.x, c.y), c);
    }
    std::stable_sort(clear.begin(), clear.end(), [](auto const& a, auto const& b)
        { return std::tie(std::get<0>(a), std::get<1>(a)) < std::tie(std::get<0>(b), std::get<1>(b)); });
    std::vector<std::pair<float, HazardPoint>> sorted;
    sorted.reserve(clear.size());
    for (auto const& entry : clear)
        sorted.emplace_back(std::get<0>(entry), std::get<2>(entry));
    return sorted;
}
}

std::optional<LargeHazardRadius> LargeHazardRule(std::uint32_t spellId)
{
    for (auto const& hazard : kLargeHazards)
        if (spellId && hazard.spell == spellId)
            return hazard.radius;
    return std::nullopt;
}

std::optional<LargeHazardRadius> LargeHazardRule(HazardSpellIds const& ids)
{
    for (std::uint32_t id : ids)
        if (auto const rule = LargeHazardRule(id))
            return rule;
    return std::nullopt;
}

float HazardAvoidRadius(HazardSpellIds const& ids, float spellRadius, float casterReach, float stockCap,
    bool whitelisted)
{
    if (whitelisted)
        return 0.0f;
    if (auto const rule = LargeHazardRule(ids))
    {
        float const radius = *rule == LargeHazardRadius::CasterReach ? casterReach : spellRadius;
        return radius > 0.0f && radius <= kHazardLargeCapYards ? radius : 0.0f;
    }
    return spellRadius > 0.0f && spellRadius <= stockCap ? spellRadius : 0.0f;
}

float HazardTriggerZoneRadius(HazardSpellIds const& ids, float spellRadius, float unitReach, float stockCap,
    bool whitelisted)
{
    float const radius = HazardAvoidRadius(ids, spellRadius, unitReach, stockCap, whitelisted);
    if (radius <= 0.0f || LargeHazardRule(ids) == LargeHazardRadius::CasterReach)
        return radius;
    return radius + unitReach;
}

bool HeldAuraHazard(HazardSpellIds const& ids)
{
    for (std::uint32_t id : ids)
        if (id && std::find(kHeldAuraHazards.begin(), kHeldAuraHazards.end(), id) != kHeldAuraHazards.end())
            return true;
    return false;
}

float HazardHeldAuraRadius(HazardSpellIds const& ids, float spellRadius, float unitReach, bool whitelisted)
{
    if (whitelisted || !HeldAuraHazard(ids) || spellRadius <= 0.0f || spellRadius > kHazardLargeCapYards)
        return 0.0f;
    return spellRadius + std::max(0.0f, unitReach);
}

std::optional<ProximityHazardRule> ProximityHazardFor(std::uint32_t entry)
{
    for (auto const& rule : kProximityHazards)
        if (entry && rule.entry == entry)
            return rule;
    return std::nullopt;
}

float HazardProximityRadius(ProximityHazardRule const& rule, float blastRadius, float unitReach)
{
    float const trigger = rule.trigger + (rule.edges ? std::max(0.0f, unitReach) : 0.0f);
    float const blast = rule.blast && blastRadius <= kHazardLargeCapYards ? blastRadius : 0.0f;
    float const radius = std::max(trigger, blast);
    return radius > 0.0f && radius <= kHazardLargeCapYards ? radius : 0.0f;
}

HazardZone HazardLeadZone(HazardZone zone, std::optional<HazardPoint> next, float speed)
{
    if (!next || !(speed > 0.0f))
        return zone;
    float const dx = next->x - zone.x;
    float const dy = next->y - zone.y;
    float const left = std::hypot(dx, dy);
    float const travel = std::min(left, speed * kHazardLeadSeconds);
    if (!(travel > 0.01f))
        return zone;
    zone.px = zone.x;
    zone.py = zone.y;
    zone.core = zone.radius;
    zone.vx = dx / left * speed;
    zone.vy = dy / left * speed;
    zone.until = left / speed;
    zone.x += dx / left * travel / 2.0f;
    zone.y += dy / left * travel / 2.0f;
    zone.radius += travel / 2.0f;
    zone.moving = true;
    return zone;
}

bool HazardApplies(HazardZone const& zone, std::uint32_t phaseMask, bool tank)
{
    if (zone.phase && !(zone.phase & phaseMask))
        return false;
    return !(zone.held && tank);
}

std::vector<HazardZone> HazardZonesFor(std::vector<HazardZone> const& zones, std::uint32_t phaseMask, bool tank)
{
    std::vector<HazardZone> out;
    out.reserve(zones.size());
    for (HazardZone const& zone : zones)
        if (HazardApplies(zone, phaseMask, tank))
            out.push_back(zone);
    return out;
}

std::uint64_t HazardSelectionKey(std::uint64_t zonesVersion, std::uint32_t phaseMask, bool tank)
{
    return (zonesVersion << 33) | (static_cast<std::uint64_t>(phaseMask) << 1) | (tank ? 1u : 0u);
}

bool InHazard(HazardPoint p, HazardZone const& zone, float reach)
{
    return SameFloor(p, zone) && Distance2d(p, zone.x, zone.y) <= zone.radius + reach;
}

std::optional<std::size_t> HazardAt(HazardPoint p, std::vector<HazardZone> const& zones, float reach)
{
    for (std::size_t i = 0; i < zones.size(); ++i)
        if (InHazard(p, zones[i], reach))
            return i;
    return std::nullopt;
}

bool HazardClear(HazardPoint p, std::vector<HazardZone> const& zones, float reach)
{
    for (auto const& zone : zones)
        if (SameFloor(p, zone) && Distance2d(p, zone.x, zone.y) < zone.radius + reach + kHazardMarginYards)
            return false;
    return true;
}

bool HazardSegmentClear(HazardPoint a, HazardPoint b, std::vector<HazardZone> const& zones, float reach)
{
    for (auto const& zone : zones)
        if (Crosses(a, b, zone, reach))
            return false;
    return true;
}

std::vector<HazardPoint> HazardSafeSpots(HazardPoint p, HazardPoint toward, std::vector<HazardZone> const& zones,
    float reach, std::size_t limit)
{
    if (limit == 0 || HazardClear(p, zones, reach))
        return {};
    std::vector<HazardPoint> spots;
    for (auto const& entry : ClearCandidates(p, toward, zones, reach))
    {
        if (spots.size() == limit)
            break;
        spots.push_back(entry.second);
    }
    return spots;
}

std::vector<HazardPoint> HazardExitSpots(HazardPoint at, std::optional<HazardPoint> goal, HazardPoint toward,
    std::vector<HazardZone> const& zones, float reach, std::size_t limit)
{
    if (limit == 0 || HazardClear(at, zones, reach))
        return {};
    std::vector<std::tuple<float, float, HazardPoint>> ranked;
    for (auto const& [shift, spot] : ClearCandidates(at, toward, zones, reach))
    {
        bool const hisSide = !goal || HazardSegmentClear(spot, *goal, zones, reach);
        ranked.emplace_back(shift + (hisSide ? 0.0f : kHazardSideSlackYards), Distance2d(toward, spot.x, spot.y),
            spot);
    }
    std::stable_sort(ranked.begin(), ranked.end(), [](auto const& a, auto const& b)
        { return std::tie(std::get<0>(a), std::get<1>(a)) < std::tie(std::get<0>(b), std::get<1>(b)); });
    std::vector<HazardPoint> spots;
    for (auto const& entry : ranked)
    {
        if (spots.size() == limit)
            break;
        spots.push_back(std::get<2>(entry));
    }
    return spots;
}

std::vector<HazardPoint> HazardDetour(HazardPoint from, HazardPoint goal, std::vector<HazardZone> const& zones,
    float reach, float minStep)
{
    if (HazardAt(from, zones, reach) || HazardSegmentClear(from, goal, zones, reach))
        return {};
    HazardZone const* first = nullptr;
    float firstT = 2.0f;
    for (auto const& zone : zones)
    {
        if (!Crosses(from, goal, zone, reach))
            continue;
        float const t = Closest(from, goal, zone).first;
        if (t < firstT)
        {
            firstT = t;
            first = &zone;
        }
    }
    if (!first)
        return {};

    HazardZone const& zone = *first;
    float const ring = zone.radius + reach + kHazardMarginYards + kHazardDetourOutsetYards;
    float const fromAngle = std::atan2(from.y - zone.y, from.x - zone.x);
    float delta = std::atan2(goal.y - zone.y, goal.x - zone.x) - fromAngle;
    while (delta > kPi)
        delta -= 2.0f * kPi;
    while (delta <= -kPi)
        delta += 2.0f * kPi;
    float const maxStep = kHazardDetourMaxDegrees * kPi / 180.0f;
    float const stepSize = kHazardDetourStepDegrees * kPi / 180.0f;

    std::vector<HazardPoint> waypoints;
    int const shorter = delta >= 0.0f ? 1 : -1;
    for (int direction : {shorter, -shorter})
    {
        float const remaining = direction == shorter ? std::fabs(delta) : 2.0f * kPi - std::fabs(delta);
        // The farthest round that a clear straight line still reaches.
        for (float step = std::min(remaining, maxStep); step > 0.0f; step -= stepSize)
        {
            float const angle = fromAngle + static_cast<float>(direction) * step;
            HazardPoint const waypoint{zone.x + ring * std::cos(angle), zone.y + ring * std::sin(angle), from.z};
            // A step of a degree or two where a second zone shuts the way is the bot's own spot: no progress.
            if (Distance2d(from, waypoint.x, waypoint.y) >= minStep && HazardClear(waypoint, zones, reach)
                && HazardSegmentClear(from, waypoint, zones, reach))
            {
                waypoints.push_back(waypoint);
                break;
            }
        }
    }
    return waypoints;
}

float HazardDetourStep(float followDistance, float reach)
{
    return std::max(kHazardDetourMinStepYards, followDistance + reach + 0.5f);
}

bool HazardDetourAllowed(HazardDetourTrack& track, HazardPoint at, HazardPoint goal)
{
    float const left = Distance2d(at, goal.x, goal.y);
    if (!track.started || Distance2d(track.goal, goal.x, goal.y) > kHazardDetourGoalMoveYards)
    {
        track = {goal, left, 0, true, false};
        return true;
    }
    if (track.givenUp)
        return false;
    if (left < track.best - kHazardDetourGainYards)
    {
        track.best = left;
        track.idleLegs = 0;
        return true;
    }
    if (++track.idleLegs > kHazardDetourIdleLegs)
    {
        track.givenUp = true;
        return false;
    }
    return true;
}

bool HazardFirstCrossingMoves(HazardPoint from, HazardPoint goal, std::vector<HazardZone> const& zones, float reach)
{
    HazardZone const* first = nullptr;
    float firstT = 2.0f;
    for (HazardZone const& zone : zones)
        if (Crosses(from, goal, zone, reach))
            if (float const t = Closest(from, goal, zone).first; t < firstT)
            {
                firstT = t;
                first = &zone;
            }
    return first && first->moving;
}

bool HazardStandClear(HazardPoint p, std::vector<HazardZone> const& zones, float reach)
{
    if (!HazardClear(p, zones, reach))
        return false;
    for (HazardZone const& zone : zones)
        if (zone.moving && SameFloor(p, zone)
            && Approach(p, p, zone, kHazardStandSeconds) < zone.core + reach + kHazardMarginYards)
            return false;
    return true;
}

std::optional<HazardPoint> HazardMovingLeg(HazardPoint at, HazardPoint goal, std::optional<HazardPoint> waypoint,
    std::vector<HazardZone> const& zones, float reach, float minLeg,
    std::function<bool(HazardPoint const&)> const& reachable)
{
    if (std::none_of(zones.begin(), zones.end(), [&](HazardZone const& zone)
            { return zone.moving && SameFloor(at, zone) && Distance2d(at, zone.x, zone.y) <= kHazardMovingNearYards; }))
        return std::nullopt;
    if (HazardAt(at, zones, reach))
        return std::nullopt; // stepping out comes first (the exit)
    HazardPoint const to = waypoint.value_or(goal);
    if (!waypoint && !HazardSegmentClear(at, to, zones, reach) && !HazardFirstCrossingMoves(at, to, zones, reach))
        return std::nullopt; // a zone that stays shuts the way: as before
    // A leg, or farther to the first spot along the way where the bot may stand, so that it crosses a moving
    // npc's path in one walk instead of stopping in it; only while the walk there meets nothing.
    float const length = Distance2d(at, to.x, to.y);
    for (float along = std::min(std::max(kHazardMovingLegYards, minLeg), length);; along += 1.0f)
    {
        along = std::min(along, length);
        float const part = length > 0.001f ? along / length : 1.0f;
        HazardPoint const end{at.x + (to.x - at.x) * part, at.y + (to.y - at.y) * part, at.z + (to.z - at.z) * part};
        if (!WalkClear(at, end, zones, reach))
            break;
        if (HazardStandClear(end, zones, reach) && (!reachable || reachable(end)))
            return end;
        if (along >= length)
            break;
    }
    // A moving npc shuts the way for now: wait for it to pass where the bot may stand that long, else step aside
    // to the nearest such spot a clear walk reaches (ties toward the target).
    if (HazardStandClear(at, zones, reach))
        return at;
    std::optional<HazardPoint> best;
    float bestToward = 0.0f;
    for (float ring = kHazardRingStepYards; ring <= kHazardAsideYards + 0.001f; ring += kHazardRingStepYards)
    {
        for (int k = 0; k < kHazardRingDirections; ++k)
        {
            float const angle = 2.0f * kPi * static_cast<float>(k) / static_cast<float>(kHazardRingDirections);
            HazardPoint const spot{at.x + ring * std::cos(angle), at.y + ring * std::sin(angle), at.z};
            if (!HazardStandClear(spot, zones, reach) || !WalkClear(at, spot, zones, reach))
                continue;
            float const toward = Distance2d(spot, to.x, to.y);
            if ((!best || toward < bestToward) && (!reachable || reachable(spot)))
            {
                best = spot;
                bestToward = toward;
            }
        }
        if (best)
            break; // the nearest ring that has one
    }
    // A caller that checks the real navmesh must never fall back to an unchecked
    // follow goal when no candidate is reachable. Retry from the next snapshot.
    return best ? best : (reachable ? std::optional<HazardPoint>(at) : std::nullopt);
}

bool HazardExitYielded(HazardExitGuard const& guard, std::vector<std::uint64_t> const& zones, std::uint64_t now)
{
    if (zones.empty())
        return false;
    for (std::uint64_t zone : zones)
    {
        auto const found = guard.zones.find(zone);
        if (found == guard.zones.end() || now >= found->second.yieldUntil)
            return false;
    }
    return true;
}

namespace
{
// Counts an entry into each of `zones` not yielded now; true when one's yield starts.
bool CountEntries(HazardExitGuard& guard, std::vector<std::uint64_t> const& zones, std::uint64_t now)
{
    // Forget hazards neither counting nor yielded any more.
    std::erase_if(guard.zones, [&](auto const& item)
        { return now - item.second.since > kHazardExitWindowMs && now >= item.second.yieldUntil; });
    bool started = false;
    for (std::uint64_t zone : zones)
    {
        HazardExitCount& count = guard.zones[zone];
        if (now < count.yieldUntil)
            continue;
        if (count.exits == 0 || now - count.since > kHazardExitWindowMs)
        {
            count = {1, now, 0};
            continue;
        }
        if (++count.exits >= kHazardExitsPerWindow)
        {
            count = {0, now, now + kHazardYieldMs};
            started = true;
        }
    }
    return started;
}
}

HazardEntry NoteHazardEntry(HazardExitGuard& guard, std::vector<std::uint64_t> const& zones, std::uint64_t now)
{
    if (HazardExitYielded(guard, zones, now))
        return HazardEntry::Yielded;
    CountEntries(guard, zones, now);
    return HazardExitYielded(guard, zones, now) ? HazardEntry::YieldStarts : HazardEntry::Exit;
}

HazardEntry NoteHazardZoneEntry(HazardExitGuard& guard, std::vector<HazardZone> const& inside, std::uint64_t now)
{
    std::vector<std::uint64_t> standing;
    std::vector<std::uint64_t> fixed;
    std::vector<std::uint64_t> entered;
    bool moving = false;
    for (HazardZone const& zone : inside)
    {
        standing.push_back(zone.id);
        if (zone.moving)
        {
            moving = true;
            continue;
        }
        fixed.push_back(zone.id);
        if (std::find(guard.standing.begin(), guard.standing.end(), zone.id) == guard.standing.end())
            entered.push_back(zone.id);
    }
    guard.standing = std::move(standing);
    bool const started = CountEntries(guard, entered, now);
    if (moving || !HazardExitYielded(guard, fixed, now))
        return HazardEntry::Exit;
    return started ? HazardEntry::YieldStarts : HazardEntry::Yielded;
}

bool HazardEligible(HazardMemberFacts const& facts)
{
    return facts.enabled && facts.cast && facts.bot && facts.ownerGroup && facts.ownerMaster;
}

bool HazardAware(HazardMemberFacts const& facts)
{
    return HazardEligible(facts) && !facts.fenced;
}

HazardStep DecideHazardStrategy(HazardMemberFacts const& facts, HazardHold const& hold)
{
    if (hold.journaled && !hold.sameLife)
        return HazardStep::Forget; // a new body or AI starts from its own defaults
    if (facts.fenced)
        return HazardStep::None;
    bool const eligible = HazardEligible(facts);
    if (hold.journaled)
    {
        if (!hold.present)
            return eligible ? HazardStep::Add : HazardStep::Forget;
        return eligible ? HazardStep::None : HazardStep::Remove;
    }
    return eligible && !hold.present ? HazardStep::Add : HazardStep::None;
}

std::string_view HazardOffReason(HazardMemberFacts const& facts)
{
    if (!facts.enabled)
        return "off";
    if (!facts.cast)
        return "not_cast";
    if (!facts.bot)
        return "not_bot";
    if (!facts.ownerGroup)
        return "not_in_group";
    return "master";
}
}
