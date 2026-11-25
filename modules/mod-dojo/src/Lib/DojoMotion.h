// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.
#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>

namespace Dojo
{
inline bool ContinuousMovement(float distance, float previousSpeed, float currentSpeed, std::uint32_t diff)
{
    if (!std::isfinite(distance) || !std::isfinite(previousSpeed) || !std::isfinite(currentSpeed)
        || distance < 0.f || previousSpeed < 0.f || currentSpeed < 0.f || !diff) return false;
    // A native speed aura can expire after movement in this update. Preserve
    // both observed endpoint rates; the original distance margin is unchanged.
    return distance <= 1.f + std::max(previousSpeed, currentSpeed) * float(diff) * 0.0015f;
}
}
