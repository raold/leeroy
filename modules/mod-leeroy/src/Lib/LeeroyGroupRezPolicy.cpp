// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyGroupRezPolicy.h"

namespace Leeroy::Guild
{
namespace
{
constexpr std::array<std::uint32_t, 7> kRedemption{48950, 48949, 20773, 20772, 10324, 10322, 7328};
constexpr std::array<std::uint32_t, 7> kResurrection{48171, 25435, 20770, 10881, 10880, 2010, 2006};
constexpr std::array<std::uint32_t, 7> kAncestralSpirit{49277, 25590, 20777, 20776, 20610, 20609, 2008};
}

std::array<std::uint32_t, 7> const* RezRanks(std::uint8_t playerClass)
{
    switch (playerClass)
    {
        case 2: return &kRedemption;      // paladin
        case 5: return &kResurrection;    // priest
        case 7: return &kAncestralSpirit; // shaman
        default: return nullptr;
    }
}

GroupRezDecision DecideGroupRez(GroupRezBody const& body, bool groupCombat,
    std::vector<GroupRezCandidate> const& candidates, GroupRezMemory& memory,
    std::uint64_t now, std::uint64_t releaseMs)
{
    if (body.fenced || groupCombat)
    {
        memory.quietSince.reset();
        return {};
    }
    if (!memory.quietSince)
        memory.quietSince = now;
    if (body.requested || body.rezzing)
        return {};
    if (body.bot && !body.instance && releaseMs && now - *memory.quietSince >= releaseMs)
        return {GroupRezStep::Release, 0};
    if (body.selfRes)
        return {};

    GroupRezCandidate const* best = nullptr;
    for (auto const& c : candidates)
    {
        if (!c.los || c.distance > kGroupRezSearchYards || (c.mounted && !c.grounded))
            continue;
        if (!best || c.distance < best->distance || (c.distance == best->distance && c.guid < best->guid))
            best = &c;
    }
    if (!best)
        return {};
    if (best->distance <= best->castRange)
    {
        if (memory.lastCast && now - *memory.lastCast < kGroupRezRetryMs)
            return {};
        memory.lastCast = now;
        return {GroupRezStep::Cast, best->guid};
    }
    if (best->mounted)
        return {GroupRezStep::Dismount, best->guid};
    return {};
}
}
