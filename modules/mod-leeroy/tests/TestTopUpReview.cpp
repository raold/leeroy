// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyTopUpPolicy.h"
#include "TestHarness.h"

using namespace Leeroy::Guild;

LEEROY_TEST(Guild_TopUpFoodDoesNotConsumeDrinkSpaceWithMoreExistingFood)
{
    CHECK(TopUpFoodCategories(true, false, 1000) == std::vector<std::uint32_t>({59}));
    CHECK(TopUpFoodCategories(false, true, 1000) == std::vector<std::uint32_t>({11}));
    CHECK(TopUpFoodCategories(true, true, 1000).empty());
}

LEEROY_TEST(Guild_TopUpDrinkDependsOnManaCapacityNotCurrentFormOrMana)
{
    // A shifted or empty-mana caster retains its mana capacity; no current-power input can suppress drink.
    CHECK(TopUpFoodCategories(false, false, 1) == std::vector<std::uint32_t>({11, 59}));
    CHECK(TopUpFoodCategories(true, false, 1) == std::vector<std::uint32_t>({59}));
    CHECK(TopUpFoodCategories(false, false, 0) == std::vector<std::uint32_t>({11}));
    CHECK(TopUpFoodCategories(true, false, 0).empty());
}

LEEROY_TEST(Guild_TopUpPartialFoodSuccessLeavesOnlyDrinkForTheNextPass)
{
    CHECK(TopUpFoodCategories(false, false, 1000) == std::vector<std::uint32_t>({11, 59}));
    // A single free slot held the food stack. The next pass must never request another food stack.
    CHECK(TopUpFoodCategories(true, false, 1000) == std::vector<std::uint32_t>({59}));
    CHECK(TopUpFoodCategories(true, true, 1000).empty());
}
