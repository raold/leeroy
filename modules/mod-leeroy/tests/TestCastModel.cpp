// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyCastModel.h"
#include "TestHarness.h"
#include <nlohmann/json.hpp>
#include <set>

using namespace Leeroy;
using json = nlohmann::json;

LEEROY_TEST(Cast_Eligibility)
{
    CHECK(IsCastEligible(5, 80, 1)); CHECK(!IsCastEligible(5, 79, 1)); CHECK(!IsCastEligible(1, 83, 1));
}

LEEROY_TEST(Cast_MemberJsonKeys)
{
    auto j = ToJson({9, "Anna", 10, 2, 1, 80, 3, false, -1, 1637, -1});
    std::set<std::string> keys; for (auto& [k, v] : j.items()) keys.insert(k);
    CHECK(keys == std::set<std::string>({"guid", "name", "race", "class", "gender", "level", "rank", "online",
                                         "map", "zone", "spec_tab"}));
    CHECK_EQ(j["class"], 2); CHECK_EQ(j["map"], -1); CHECK_EQ(j["spec_tab"], -1);
}

LEEROY_TEST(Cast_EncodeSortedByGuid)
{
    auto j = json::parse(EncodeCast(1, {{9, "c"}, {3, "a"}, {5, "b"}}));
    CHECK_EQ(j["type"], "cast"); CHECK_EQ(j["members"].size(), 3u);
    CHECK_EQ(j["members"][0]["guid"], 3); CHECK_EQ(j["members"][2]["guid"], 9);
}
