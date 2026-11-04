// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmCommands.h"
#include "TestHarness.h"
#include <array>
#include <fstream>
#include <iterator>
#include <set>

using namespace Leeroy::Realm;
LEEROY_TEST(Realm_ItemsDistParses)
{
    std::ifstream input(std::string(LEEROY_TEST_FIXTURES) + "/../../conf/leeroy_items.csv.dist");
    CHECK(input.is_open());
    std::string csv((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>()), error;
    auto rules = ParseItems(csv, error);
    CHECK(rules && !rules->empty()); CHECK(error.empty());
    if (!rules) return;
    std::set<std::pair<std::uint32_t, std::string>> unique;
    for (auto const& row : *rules)
    {
        CHECK(row.use != "equip"); CHECK(row.itemId != 6256 && row.itemId != 5976);
        CHECK(unique.emplace(row.itemId, row.use).second);
        CHECK(row.maxCount && row.maxCount <= kMaxItemCount);
    }
    for (auto id : {43015u,34753u,46377u,46376u,46379u,46378u})
    { CHECK(unique.contains({id, "bank"})); CHECK(unique.contains({id, "gift"})); }
    std::array<std::array<std::uint32_t, 6>, 4> const categories{{
        {40111,40112,40113,40118,40123,40133}, {34054,34055,34056,34057,34052,34053},
        {47586,47590,47592,47596,47600,47601}, {8485,8486,8487,8488,8489,8490}}};
    for (auto const& category : categories)
        for (auto id : category) CHECK(unique.contains({id, "bank"}));
    for (auto id : {22218u,22236u,22237u,22238u,22239u,37584u,37585u})
    {
        CHECK(unique.contains({id, "holiday"}));
        CHECK(!unique.contains({id, "bank"}) && !unique.contains({id, "gift"}));
    }
    for (auto id : {8490u,8491u,8492u,8494u,8495u,8496u}) CHECK(unique.contains({id, "prize"}));
}
