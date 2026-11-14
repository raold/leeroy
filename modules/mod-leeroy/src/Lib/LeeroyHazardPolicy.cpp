// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyHazardPolicy.h"

#include <algorithm>
#include <array>
#include <cmath>
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
constexpr std::array<LargeHazard, 5> kLargeHazards{{
    {28241, LargeHazardRadius::CasterReach}, // Poison, Grobbulus's cloud: hits within the cloud's growing reach
    {69789, LargeHazardRadius::Spell},       // Ooze Flood, Rotface's flooded corner (25 yards)
    {71215, LargeHazardRadius::Spell},       // its 25-man and heroic versions
    {71587, LargeHazardRadius::Spell},
    {71588, LargeHazardRadius::Spell},
}};

float Distance2d(HazardPoint a, float x, float y)
{
    return std::hypot(a.x - x, a.y - y);
}

bool SameFloor(HazardPoint p, HazardZone const& zone)
{
    return std::fabs(p.z - zone.z) <= kHazardHeightYards;
}
}

std::optional<LargeHazardRadius> LargeHazardRule(std::uint32_t spellId)
{
    for (auto const& hazard : kLargeHazards)
        if (hazard.spell == spellId)
            return hazard.radius;
    return std::nullopt;
}

float HazardAvoidRadius(std::uint32_t spellId, float spellRadius, float casterReach, float stockCap,
    bool whitelisted)
{
    if (whitelisted)
        return 0.0f;
    if (auto const rule = LargeHazardRule(spellId))
    {
        float const radius = *rule == LargeHazardRadius::CasterReach ? casterReach : spellRadius;
        return radius > 0.0f && radius <= kHazardLargeCapYards ? radius : 0.0f;
    }
    return spellRadius > 0.0f && spellRadius <= stockCap ? spellRadius : 0.0f;
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

std::vector<HazardPoint> HazardSafeSpots(HazardPoint p, HazardPoint toward, std::vector<HazardZone> const& zones,
    float reach, std::size_t limit)
{
    if (limit == 0 || HazardClear(p, zones, reach))
        return {};

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
    float constexpr pi = 3.14159265358979f;
    for (float ring = kHazardRingStepYards; ring <= kHazardMaxShiftYards + 0.001f; ring += kHazardRingStepYards)
        for (int k = 0; k < kHazardRingDirections; ++k)
        {
            float const angle = 2.0f * pi * static_cast<float>(k) / static_cast<float>(kHazardRingDirections);
            candidates.push_back({p.x + ring * std::cos(angle), p.y + ring * std::sin(angle), p.z});
        }

    std::vector<std::pair<std::pair<float, float>, HazardPoint>> clear;
    for (auto const& c : candidates)
    {
        float const shift = Distance2d(p, c.x, c.y);
        if (shift <= kHazardMaxShiftYards + 0.001f && HazardClear(c, zones, reach))
            clear.push_back({{shift, Distance2d(toward, c.x, c.y)}, c});
    }
    std::stable_sort(clear.begin(), clear.end(),
        [](auto const& a, auto const& b) { return a.first < b.first; });
    std::vector<HazardPoint> spots;
    for (auto const& entry : clear)
    {
        if (spots.size() == limit)
            break;
        spots.push_back(entry.second);
    }
    return spots;
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
