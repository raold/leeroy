// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyBridge.h"
#include "TestClient.h"
#include "TestHarness.h"

#include <chrono>
#include <string>
#include <vector>
#include <nlohmann/json.hpp>

using namespace Leeroy;
using namespace LeeroyTest;
using namespace std::chrono_literals;

namespace
{
void NoLog(Bridge::LogLevel, std::string const&)
{
}
}

LEEROY_TEST(Bridge_RefusesNonLoopback)
{
    Bridge b(NoLog);
    CHECK(!b.Start("0.0.0.0", 0));
    CHECK(!b.Running());
}

LEEROY_TEST(Bridge_GreetingPrecedesBufferedEvents)
{
    Bridge b(NoLog);
    CHECK(b.Start("127.0.0.1", 0));
    b.PublishEvent(R"({"type":"zone"})", true);
    TestClient c;
    CHECK(c.Connect(b.BoundPort()));
    CHECK(WaitFor([&] { return b.ConnectionSerial() == 1; }, 1s));
    CHECK(!c.ReadLine(100ms));                                  // nothing before the greeting
    b.Greet(1, "HELLO", "CAST");
    CHECK_EQ(NextLine(c), "HELLO"); CHECK_EQ(NextLine(c), "CAST"); CHECK_EQ(NextLine(c), R"({"type":"zone"})");
}

LEEROY_TEST(Bridge_SecondMindRefused)
{
    Bridge b(NoLog);
    b.Start("127.0.0.1", 0);
    TestClient c1;
    c1.Connect(b.BoundPort());
    WaitFor([&] { return b.Connected(); }, 1s);
    TestClient c2;
    CHECK(c2.Connect(b.BoundPort()));
    auto j = nlohmann::json::parse(NextLine(c2));
    CHECK_EQ(j["type"], "error"); CHECK_EQ(j["message"], "another mind is connected");
    CHECK(c2.WaitClosed(1s)); CHECK(b.Connected()); CHECK_EQ(b.ConnectionSerial(), 1u);
}

LEEROY_TEST(Bridge_CommandsQueuedErrorsAnswered)
{
    Bridge b(NoLog);
    b.Start("127.0.0.1", 0);
    TestClient c;
    c.Connect(b.BoundPort());
    WaitFor([&] { return b.ConnectionSerial() == 1; }, 1s);
    b.Greet(1, "H", "C");
    NextLine(c);
    NextLine(c);
    c.Send(R"({"v":1,"type":"ping","id":"p"})");
    std::vector<Command> cmds;
    CHECK(WaitFor([&] { return b.PopCommands(cmds, 10) == 1; }, 1s)); CHECK_EQ(cmds[0].id, "p");
    c.Send("nope");
    CHECK_EQ(nlohmann::json::parse(NextLine(c))["message"], "bad json");
    c.Send(R"({"v":1,"type":"say","id":"s","guid":4,"channel":"guild"})");
    auto a = nlohmann::json::parse(NextLine(c));
    CHECK_EQ(a["type"], "ack"); CHECK_EQ(a["id"], "s");
    CHECK_EQ(a["ok"], false); CHECK_EQ(a["error"], "bad field: text");
    b.Reply("R1");
    CHECK_EQ(NextLine(c), "R1");
}

LEEROY_TEST(Bridge_OverlongLineCloses)
{
    Bridge b(NoLog);
    b.Start("127.0.0.1", 0);
    TestClient c;
    c.Connect(b.BoundPort());
    WaitFor([&] { return b.Connected(); }, 1s);
    c.Send(std::string(70000, 'x'));
    CHECK_EQ(nlohmann::json::parse(NextLine(c))["message"], "line too long");
    CHECK(c.WaitClosed(1s)); CHECK(WaitFor([&] { return !b.Connected(); }, 1s));
}

LEEROY_TEST(Bridge_FirstLineNotJsonClosesConnection)
{
    Bridge b(NoLog);
    b.Start("127.0.0.1", 0);
    TestClient c;
    c.Connect(b.BoundPort());
    WaitFor([&] { return b.ConnectionSerial() == 1; }, 1s);
    c.Send("POST / HTTP/1.1\r");
    c.Send(R"({"v":1,"type":"ping","id":"p"})");
    auto j = nlohmann::json::parse(NextLine(c));
    CHECK_EQ(j["type"], "error"); CHECK_EQ(j["message"], "bad json"); CHECK(!j.contains("id"));
    CHECK(c.WaitClosed(1s)); CHECK(WaitFor([&] { return !b.Connected(); }, 1s));
    b.Greet(1, "H", "C");
    std::vector<Command> cmds;
    CHECK_EQ(b.PopCommands(cmds, 10), 0u);
    TestClient c2;
    CHECK(c2.Connect(b.BoundPort()));
    CHECK(WaitFor([&] { return b.ConnectionSerial() == 2; }, 1s));
    b.Greet(2, "H2", "C2");
    CHECK_EQ(NextLine(c2), "H2"); CHECK_EQ(NextLine(c2), "C2");
}

LEEROY_TEST(Bridge_LaterBadLineKeepsConnection)
{
    Bridge b(NoLog);
    b.Start("127.0.0.1", 0);
    TestClient c;
    c.Connect(b.BoundPort());
    WaitFor([&] { return b.ConnectionSerial() == 1; }, 1s);
    b.Greet(1, "H", "C");
    NextLine(c);
    NextLine(c);
    c.Send(R"({"v":1,"type":"ping","id":"a"})");
    c.Send("garbage");
    CHECK_EQ(nlohmann::json::parse(NextLine(c))["message"], "bad json");
    c.Send(R"({"v":1,"type":"ping","id":"b"})");
    std::vector<Command> cmds;
    CHECK(WaitFor([&] { b.PopCommands(cmds, 10); return cmds.size() == 2; }, 1s));
    CHECK_EQ(cmds[0].id, "a"); CHECK_EQ(cmds[1].id, "b"); CHECK(b.Connected());
}

LEEROY_TEST(Bridge_ReconnectNeedsFreshGreeting)
{
    Bridge b(NoLog);
    b.Start("127.0.0.1", 0);
    {
        TestClient c;
        c.Connect(b.BoundPort());
        WaitFor([&] { return b.ConnectionSerial() == 1; }, 1s);
        b.Greet(1, "H", "C");
        c.Close();
    }
    CHECK(WaitFor([&] { return !b.Connected(); }, 1s));
    b.PublishEvent("E", false);
    TestClient c2;
    c2.Connect(b.BoundPort());
    WaitFor([&] { return b.ConnectionSerial() == 2; }, 1s);
    b.Greet(1, "STALE", "STALE"); CHECK(!c2.ReadLine(100ms));     // wrong serial is ignored
    b.Greet(2, "H2", "C2");
    CHECK_EQ(NextLine(c2), "H2"); CHECK_EQ(NextLine(c2), "C2"); CHECK_EQ(NextLine(c2), "E");
}

LEEROY_TEST(Bridge_StopWithMindConnected)
{
    Bridge b(NoLog);
    b.Start("127.0.0.1", 0);
    TestClient c;
    c.Connect(b.BoundPort());
    WaitFor([&] { return b.Connected(); }, 1s);
    auto t0 = std::chrono::steady_clock::now();
    b.Stop();
    CHECK(std::chrono::steady_clock::now() - t0 < 2s); CHECK(c.WaitClosed(1s)); CHECK(!b.Running());
}
