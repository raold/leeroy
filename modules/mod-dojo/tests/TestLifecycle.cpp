// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#include "DojoLifecycle.h"
#include "TestHarness.h"

#include <algorithm>
#include <fstream>
#include <optional>
#include <random>
#include <set>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

using namespace Dojo;

namespace
{
Json FixtureWorker()
{
    std::ifstream in(std::string(DOJO_TEST_FIXTURES) + "/dojo_protocol.jsonl", std::ios::binary);
    std::string line;
    std::getline(in, line);
    return Json::parse(line)["worker"];
}

Json Open()
{
    Json obs = Json::array();
    for (int i = 0; i < 2; ++i)
        obs.push_back(Json{{"actor", i}, {"obs", Json::array({0.5})}, {"mask", Json::array({1, 0, 1})}});
    return obs;
}

Json StepDone(bool terminated)
{
    Json rewards = Json::array();
    for (int i = 0; i < 2; ++i)
        rewards.push_back(Json{{"actor", i}, {"terminal", 0}, {"shaping", 0.0}});
    return Json{{"elapsed_ms", 200}, {"substeps", 20}, {"sim_ms", 200}, {"observations", Open()},
        {"events", Json::array()}, {"rewards", rewards}, {"terminated", terminated}, {"truncated", false},
        {"outcome", terminated ? Json{{"reason", "time_limit"}, {"winner", nullptr}} : Json(nullptr)}};
}

Output Recv(Lifecycle& lc, Json const& message)
{
    return lc.Handle(DecodeRequest(Encode(message), lc.GetBounds()));
}

bool Refused(Json info)
{
    try
    {
        Lifecycle lc(std::move(info));
    }
    catch (std::invalid_argument const&)
    {
        return true;
    }
    return false;
}
}

DOJO_TEST(Lifecycle_RefusesBadWorkerDescriptions)
{
    Json const worker = FixtureWorker();
    CHECK(!Refused(worker));
    auto with = [&worker](char const* key, Json value)
    {
        Json info = worker;
        info[key] = std::move(value);
        return info;
    };
    CHECK(Refused(with("actions", Json::array({"bolt", "noop"}))));
    CHECK(Refused(with("actions", Json::array())));
    CHECK(Refused(with("actors", Json::array())));
    CHECK(Refused(with("actors", Json::array({Json{{"slot", 1}, {"team", 0}}}))));
    CHECK(Refused(with("actors", Json::array({Json{{"slot", 0}, {"team", 0}}, Json{{"slot", 1}, {"team", 0}},
        Json{{"slot", 2}, {"team", 1}}}))));
    CHECK(Refused(with("scenario", "has space")));
    CHECK(Refused(with("extra", 1)));
}

DOJO_TEST(Lifecycle_OneSessionAtATime)
{
    Lifecycle lc(FixtureWorker());
    lc.Connect("s1");
    bool threw = false;
    try
    {
        lc.Connect("s2");
    }
    catch (std::logic_error const&)
    {
        threw = true;
    }
    CHECK(threw);
    lc.Disconnect();
    lc.Connect("s2");
    CHECK(lc.Session() == std::optional<std::string>("s2"));
}

DOJO_TEST(Lifecycle_RecentEpisodeIdsCannotBeReused)
{
    Lifecycle lc(FixtureWorker());
    lc.Connect("s1");
    Recv(lc, Json{{"v", 1}, {"type", "hello"}, {"id", "h"}, {"protocol", {{"min", 1}, {"max", 1}}}});
    int n = 0;
    auto reset = [&lc, &n](std::string const& episode)
    {
        Output out = Recv(lc, Json{{"v", 1}, {"type", "reset"}, {"id", "r" + std::to_string(n++)}, {"session", "s1"},
            {"episode", episode}, {"decision", 0}, {"scenario_seed", 1}});
        if (!out.effects.empty())
            lc.CompleteReset(lc.Generation(), lc.LastToken(), Json{{"ok", true}, {"observations", Open()}, {"sim_ms", 0}});
        return out;
    };
    for (std::size_t i = 0; i < kRecentEpisodes; ++i)
        CHECK(!reset("e" + std::to_string(i)).effects.empty());
    CHECK_EQ(reset("e0").send.at(0)["code"], "stale_episode");
    CHECK_EQ(reset("e" + std::to_string(kRecentEpisodes - 1)).send.at(0)["code"], "stale_episode");
    CHECK(!reset("fresh").effects.empty());
    CHECK(!reset("e0").effects.empty());
}

// Random interleavings of duplicates, conflicts, stale and premature requests, completions, faults and disconnects.
DOJO_TEST(Lifecycle_RandomSequencesNeverApplyADecisionTwice)
{
    Json const worker = FixtureWorker();
    for (unsigned seed = 0; seed < 40; ++seed)
    {
        std::mt19937 rng(seed);
        auto chance = [&rng]() { return std::uniform_real_distribution<double>(0.0, 1.0)(rng); };
        Bounds bounds;
        bounds.resultCache = 3;
        Lifecycle lc(worker, bounds);
        int serial = 0;
        std::set<std::pair<std::uint64_t, std::int64_t>> stepsBegun;
        std::set<std::uint64_t> tokens;
        struct Pending
        {
            std::string kind;
            std::uint64_t generation;
            std::uint64_t token;
        };
        std::vector<Pending> pending;
        std::vector<Json> history;
        std::vector<std::string> begunEpisodes;
        std::uint64_t lastGeneration = 0;
        int episodes = 0;
        auto connect = [&]()
        {
            ++serial;
            lc.Connect("s" + std::to_string(serial));
            begunEpisodes.clear();
            Recv(lc, Json{{"v", 1}, {"type", "hello"}, {"id", "h" + std::to_string(serial)},
                {"protocol", {{"min", 1}, {"max", 1}}}});
        };
        connect();
        for (int i = 0; i < 400; ++i)
        {
            double const choice = chance();
            std::string const session = lc.Session().value_or("s0");
            Output out;
            if (choice < 0.08)
            {
                out = lc.Disconnect();
                connect();
            }
            else if (choice < 0.25 && !pending.empty())
            {
                std::size_t const pick = rng() % pending.size();
                Pending p = pending[pick];
                pending.erase(pending.begin() + static_cast<std::ptrdiff_t>(pick));
                bool const forged = chance() < 0.2;   // a completion naming another operation's token
                if (forged)
                    p.token = p.token + 1 + rng() % 3;
                std::optional<std::string> const before = lc.InFlight();
                out = p.kind == "reset"
                    ? lc.CompleteReset(p.generation, p.token, Json{{"ok", true}, {"observations", Open()}, {"sim_ms", 0}})
                    : lc.CompleteStep(p.generation, p.token, StepDone(chance() < 0.05));
                if (forged && !out.stale)
                    CHECK_MSG(false, "a forged token completed an operation, seed " + std::to_string(seed));
                if (out.stale)
                    CHECK(lc.InFlight() == before);
            }
            else if (choice < 0.3)
                out = lc.Fault();
            else if (choice < 0.45 && !history.empty())
            {
                Json message = history[rng() % history.size()];
                if (chance() < 0.3 && message["type"] == "step")
                    message["actions"][0]["action"] = 2 - message["actions"][0]["action"].get<int>();
                out = Recv(lc, message);
            }
            else if (choice < 0.6)
            {
                ++episodes;
                int const reuse = chance() < 0.2 ? static_cast<int>(rng() % static_cast<unsigned>(std::max(episodes / 2, 1)))
                                                 : episodes;
                Json message{{"v", 1}, {"type", "reset"}, {"id", "r" + std::to_string(history.size())},
                    {"session", session}, {"episode", "e" + std::to_string(reuse)}, {"decision", 0},
                    {"scenario_seed", rng() % 100}};
                history.push_back(message);
                out = Recv(lc, message);
            }
            else
            {
                static constexpr int deltas[] = {0, 0, 0, 1, -1};
                std::int64_t const decision = std::max<std::int64_t>(lc.Decision().value_or(0) + deltas[rng() % 5], 0);
                Json message{{"v", 1}, {"type", "step"}, {"id", "r" + std::to_string(history.size())},
                    {"session", session}, {"episode", lc.Episode().value_or("e0")}, {"decision", decision},
                    {"actions", Json::array({Json{{"actor", 0}, {"action", rng() % 2 ? 2 : 0}},
                        Json{{"actor", 1}, {"action", 0}}})}};
                history.push_back(message);
                out = Recv(lc, message);
            }

            CHECK(lc.Generation() >= lastGeneration);
            lastGeneration = lc.Generation();
            if (out.stale)
                CHECK(out.send.empty());
            for (Json const& effect : out.effects)
            {
                if (effect["effect"] == "begin_step")
                {
                    auto const key = std::make_pair(effect["generation"].get<std::uint64_t>(),
                        effect["decision"].get<std::int64_t>());
                    CHECK_MSG(stepsBegun.insert(key).second, "a decision was applied twice, seed " + std::to_string(seed));
                    CHECK(tokens.insert(effect["token"].get<std::uint64_t>()).second);
                    pending.push_back({"step", key.first, effect["token"].get<std::uint64_t>()});
                }
                else if (effect["effect"] == "begin_reset")
                {
                    std::string const episode = effect["episode"].get<std::string>();
                    std::size_t const from = begunEpisodes.size() > kRecentEpisodes
                        ? begunEpisodes.size() - kRecentEpisodes : 0;
                    CHECK_MSG(std::find(begunEpisodes.begin() + static_cast<std::ptrdiff_t>(from), begunEpisodes.end(),
                        episode) == begunEpisodes.end(), "a recent episode was reset twice, seed " + std::to_string(seed));
                    begunEpisodes.push_back(episode);
                    CHECK(tokens.insert(effect["token"].get<std::uint64_t>()).second);
                    pending.push_back({"reset", effect["generation"].get<std::uint64_t>(),
                        effect["token"].get<std::uint64_t>()});
                }
            }
            for (Json const& m : out.send)
                CHECK(!HasNonFinite(m));
        }
    }
}

// The reviewer's probe: thousands of copies of one step while it is in flight. Only maxCopies are owed; the rest are
// answered busy at once, so a completion can never fan out without bound.
DOJO_TEST(Lifecycle_InFlightCopiesAreCapped)
{
    Lifecycle lc(FixtureWorker());
    lc.Connect("s1");
    Recv(lc, Json{{"v", 1}, {"type", "hello"}, {"id", "h"}, {"protocol", {{"min", 1}, {"max", 1}}}});
    Recv(lc, Json{{"v", 1}, {"type", "reset"}, {"id", "r1"}, {"session", "s1"}, {"episode", "e1"}, {"decision", 0},
        {"scenario_seed", 1}});
    lc.CompleteReset(lc.Generation(), lc.LastToken(), Json{{"ok", true}, {"observations", Open()}, {"sim_ms", 0}});
    Json const step{{"v", 1}, {"type", "step"}, {"id", "s9"}, {"session", "s1"}, {"episode", "e1"}, {"decision", 0},
        {"actions", Json::array({Json{{"actor", 0}, {"action", 0}}, Json{{"actor", 1}, {"action", 0}}})}};
    CHECK_EQ(Recv(lc, step).effects.size(), std::size_t(1));
    std::size_t busy = 0;
    for (int i = 0; i < 5000; ++i)
    {
        Output const out = Recv(lc, step);
        CHECK(out.send.size() <= 1);
        if (!out.send.empty() && out.send[0]["code"] == "busy")
            ++busy;
    }
    CHECK_EQ(busy, std::size_t(5001 - kMaxCopies));
    Output const done = lc.CompleteStep(lc.Generation(), lc.LastToken(), StepDone(false));
    CHECK_EQ(done.send.size(), kMaxCopies);
    for (Json const& m : done.send)
        CHECK_EQ(m["type"], "step_result");
}

// The reviewer's probe: with decision 1 in flight, a late completion of decision 0 (same generation) is stale.
DOJO_TEST(Lifecycle_ALateCompletionCannotFinishTheNextStep)
{
    Lifecycle lc(FixtureWorker());
    lc.Connect("s1");
    Recv(lc, Json{{"v", 1}, {"type", "hello"}, {"id", "h"}, {"protocol", {{"min", 1}, {"max", 1}}}});
    Recv(lc, Json{{"v", 1}, {"type", "reset"}, {"id", "r1"}, {"session", "s1"}, {"episode", "e1"}, {"decision", 0},
        {"scenario_seed", 1}});
    lc.CompleteReset(lc.Generation(), lc.LastToken(), Json{{"ok", true}, {"observations", Open()}, {"sim_ms", 0}});
    auto step = [](std::string const& id, int decision)
    {
        return Json{{"v", 1}, {"type", "step"}, {"id", id}, {"session", "s1"}, {"episode", "e1"}, {"decision", decision},
            {"actions", Json::array({Json{{"actor", 0}, {"action", 0}}, Json{{"actor", 1}, {"action", 0}}})}};
    };
    Recv(lc, step("a0", 0));
    std::uint64_t const first = lc.LastToken();
    CHECK_EQ(lc.CompleteStep(lc.Generation(), first, StepDone(false)).send.at(0)["id"], "a0");
    Recv(lc, step("a1", 1));
    Output const late = lc.CompleteStep(lc.Generation(), first, StepDone(false));
    CHECK(late.stale);
    CHECK(late.send.empty());
    CHECK(lc.InFlight() == std::optional<std::string>("a1"));
    CHECK(lc.Decision() == std::optional<std::int64_t>(1));
    CHECK_EQ(lc.CompleteStep(lc.Generation(), lc.LastToken(), StepDone(false)).send.at(0)["id"], "a1");
}

DOJO_TEST(Lifecycle_ErrorMessagesAreClipped)
{
    Lifecycle lc(FixtureWorker());
    lc.Connect("s1");
    Recv(lc, Json{{"v", 1}, {"type", "hello"}, {"id", "h"}, {"protocol", {{"min", 1}, {"max", 1}}}});
    Recv(lc, Json{{"v", 1}, {"type", "reset"}, {"id", "r1"}, {"session", "s1"}, {"episode", "e1"}, {"decision", 0},
        {"scenario_seed", 1}});
    std::string bad = std::string(3000, 'x') + "\xff\xfe";   // long and not even UTF-8
    Output const out = lc.CompleteReset(lc.Generation(), lc.LastToken(), Json{{"ok", false}, {"reason", bad}});
    CHECK_EQ(out.send.size(), std::size_t(1));
    std::string const message = out.send.at(0)["message"].get<std::string>();
    CHECK(message.size() <= kMaxTextBytes);
    CHECK(!Encode(out.send.at(0)).empty());
    CHECK_EQ(Clip("caf\xc3\xa9", 5), std::string("caf\xc3\xa9"));
    CHECK_EQ(Clip("a\xc3\xa9\xc3\xa9" "b", 5), std::string("a..."));   // never half a character
    CHECK_EQ(Clip("\xc3\xa9\xc3\xa9\xc3\xa9", 5), std::string("\xc3\xa9..."));
    CHECK_EQ(Clip("ok\xff", 10), std::string("ok?"));
    CHECK_EQ(Clip("\xed\xa0\x80", 10), std::string("???"));   // an encoded surrogate is not UTF-8
}
