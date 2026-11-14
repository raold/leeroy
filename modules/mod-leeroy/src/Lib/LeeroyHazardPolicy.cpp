// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyHazardPolicy.h"

#include <algorithm>
#include <cmath>
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

bool Crosses(HazardPoint a, HazardPoint b, HazardZone const& zone, float reach)
{
    return (SameFloor(a, zone) || SameFloor(b, zone)) && Closest(a, b, zone).second <= zone.radius + reach;
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

HazardEntry NoteHazardEntry(HazardExitGuard& guard, std::vector<std::uint64_t> const& zones, std::uint64_t now)
{
    if (HazardExitYielded(guard, zones, now))
        return HazardEntry::Yielded;
    // Forget hazards neither counting nor yielded any more.
    std::erase_if(guard.zones, [&](auto const& item)
        { return now - item.second.since > kHazardExitWindowMs && now >= item.second.yieldUntil; });
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
            count = {0, now, now + kHazardYieldMs};
    }
    return HazardExitYielded(guard, zones, now) ? HazardEntry::YieldStarts : HazardEntry::Exit;
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
