// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_GEAR_UP_POLICY_H
#define MOD_LEEROY_GEAR_UP_POLICY_H

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <string_view>

namespace Leeroy::Guild
{
// Snapshot immediately before mutation, not the state when the command was queued.
// Ownership means current configured-guild membership, excluding the human; level is deliberately absent.
struct GearUpState
{
    bool owned = false, online = false, bot = false;
    bool grouped = false, inBg = false, inInstance = false, teleporting = false;
    bool dead = false, inCombat = false, inFlight = false, trading = false;
};

inline std::string_view GearUpSkipReason(GearUpState const& s)
{
    if (!s.owned) return "not_cast";
    if (!s.online) return "offline";
    if (!s.bot) return "not_bot";
    if (s.grouped) return "grouped";
    if (s.inBg) return "in_bg";
    if (s.inInstance) return "in_instance";
    if (s.teleporting) return "teleporting";
    if (s.dead) return "dead";
    if (s.inCombat) return "in_combat";
    if (s.inFlight) return "in_flight";
    if (s.trading) return "trading";
    return {};
}

inline std::uint32_t GearUpLevel(std::uint32_t current, std::uint32_t requested)
{
    return std::max(current, requested);
}

inline constexpr auto kGearUpTimeout = std::chrono::minutes(5);
inline bool GearUpExpired(std::chrono::steady_clock::time_point started,
    std::chrono::steady_clock::time_point now)
{
    return now - started >= kGearUpTimeout;
}
}

#endif
