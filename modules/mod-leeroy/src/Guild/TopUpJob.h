// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_TOP_UP_JOB_H
#define MOD_LEEROY_TOP_UP_JOB_H

#include <cstdint>

namespace Leeroy::Guild
{
inline constexpr std::uint32_t kTopUpEveryMs = 15 * 60 * 1000;
inline constexpr std::uint32_t kTopUpPerTick = 5;
inline constexpr std::uint64_t kTopUpCooldownMs = 60 * 60 * 1000;
void TickTopUp(std::uint32_t diffMs);
}

#endif
