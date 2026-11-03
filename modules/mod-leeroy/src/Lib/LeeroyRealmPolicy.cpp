// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmPolicy.h"

#include <algorithm>

namespace Leeroy::Realm
{
std::string BusyReason(BodyState const& state, Purpose purpose)
{
    bool const leave = purpose == Purpose::LeavePvp;
    bool const rearm = purpose == Purpose::Rearm;
    bool const groupWaiver = rearm || purpose == Purpose::Mail || purpose == Purpose::Bank;
    if (!state.online)
        return "offline";
    if (state.dead && !leave)
        return "dead";
    if (state.combat && !leave)
        return "combat";
    if (state.flight && !leave && !rearm)
        return "flight";
    if (state.raidGroup && !groupWaiver && !(leave && state.pvpOrLfgGroup))
        return "raid_group";
    if (state.humanGroup && !groupWaiver)
        return "human_group";
    if (state.instance && !leave && !rearm)
        return "instance";
    if (state.battleground && !leave && !rearm)
        return "battleground";
    if (state.arena && !leave && !rearm)
        return "arena";
    if (state.lfg && !leave && !rearm)
        return "lfg";
    return "";
}

LfgExit DecideLfgExit(LfgPhase phase, bool inLfgGroup)
{
    if (inLfgGroup)
        return LfgExit::LeaveGroup;
    return phase == LfgPhase::None ? LfgExit::None : LfgExit::LeaveQueue;
}

bool Flagged(HumanPvp const& human)
{
    return human.manualFlag || human.battleground || human.arena || human.wgWar;
}

bool GankerMoveAllowed(HumanPvp const& human, std::uint32_t destZone)
{
    return !human.online || destZone != human.zone || Flagged(human);
}

void KillRing::Add(Kill kill)
{
    _kills[_next] = kill;
    _next = (_next + 1) % Capacity;
    _count = std::min(_count + 1, Capacity);
}

std::optional<Kill> KillRing::Match(std::uint32_t map, std::uint32_t instanceId, std::uint32_t bossId,
                                 std::uint64_t nowMs, std::uint64_t windowMs) const
{
    std::optional<Kill> result;
    // Walk newest insertion first so equal timestamps use the most recent
    // observation, while out-of-order older events cannot replace newer facts.
    for (std::size_t offset = 0; offset < _count; ++offset)
    {
        Kill const& kill = _kills[(_next + Capacity - 1 - offset) % Capacity];
        if (!kill.guildKill || kill.map != map || kill.instanceId != instanceId || kill.bossId != bossId
            || kill.atMs > nowMs || nowMs - kill.atMs > windowMs)
            continue;
        if (!result || kill.atMs > result->atMs)
            result = kill;
    }
    return result;
}

TickBudget::TickBudget(std::uint32_t cap) : _cap(cap), _left(cap) { }

bool TickBudget::Take()
{
    if (!_left)
        return false;
    --_left;
    return true;
}

std::uint32_t TickBudget::Left() const { return _left; }
bool TickBudget::Take(std::uint32_t count)
{
    if (count > _left) return false;
    _left -= count;
    return true;
}
void TickBudget::Reset() { _left = _cap; }

bool ShouldRecord(WeaponNow const& now)
{
    return now.mainHand != 0 && !now.mainIsPole;
}

std::vector<RearmStep> PlanRearm(WeaponRecord const& record, WeaponNow const& now,
                               std::set<std::uint32_t> const& bagItems)
{
    std::array<std::uint32_t, 3> const recorded{record.mainHand, record.offHand, record.ranged};
    std::array<std::uint32_t, 3> const equipped{now.mainHand, now.offHand, now.ranged};
    std::vector<RearmStep> steps;
    auto const add = [&](WeaponSlot slot, std::uint32_t item, bool open)
    {
        if (open && item && bagItems.contains(item)
            && std::count(recorded.begin(), recorded.end(), item) == 1
            && std::find(equipped.begin(), equipped.end(), item) == equipped.end())
            steps.push_back({slot, item});
    };
    add(WeaponSlot::Main, record.mainHand, now.mainHand == 0 || now.mainIsPole);
    add(WeaponSlot::Off, record.offHand, now.offHand == 0);
    add(WeaponSlot::Ranged, record.ranged, now.ranged == 0);
    return steps;
}
}
