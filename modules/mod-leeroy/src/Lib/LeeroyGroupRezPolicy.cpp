// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyGroupRezPolicy.h"

namespace Leeroy::Guild
{
namespace
{
constexpr std::array<std::uint32_t, 7> kRedemption{48950, 48949, 20773, 20772, 10324, 10322, 7328};
constexpr std::array<std::uint32_t, 7> kResurrection{48171, 25435, 20770, 10881, 10880, 2010, 2006};
constexpr std::array<std::uint32_t, 7> kAncestralSpirit{49277, 25590, 20777, 20776, 20610, 20609, 2008};
constexpr std::array<std::uint32_t, 7> kRevive{50763, 50764, 50765, 50766, 50767, 50768, 50769};

bool Closer(GroupRezCandidate const& c, GroupRezCandidate const* best)
{
    return !best || c.distance < best->distance || (c.distance == best->distance && c.guid < best->guid);
}

bool InReachOfSpell(GroupRezCandidate const& c)
{
    return c.los && c.distance <= c.castRange;
}
}

std::array<std::uint32_t, 7> const* RezRanks(std::uint8_t playerClass)
{
    switch (playerClass)
    {
        case 2: return &kRedemption;      // paladin
        case 5: return &kResurrection;    // priest
        case 7: return &kAncestralSpirit; // shaman
        case 11: return &kRevive;         // druid
        default: return nullptr;
    }
}

void NoteGroupRezRefused(GroupRezCasters& casters, std::uint32_t caster, std::uint64_t now, bool transient)
{
    casters[caster].refusedUntil = now + (transient ? kGroupRezRetryMs : kGroupRezRefusedMs);
}

void NoteGroupRezDrinkAsked(GroupRezCasters& casters, std::uint32_t caster, std::uint64_t now)
{
    casters[caster].drinkAskedAt = now;
}

void NoteGroupRezUnreachable(GroupRezMemory& memory, std::uint32_t caster, std::uint64_t now)
{
    memory.unreachableAt[caster] = now;
    if (memory.approachCaster == caster)
        memory.approachCaster.reset();
}

void PruneGroupRezCasters(GroupRezCasters& casters, std::uint64_t now)
{
    std::erase_if(casters, [now](auto const& item) {
        auto const& c = item.second;
        return (!c.refusedUntil || now >= *c.refusedUntil)
            && (!c.drinkAskedAt || now - *c.drinkAskedAt >= kGroupRezDrinkRetryMs);
    });
}

char const* GroupRezBlockCode(GroupRezBlock block)
{
    switch (block)
    {
        case GroupRezBlock::None: return "none";
        case GroupRezBlock::Combat: return "combat";
        case GroupRezBlock::Fenced: return "fenced";
        case GroupRezBlock::AwaitingAnswer: return "awaiting_answer";
        case GroupRezBlock::SelfRes: return "self_res";
        case GroupRezBlock::NoCaster: return "no_caster";
        case GroupRezBlock::NoCasterInRange: return "no_caster_in_range";
        case GroupRezBlock::AllOom: return "all_oom";
        case GroupRezBlock::Unreachable: return "unreachable";
        case GroupRezBlock::CasterCombat: return "caster_combat";
        case GroupRezBlock::CastersBusy: return "casters_busy";
    }
    return "unknown";
}

GroupRezDecision DecideGroupRez(GroupRezBody const& body, bool fightNear,
    std::vector<GroupRezCandidate> const& candidates, GroupRezMemory& memory, GroupRezCasters const& casters,
    std::uint64_t now, std::uint64_t releaseMs)
{
    if (body.fenced || fightNear)
    {
        memory.quietSince.reset();
        memory.approachCaster.reset();
        return {GroupRezStep::None, 0, body.fenced ? GroupRezBlock::Fenced : GroupRezBlock::Combat};
    }
    if (!memory.quietSince)
        memory.quietSince = now;
    if (body.rezzing || body.requested)
    {
        memory.approachCaster.reset();
        return {GroupRezStep::None, 0, body.requested ? GroupRezBlock::AwaitingAnswer : GroupRezBlock::None};
    }
    if (body.bot && !body.instance && releaseMs && now - *memory.quietSince >= releaseMs)
        return {GroupRezStep::Release, 0, GroupRezBlock::None};
    if (body.selfRes)
        return {GroupRezStep::None, 0, GroupRezBlock::SelfRes};

    std::erase_if(memory.unreachableAt, [now](auto const& item) { return now - item.second >= kGroupRezUnreachableMs; });

    bool inReach = false, oom = false, unreachable = false, combat = false;
    GroupRezCandidate const* walking = nullptr; // already walking in to this body
    GroupRezCandidate const* ready = nullptr;   // nearest within its spell's range and in sight
    GroupRezCandidate const* nearest = nullptr;
    GroupRezCandidate const* thirsty = nullptr; // nearest short of mana, not asked to drink lately
    for (auto const& c : candidates)
    {
        if (c.distance > kGroupRezReachYards)
            continue;
        inReach = true;
        auto const known = casters.find(c.guid);
        if (c.fenced || c.busy || (c.mounted && !c.grounded)
            || (known != casters.end() && known->second.refusedUntil && now < *known->second.refusedUntil))
            continue;
        if (c.combat)
        {
            combat = true;
            continue;
        }
        if (memory.unreachableAt.contains(c.guid))
        {
            unreachable = true;
            continue;
        }
        if (c.mana < c.manaCost)
        {
            oom = true;
            bool const asked = known != casters.end() && known->second.drinkAskedAt
                && now - *known->second.drinkAskedAt < kGroupRezDrinkRetryMs;
            if (!asked && Closer(c, thirsty))
                thirsty = &c;
            continue;
        }
        if (memory.approachCaster == c.guid)
            walking = &c;
        if (InReachOfSpell(c) && Closer(c, ready))
            ready = &c;
        if (Closer(c, nearest))
            nearest = &c;
    }

    if (GroupRezCandidate const* pick = ready ? ready : walking ? walking : nearest)
    {
        if (InReachOfSpell(*pick))
        {
            memory.approachCaster.reset();
            if (memory.lastCast && now - *memory.lastCast < kGroupRezRetryMs)
                return {};
            memory.lastCast = now;
            return {GroupRezStep::Cast, pick->guid, GroupRezBlock::None};
        }
        if (memory.approachCaster != pick->guid)
        {
            memory.approachCaster = pick->guid;
            memory.approachSince = now;
        }
        else if (now - memory.approachSince >= kGroupRezApproachMs)
        {
            // No path, or it never came into sight: another rezzer tries from the next second.
            memory.unreachableAt[pick->guid] = now;
            memory.approachCaster.reset();
            return {};
        }
        return {GroupRezStep::Approach, pick->guid, GroupRezBlock::None};
    }
    memory.approachCaster.reset();

    GroupRezBlock block = GroupRezBlock::CastersBusy;
    if (candidates.empty())
        block = GroupRezBlock::NoCaster;
    else if (!inReach)
        block = GroupRezBlock::NoCasterInRange;
    else if (oom)
        block = GroupRezBlock::AllOom;
    else if (unreachable)
        block = GroupRezBlock::Unreachable;
    else if (combat)
        block = GroupRezBlock::CasterCombat;
    if (thirsty)
        return {GroupRezStep::Drink, thirsty->guid, block};
    return {GroupRezStep::None, 0, block};
}

bool GroupRezBlockLogDue(std::map<GroupRezBlock, std::uint64_t>& logged, GroupRezBlock block, std::uint64_t now)
{
    if (block == GroupRezBlock::None)
        return false;
    if (auto it = logged.find(block); it != logged.end() && now - it->second < kGroupRezBlockLogMs)
        return false;
    logged[block] = now;
    return true;
}
}
