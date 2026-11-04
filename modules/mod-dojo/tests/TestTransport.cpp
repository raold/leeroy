// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#include "DojoLifecycle.h"
#include "DojoTransport.h"
#include "TestClient.h"
#include "TestHarness.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <deque>
#include <fstream>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
#include <vector>

using namespace Dojo;
using namespace DojoTest;
using namespace std::chrono_literals;

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

Json StepDone()
{
    Json rewards = Json::array();
    for (int i = 0; i < 2; ++i)
        rewards.push_back(Json{{"actor", i}, {"terminal", 0}, {"shaping", 0.0}});
    return Json{{"elapsed_ms", 200}, {"substeps", 20}, {"sim_ms", 200}, {"observations", Open()},
        {"events", Json::array()}, {"rewards", rewards}, {"terminated", false}, {"truncated", false},
        {"outcome", nullptr}};
}

void NoLog(Transport::LogLevel, std::string const&)
{
}

// The worker thread of these tests: transport events into the lifecycle, its effects into immediate completions.
class LoopWorker
{
public:
    explicit LoopWorker(Transport::Options options = {})
        : _transport(NoLog, options), _lc(FixtureWorker(), options.bounds)
    {
    }

    ~LoopWorker()
    {
        Stop();
    }

    bool Start()
    {
        if (!_transport.Start("127.0.0.1", 0))
            return false;
        _thread = std::thread([this] { Run(); });
        return true;
    }

    void Stop()
    {
        _stop = true;
        if (_thread.joinable())
            _thread.join();
        _transport.Stop();
    }

    std::uint16_t Port() const { return _transport.BoundPort(); }
    Transport& GetTransport() { return _transport; }
    void Pause(bool paused) { _paused = paused; }
    void Hold(bool held) { _hold = held; }

    int Applied(std::string const& episode, std::int64_t decision)
    {
        std::lock_guard<std::mutex> lock(_mutex);
        auto const it = _applied.find({episode, decision});
        return it == _applied.end() ? 0 : it->second;
    }

    std::string State()
    {
        std::lock_guard<std::mutex> lock(_mutex);
        return std::string(_lc.StateString());
    }

    bool Aborted(std::string const& what)
    {
        std::lock_guard<std::mutex> lock(_mutex);
        return std::find(_aborted.begin(), _aborted.end(), what) != _aborted.end();
    }

private:
    void Run()
    {
        std::vector<TransportEvent> events;
        while (!_stop)
        {
            if (_paused)
            {
                std::this_thread::sleep_for(2ms);
                continue;
            }
            events.clear();
            std::size_t const n = _transport.Poll(events, 8);
            {
                std::lock_guard<std::mutex> lock(_mutex);
                for (TransportEvent& e : events)
                    Handle(e);
                while (!_hold && !_held.empty())
                {
                    Json const effect = _held.front();
                    _held.pop_front();
                    RunStep(effect);
                }
            }
            if (n == 0)
                std::this_thread::sleep_for(1ms);
        }
    }

    void Handle(TransportEvent const& e)
    {
        switch (e.kind)
        {
            case TransportEvent::Kind::Connected:
                _connection = e.connection;
                Apply(_lc.Connect("s" + std::to_string(e.connection)));
                break;
            case TransportEvent::Kind::Line:
                if (e.connection == _connection)
                    Apply(_lc.Handle(e.decoded));
                break;
            case TransportEvent::Kind::Disconnected:
                if (e.connection == _connection)
                {
                    Apply(_lc.Disconnect());
                    _connection = 0;
                }
                break;
        }
    }

    void Apply(Output const& out)
    {
        for (Json const& m : out.send)
            _transport.Send(_connection, Encode(m));
        for (Json const& effect : out.effects)
        {
            std::string const kind = effect["effect"].get<std::string>();
            if (kind == "begin_reset")
            {
                auto const gen = effect["generation"].get<std::uint64_t>();
                _episodes[gen] = effect["episode"].get<std::string>();
                Apply(_lc.CompleteReset(gen, Json{{"ok", true}, {"observations", Open()}, {"sim_ms", 0}}));
            }
            else if (kind == "begin_step")
            {
                if (_hold)
                    _held.push_back(effect);
                else
                    RunStep(effect);
            }
            else if (kind == "abort")
                _aborted.push_back(_episodes[effect["generation"].get<std::uint64_t>()] + ":"
                    + effect["reason"].get<std::string>());
            else if (kind == "close_connection")
                _transport.Close(_connection);
        }
    }

    void RunStep(Json const& effect)
    {
        auto const gen = effect["generation"].get<std::uint64_t>();
        if (!_lc.AcceptsCallback(gen))
        {
            Apply(_lc.CompleteStep(gen, Json{{"fault", "abandoned"}}));
            return;
        }
        ++_applied[{effect["episode"].get<std::string>(), effect["decision"].get<std::int64_t>()}];
        Apply(_lc.CompleteStep(gen, StepDone()));
    }

    Transport _transport;
    Lifecycle _lc;
    std::thread _thread;
    std::atomic<bool> _stop{false};
    std::atomic<bool> _paused{false};
    std::atomic<bool> _hold{false};
    std::mutex _mutex;
    std::uint64_t _connection = 0;
    std::map<std::pair<std::string, std::int64_t>, int> _applied;
    std::map<std::uint64_t, std::string> _episodes;
    std::vector<std::string> _aborted;
    std::deque<Json> _held;
};

Json Read(TestClient& c, std::chrono::milliseconds timeout = 2s)
{
    std::optional<std::string> line = c.ReadLine(timeout);
    return line ? Json::parse(*line) : Json(nullptr);
}

Json Ask(TestClient& c, Json const& message)
{
    c.Send(Encode(message));
    return Read(c);
}

Json Hello(std::string const& id = "h1")
{
    return Json{{"v", 1}, {"type", "hello"}, {"id", id}, {"protocol", {{"min", 1}, {"max", 1}}}};
}

Json Reset(std::string const& id, std::string const& session, std::string const& episode)
{
    return Json{{"v", 1}, {"type", "reset"}, {"id", id}, {"session", session}, {"episode", episode},
        {"decision", 0}, {"scenario_seed", 7}};
}

Json Step(std::string const& id, std::string const& session, std::string const& episode, std::int64_t decision)
{
    return Json{{"v", 1}, {"type", "step"}, {"id", id}, {"session", session}, {"episode", episode},
        {"decision", decision}, {"actions", Json::array({Json{{"actor", 0}, {"action", 2}},
            Json{{"actor", 1}, {"action", 0}}})}};
}

Json Status(std::string const& id)
{
    return Json{{"v", 1}, {"type", "status"}, {"id", id}};
}

// Connects, says hello and resets episode; returns the session.
std::string Begin(TestClient& c, std::uint16_t port, std::string const& episode)
{
    CHECK(c.Connect(port));
    Json const hello = Ask(c, Hello());
    CHECK_EQ(hello["type"], "hello_result");
    std::string const session = hello.value("session", "");
    CHECK_EQ(Ask(c, Reset("r0", session, episode))["type"], "reset_result");
    return session;
}
}

DOJO_TEST(Transport_RefusesNonLoopback)
{
    Transport t(NoLog);
    CHECK(!t.Start("0.0.0.0", 0));
    CHECK(!t.Start("192.168.1.69", 0));
    CHECK(!t.Running());
    CHECK(t.Start("localhost", 0));
    CHECK(t.BoundPort() != 0);
}

DOJO_TEST(Transport_EpisodeAndClose)
{
    LoopWorker w;
    CHECK(w.Start());
    TestClient c;
    std::string const session = Begin(c, w.Port(), "e1");
    for (int d = 0; d < 3; ++d)
    {
        Json const result = Ask(c, Step("s" + std::to_string(d), session, "e1", d));
        CHECK_EQ(result["type"], "step_result");
        CHECK_EQ(result["next_decision"], d + 1);
        CHECK_EQ(result["requested"][0]["action_id"], 2 * d);
    }
    Json const closed = Ask(c, Json{{"v", 1}, {"type", "close"}, {"id", "c1"}, {"session", session}});
    CHECK_EQ(closed["type"], "close_result");
    CHECK(c.WaitClosed(2s));
    CHECK(WaitFor([&] { return w.State() == "ready"; }, 2s));
    CHECK(w.Aborted("e1:closed"));
    for (int d = 0; d < 3; ++d)
        CHECK_EQ(w.Applied("e1", d), 1);
}

DOJO_TEST(Transport_DuplicateDeliveryAppliesOnce)
{
    LoopWorker w;
    CHECK(w.Start());
    TestClient c;
    std::string const session = Begin(c, w.Port(), "e1");
    std::string const line = Encode(Step("s1", session, "e1", 0));
    c.Send(line);
    c.Send(line);
    Json const first = Read(c);
    Json const second = Read(c);
    CHECK_EQ(first["type"], "step_result");
    CHECK(first == second);
    c.Send(line);
    CHECK(Read(c) == first);
    CHECK_EQ(w.Applied("e1", 0), 1);
    Json other = Step("s1", session, "e1", 0);
    other["actions"][0]["action"] = 0;
    CHECK_EQ(Ask(c, other)["code"], "conflicting_duplicate");
}

DOJO_TEST(Transport_OverlongLineIsAnsweredAndClosed)
{
    Transport::Options options;
    options.bounds.maxLineBytes = 1024;
    LoopWorker w(options);
    CHECK(w.Start());
    TestClient c;
    Begin(c, w.Port(), "e1");
    c.SendRaw(std::string(2000, 'x'));
    Json const answer = Read(c);
    CHECK_EQ(answer["code"], "line_too_long");
    CHECK(answer["id"].is_null());
    CHECK(c.WaitClosed(2s));
    CHECK(WaitFor([&] { return w.Aborted("e1:disconnect"); }, 2s));
    CHECK_EQ(w.State(), "ready");

    TestClient again;
    CHECK(again.Connect(w.Port()));
    std::string const atBound = R"({"v":1,"type":"close","id":"c","session":"s","reason":")";
    again.Send(atBound + std::string(1024 - atBound.size() - 2, 'z') + "\"}");
    CHECK_EQ(Read(again)["code"], "malformed");   // within the line bound, over the reason's
}

DOJO_TEST(Transport_SecondControllerIsRefused)
{
    LoopWorker w;
    CHECK(w.Start());
    TestClient first;
    std::string const session = Begin(first, w.Port(), "e1");
    TestClient second;
    CHECK(second.Connect(w.Port()));
    Json const refused = Read(second);
    CHECK_EQ(refused["code"], "busy");
    CHECK(second.WaitClosed(2s));
    CHECK_EQ(w.GetTransport().Stats().refused, 1u);
    CHECK_EQ(Ask(first, Step("s1", session, "e1", 0))["type"], "step_result");
}

DOJO_TEST(Transport_BackpressureDropsNothing)
{
    Transport::Options options;
    options.inboxCapacity = 2;
    LoopWorker w(options);
    CHECK(w.Start());
    TestClient c;
    CHECK(c.Connect(w.Port()));
    CHECK(WaitFor([&] { return w.GetTransport().Connection() != 0; }, 2s));
    w.Pause(true);
    std::string burst;
    for (int i = 0; i < 40; ++i)
        burst += Encode(Status("q" + std::to_string(i))) + "\n";
    c.SendRaw(burst);
    std::this_thread::sleep_for(300ms);
    CHECK(w.GetTransport().Stats().maxInboxLines <= 2);
    CHECK(w.GetTransport().Stats().readPauses > 0);
    w.Pause(false);
    for (int i = 0; i < 40; ++i)
        CHECK_EQ(Read(c)["id"], "q" + std::to_string(i));
    CHECK_EQ(w.GetTransport().Stats().linesIn, 40u);
}

DOJO_TEST(Transport_SendsForAnOldConnectionAreDropped)
{
    Transport t(NoLog);
    CHECK(t.Start("127.0.0.1", 0));
    std::vector<TransportEvent> events;
    auto next = [&]()
    {
        events.clear();
        WaitFor([&] { return t.Poll(events, 1) == 1; }, 2s);
        return events.empty() ? TransportEvent{} : events[0];
    };
    std::uint64_t first = 0;
    {
        TestClient c1;
        CHECK(c1.Connect(t.BoundPort()));
        TransportEvent const connected = next();
        CHECK(connected.kind == TransportEvent::Kind::Connected);
        first = connected.connection;
    }
    TransportEvent const gone = next();
    CHECK(gone.kind == TransportEvent::Kind::Disconnected && gone.connection == first);
    TestClient c2;
    CHECK(c2.Connect(t.BoundPort()));
    TransportEvent const connected = next();
    CHECK(connected.kind == TransportEvent::Kind::Connected && connected.connection != first);
    CHECK(!t.Send(first, R"({"id":"old"})"));
    CHECK(t.Send(connected.connection, R"({"id":"new"})"));
    CHECK_EQ(Read(c2)["id"], "new");
    CHECK_EQ(t.Stats().droppedSends, 1u);
    c2.SendRaw(R"({"v":1,"type":"step","id":"s1","act)");   // half a line, then gone: nothing is delivered
    c2.Close();
    TransportEvent const closed = next();
    CHECK(closed.kind == TransportEvent::Kind::Disconnected);
    events.clear();
    std::this_thread::sleep_for(50ms);
    CHECK_EQ(t.Poll(events, 8), std::size_t(0));
}

DOJO_TEST(Transport_ConnectionLossAfterCommitAbortsTheEpisode)
{
    LoopWorker w;
    CHECK(w.Start());
    std::string oldSession;
    {
        TestClient c;
        oldSession = Begin(c, w.Port(), "e1");
        c.Send(Encode(Step("s1", oldSession, "e1", 0)));
    }
    CHECK(WaitFor([&] { return w.Aborted("e1:disconnect"); }, 2s));
    CHECK_EQ(w.Applied("e1", 0), 1);
    TestClient c;
    CHECK(c.Connect(w.Port()));
    Json const hello = Ask(c, Hello());
    CHECK(hello["session"] != oldSession);
    CHECK_EQ(Ask(c, Step("s2", oldSession, "e1", 1))["code"], "bad_session");
    CHECK_EQ(Ask(c, Step("s3", hello["session"].get<std::string>(), "e1", 1))["code"], "not_ready");
    CHECK_EQ(Ask(c, Reset("r1", hello["session"].get<std::string>(), "e2"))["type"], "reset_result");
    CHECK_EQ(w.Applied("e1", 0), 1);
}

DOJO_TEST(Transport_CloseWithAStepInFlight)
{
    LoopWorker w;
    CHECK(w.Start());
    TestClient c;
    std::string const session = Begin(c, w.Port(), "e1");
    w.Hold(true);
    c.Send(Encode(Step("s1", session, "e1", 0)));
    CHECK(WaitFor([&] { return w.State() == "stepping"; }, 2s));
    c.Send(Encode(Json{{"v", 1}, {"type", "close"}, {"id", "c1"}, {"session", session}}));
    Json const aborted = Read(c);
    CHECK_EQ(aborted["id"], "s1");
    CHECK_EQ(aborted["code"], "aborted");
    CHECK_EQ(aborted["reason"], "closed");
    CHECK_EQ(Read(c)["type"], "close_result");
    CHECK(c.WaitClosed(2s));
    CHECK_EQ(w.State(), "closing");
    w.Hold(false);
    CHECK(WaitFor([&] { return w.State() == "ready"; }, 2s));
    CHECK_EQ(w.Applied("e1", 0), 0);
}
