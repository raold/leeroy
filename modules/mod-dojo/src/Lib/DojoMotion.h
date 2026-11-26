// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.
#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>

namespace Dojo
{
inline float GroundChargeSpeed(float requested, float running)
{
    if (!std::isfinite(requested) || !std::isfinite(running) || requested <= 0.f || running < 0.f) return 0.f;
    // MoveSplineInit::Launch applies this client limit to a ground spline.
    return std::min(requested, std::max(28.f, running * 4.f));
}
inline bool ContinuousMovement(float distance, float previousSpeed, float currentSpeed, std::uint32_t diff)
{
    if (!std::isfinite(distance) || !std::isfinite(previousSpeed) || !std::isfinite(currentSpeed)
        || distance < 0.f || previousSpeed < 0.f || currentSpeed < 0.f || !diff) return false;
    // A native speed aura can expire after movement in this update. Preserve
    // both observed endpoint rates; the original distance margin is unchanged.
    return distance <= 1.f + std::max(previousSpeed, currentSpeed) * float(diff) * 0.0015f;
}
}
