// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.
#include "DojoMotion.h"
#include "TestHarness.h"
#include <limits>

DOJO_TEST(blink_requires_the_cast_direction_and_modified_radius)
{
    CHECK(Dojo::BlinkDestination(20.f, 0.f, 0.f, 20.f));
    CHECK(Dojo::BlinkDestination(19.4f, 0.f, 0.f, 20.f)); // Collision can shorten Blink.
    CHECK(Dojo::BlinkDestination(25.f, 0.f, 0.f, 25.f)); // Modified spell radius.
    CHECK(!Dojo::BlinkDestination(25.f, 0.f, 0.f, 20.f));
    CHECK(!Dojo::BlinkDestination(-19.4f, 0.f, 0.f, 20.f));
    CHECK(!Dojo::BlinkDestination(19.4f, 1.f, 0.f, 20.f));
    CHECK(!Dojo::BlinkDestination(20.2f, 0.f, 0.f, 20.f));
    CHECK(!Dojo::BlinkDestination(0.f, 0.f, 0.f, 0.f));
    for (float invalid : {std::numeric_limits<float>::quiet_NaN(), std::numeric_limits<float>::infinity()})
    {
        CHECK(!Dojo::BlinkDestination(invalid, 0.f, 0.f, 20.f));
        CHECK(!Dojo::BlinkDestination(20.f, invalid, 0.f, 20.f));
        CHECK(!Dojo::BlinkDestination(20.f, 0.f, invalid, 20.f));
        CHECK(!Dojo::BlinkDestination(20.f, 0.f, 0.f, invalid));
    }
}

DOJO_TEST(movement_matches_the_native_ground_charge_speed_limit)
{
    // Actual Intercept trace: 42 requested, 28 on the launched spline,
    // 1.661907 yards in 59 ms. Comparing against 42 lost the charge witness.
    CHECK(Dojo::GroundChargeSpeed(42.f, 7.f) == 28.f);
    CHECK(Dojo::ContinuousMovement(1.661907f, 28.f, 28.f, 59));
    CHECK(!Dojo::ContinuousMovement(1.661907f, 7.f, 7.f, 59));
    CHECK(!Dojo::ContinuousMovement(4.f, 28.f, 28.f, 59));
    CHECK(Dojo::GroundChargeSpeed(42.f, 14.f) == 42.f);
    CHECK(Dojo::GroundChargeSpeed(20.f, 7.f) == 20.f);
    CHECK(Dojo::GroundChargeSpeed(42.f, 0.f) == 28.f);
    for (float invalid : {-1.f, std::numeric_limits<float>::quiet_NaN(), std::numeric_limits<float>::infinity()})
    {
        CHECK(Dojo::GroundChargeSpeed(invalid, 7.f) == 0.f);
        CHECK(Dojo::GroundChargeSpeed(42.f, invalid) == 0.f);
    }
}

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
