// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.
#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>

namespace Dojo
{
class ConstrictorCastProof
{
public:
    void Record(std::uint32_t spell, std::uint64_t caster, std::uint64_t target, std::uint32_t now)
    {
        if (spell == 64133) { _spawnActor = caster; _spawnMs = now; }
        else if (spell == 64123) { _lungeActor = caster; _lungeTarget = target; _lungeMs = now; }
    }
    bool Matches(std::uint64_t actor, std::uint64_t vehicle, std::uint32_t now) const
    {
        return actor && vehicle && _spawnActor == actor && _lungeActor == actor && _lungeTarget == vehicle
            && now - _spawnMs <= 250 && now - _lungeMs <= 250;
    }
private:
    std::uint64_t _spawnActor = 0, _lungeActor = 0, _lungeTarget = 0;
    std::uint32_t _spawnMs = 0, _lungeMs = 0;
};

inline bool BlinkDestination(float dx, float dy, float facing, float radius)
{
    if (!std::isfinite(dx) || !std::isfinite(dy) || !std::isfinite(facing)
        || !std::isfinite(radius) || radius <= 0.f) return false;
    // TARGET_DEST_CASTER_FRONT_LEAP follows the cast facing and stops at
    // collision, within the spell's modified radius. Z follows the core floor.
    float const forward = dx * std::cos(facing) + dy * std::sin(facing);
    float const lateral = dy * std::cos(facing) - dx * std::sin(facing);
    return forward >= -0.1f && forward <= radius + 0.1f && std::abs(lateral) <= 0.1f;
}
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
