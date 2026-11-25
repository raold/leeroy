// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.
#include "DojoMotion.h"
#include "TestHarness.h"
#include <limits>

DOJO_TEST(movement_keeps_the_original_constant_speed_bound)
{
    for (float speed : {0.f, 7.f, 14.f, 42.f})
        for (unsigned diff : {10u, 38u, 200u})
        {
            float const limit = 1.f + speed * float(diff) * 0.0015f;
            CHECK(Dojo::ContinuousMovement(limit, speed, speed, diff));
            CHECK(!Dojo::ContinuousMovement(limit + 0.01f, speed, speed, diff));
        }
}

DOJO_TEST(movement_accounts_for_a_native_speed_change_within_the_update)
{
    CHECK(Dojo::ContinuousMovement(1.930734f, 42.f, 14.f, 38));
    CHECK(!Dojo::ContinuousMovement(1.930734f, 14.f, 14.f, 38));
    CHECK(Dojo::ContinuousMovement(1.930734f, 14.f, 42.f, 38));
    CHECK(!Dojo::ContinuousMovement(3.5f, 42.f, 14.f, 38));
    CHECK(!Dojo::ContinuousMovement(3.5f, 14.f, 42.f, 38));
    CHECK(!Dojo::ContinuousMovement(0.f, 14.f, 14.f, 0));
    for (float invalid : {-1.f, std::numeric_limits<float>::quiet_NaN(), std::numeric_limits<float>::infinity()})
    {
        CHECK(!Dojo::ContinuousMovement(invalid, 14.f, 14.f, 38));
        CHECK(!Dojo::ContinuousMovement(1.f, invalid, 14.f, 38));
        CHECK(!Dojo::ContinuousMovement(1.f, 14.f, invalid, 38));
    }
}
