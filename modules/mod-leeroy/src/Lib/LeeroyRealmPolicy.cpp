// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmPolicy.h"

#include <algorithm>
#include <charconv>
#include <limits>
#include <utility>

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

bool ValidKillId(std::string_view id)
{
    if (id.size() < 34 || id.size() > 53 || id[32] != ':' || id[33] < '1' || id[33] > '9') return false;
    if (!std::all_of(id.begin(), id.begin() + 32, [](char c) {
        return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
    })) return false;
    if (!std::all_of(id.begin() + 33, id.end(), [](char c) { return c >= '0' && c <= '9'; })) return false;
    std::uint64_t serial = 0;
    auto const parsed = std::from_chars(id.data() + 33, id.data() + id.size(), serial);
    return parsed.ec == std::errc{} && parsed.ptr == id.data() + id.size() && serial != 0;
}

bool ValidRaidMode(std::int32_t difficulty, std::uint32_t size, bool heroic)
{
    if (size != 10 && size != 25) return false;
    if (difficulty == -1) return true; // Exact mapping unavailable; size/heroic still native observations.
    return difficulty >= 0 && difficulty <= 3 && size == (difficulty % 2 ? 25u : 10u)
        && heroic == (difficulty >= 2);
}

bool ValidKillObservation(Kill const& kill)
{
    auto const maximum = static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max());
    return kill.nativeEpoch && kill.guildId && kill.humanGuid && kill.instanceId
        && ValidRaidMode(kill.difficulty, kill.size, kill.heroic)
        && kill.verifiedMs >= kill.atMs && kill.verifiedMs - kill.atMs <= kKillVerifyWithinMs
        && kill.killedAtMs && kill.killedAtMs <= kill.verifiedAtMs && kill.verifiedAtMs <= maximum
        && (kill.humanPresent || !kill.castMembers.empty()) && kill.castMembers.size() <= kMaxKillPlayers
        && (!kill.humanPresent || kill.castMembers.size() < kMaxKillPlayers)
        && std::is_sorted(kill.castMembers.begin(), kill.castMembers.end())
        && std::adjacent_find(kill.castMembers.begin(), kill.castMembers.end()) == kill.castMembers.end()
        && std::all_of(kill.castMembers.begin(), kill.castMembers.end(), [&](auto guid) {
            return guid != 0 && guid != kill.humanGuid;
        });
}

bool ValidKillProof(Kill const& kill)
{
    return ValidKillObservation(kill) && ValidKillId(kill.killId) && kill.publishedAtMs >= kill.verifiedAtMs
        && kill.publishedAtMs <= static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max());
}

std::optional<KillParticipants> QualifyKillParticipants(std::vector<PresentKillPlayer> const& present,
    std::unordered_set<std::uint32_t> const& capturedCast, std::uint32_t guild, std::uint32_t human)
{
    if (!guild || !human || present.size() > kMaxKillPlayers) return std::nullopt;
    KillParticipants result;
    std::set<std::uint32_t> seen;
    for (auto const& player : present)
    {
        if (!player.guid || !seen.insert(player.guid).second) return std::nullopt;
        if (player.guildId != guild) continue;
        if (player.guid == human) result.humanPresent = true;
        else if (capturedCast.contains(player.guid)) result.castMembers.push_back(player.guid);
    }
    if (!result.humanPresent && result.castMembers.empty()) return std::nullopt;
    std::sort(result.castMembers.begin(), result.castMembers.end());
    return result;
}

bool KillRing::Add(Kill kill)
{
    if (!ValidKillProof(kill)) return false;
    for (std::size_t i = 0; i < _count; ++i)
        if (_kills[i].killId == kill.killId) return _kills[i] == kill;
    _kills[_next] = std::move(kill);
    _next = (_next + 1) % Capacity;
    _count = std::min(_count + 1, Capacity);
    return true;
}

std::optional<Kill> KillRing::MatchProof(std::string_view killId, KillIdentity const& expected,
                                      std::uint64_t nowMs, std::uint64_t windowMs) const
{
    if (!ValidKillId(killId)) return std::nullopt;
    for (std::size_t offset = 0; offset < _count; ++offset)
    {
        Kill const& kill = _kills[(_next + Capacity - 1 - offset) % Capacity];
        if (kill.killId != killId) continue;
        if (static_cast<KillIdentity const&>(kill) != expected || nowMs < kill.verifiedMs
            || nowMs < kill.atMs || nowMs - kill.atMs > windowMs) return std::nullopt;
        return kill;
    }
    return std::nullopt;
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
