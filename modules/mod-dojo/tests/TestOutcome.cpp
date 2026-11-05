// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

// Episode outcomes and rewards: the shared cases in tests/fixtures/outcome_cases.json, which
// dojo/tests/test_outcome.py also runs.

#include "DojoOutcome.h"
#include "TestHarness.h"

#include <cmath>
#include <fstream>
#include <sstream>
#include <string>

#include <nlohmann/json.hpp>

using namespace Dojo;
using nlohmann::json;

namespace
{
json Cases()
{
    std::ifstream in(std::string(DOJO_TEST_FIXTURES) + "/outcome_cases.json", std::ios::binary);
    std::ostringstream text;
    text << in.rdbuf();
    return json::parse(text.str());
}

bool Near(double a, double b)
{
    return std::fabs(a - b) < 1e-9;
}
}

DOJO_TEST(Outcome_SharedCases)
{
    json const doc = Cases();
    CHECK(Near(doc["shaping_scale"].get<double>(), kShapingScale));
    CHECK(doc["cases"].size() >= 8);
    for (json const& c : doc["cases"])
    {
        std::string const name = c["name"].get<std::string>();
        std::vector<Standing> actors;
        for (json const& a : c["actors"])
            actors.push_back({a["team"].get<std::uint32_t>(), a["alive"].get<bool>(), a["health"].get<double>()});
        Outcome const out = DecideOutcome(actors, c["time_up"].get<bool>());
        CHECK_MSG(std::string(OutcomeReasonName(out.reason)) == c["outcome"]["reason"].get<std::string>(), name);
        if (c["outcome"]["winner"].is_null())
            CHECK_MSG(!out.winner.has_value(), name);
        else
            CHECK_MSG(out.winner == c["outcome"]["winner"].get<std::uint32_t>(), name);
        CHECK_MSG(Near(Potential(actors), c["potential"].get<double>()), name + ": potential " + std::to_string(Potential(actors)));
        std::vector<Reward> const rewards = Rewards(actors, c["before"].get<double>(), out);
        CHECK_MSG(rewards.size() == c["rewards"].size(), name);
        for (std::size_t i = 0; i < rewards.size() && i < c["rewards"].size(); ++i)
        {
            CHECK_MSG(Near(rewards[i].terminal, c["rewards"][i]["terminal"].get<double>()), name + " terminal " + std::to_string(i));
            CHECK_MSG(Near(rewards[i].shaping, c["rewards"][i]["shaping"].get<double>()),
                name + " shaping " + std::to_string(i) + ": " + std::to_string(rewards[i].shaping));
        }
    }
}

DOJO_TEST(Outcome_TerminalRewardsSumToZeroAndShapingCancels)
{
    for (bool deadA : {false, true})
        for (bool deadB : {false, true})
            for (bool timeUp : {false, true})
            {
                std::vector<Standing> const actors{{0, !deadA, 0.37}, {1, !deadB, 0.81}};
                Outcome const out = DecideOutcome(actors, timeUp);
                CHECK(out.Over() == (deadA || deadB || timeUp));
                std::vector<Reward> const r = Rewards(actors, -0.2, out);
                CHECK(Near(r[0].terminal + r[1].terminal, 0.0));
                CHECK(Near(r[0].shaping + r[1].shaping, 0.0));
                // Phi is 0 at every terminal state: the shaping only returns what the episode had accumulated.
                if (out.Over())
                    CHECK(Near(r[0].shaping, 0.05 * 0.2));
            }
}

DOJO_TEST(Outcome_NothingToDecideWithoutActors)
{
    CHECK(!DecideOutcome({}, true).Over());
    CHECK(Rewards({}, 0.0, Outcome{}).empty());
}
