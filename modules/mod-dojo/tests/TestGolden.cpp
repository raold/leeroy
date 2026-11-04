// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

// The shared golden fixture, tests/fixtures/dojo_protocol.jsonl, which dojo/tests/test_contract_golden.py also runs.

#include "DojoLifecycle.h"
#include "DojoProtocol.h"
#include "TestHarness.h"

#include <fstream>
#include <string>
#include <vector>

using namespace Dojo;

namespace
{
std::vector<Json> LoadCases()
{
    std::ifstream in(std::string(DOJO_TEST_FIXTURES) + "/dojo_protocol.jsonl", std::ios::binary);
    std::vector<Json> cases;
    std::string line;
    while (std::getline(in, line))
        if (!line.empty())
            cases.push_back(Json::parse(line));
    return cases;
}

Json WithoutMessage(Json message)
{
    if (message.is_object() && message.value("type", "") == "error")
    {
        CHECK(message.contains("message") && message["message"].is_string());
        message.erase("message");
    }
    return message;
}

std::string LineOf(Json const& holder)
{
    return holder.contains("line") ? holder["line"].get<std::string>() : Encode(holder["json"]);
}

void RunDecode(Json const& c)
{
    std::string const name = c["name"].get<std::string>();
    Bounds const bounds = Bounds::FromWire(c.value("bounds", Json::object()));
    Decoded const d = DecodeRequest(LineOf(c), bounds);
    Json const& expect = c["expect"];
    if (expect["code"].is_null())
    {
        CHECK_MSG(d.request.has_value(), name + ": " + d.message);
        if (d.request)
        {
            CHECK_MSG(d.request->type == expect["type"].get<std::string>(), name);
            CHECK_MSG(d.request->id == expect["id"].get<std::string>(), name);
        }
        return;
    }
    CHECK_MSG(!d.request.has_value(), name);
    CHECK_MSG(d.code == expect["code"].get<std::string>(), name + ": got " + d.code);
    Json const id = d.id ? Json(*d.id) : Json(nullptr);
    CHECK_MSG(id == expect["id"], name + ": got id " + id.dump());
}

void RunSession(Json const& c, Json const& worker)
{
    std::string const name = c["name"].get<std::string>();
    Bounds const bounds = Bounds::FromWire(c.value("bounds", Json::object()));
    Lifecycle lc(worker, bounds, c.value("booted", true));
    Json const& script = c["script"];
    for (std::size_t i = 0; i < script.size(); ++i)
    {
        Json const& op = script[i];
        std::string const kind = op["op"].get<std::string>();
        std::string const where = name + " #" + std::to_string(i) + " " + kind;
        if (kind == "callback")
        {
            CHECK_MSG(lc.AcceptsCallback(op["generation"].get<std::uint64_t>()) == op["accepted"].get<bool>(), where);
            continue;
        }
        Output out;
        if (kind == "connect")
            out = lc.Connect(op["session"].get<std::string>());
        else if (kind == "disconnect")
            out = lc.Disconnect();
        else if (kind == "recv")
            out = lc.Handle(DecodeRequest(LineOf(op), bounds));
        else if (kind == "complete_reset")
            out = lc.CompleteReset(op["generation"].get<std::uint64_t>(), op["payload"]);
        else if (kind == "complete_step")
            out = lc.CompleteStep(op["generation"].get<std::uint64_t>(), op["payload"]);
        else if (kind == "fault")
            out = lc.Fault();
        else if (kind == "drained")
            out = lc.Drained();
        else if (kind == "boot_complete")
            out = lc.BootComplete();
        else
        {
            CHECK_MSG(false, where + ": unknown op");
            continue;
        }

        Json sent = Json::array();
        for (Json const& m : out.send)
            sent.push_back(WithoutMessage(Json::parse(Encode(m))));
        Json const wantSend = op.value("send", Json::array());
        CHECK_MSG(sent == wantSend, where + "\n    sent " + sent.dump() + "\n    want " + wantSend.dump());
        Json effects = Json::array();
        for (Json const& e : out.effects)
            effects.push_back(e);
        Json const wantEffects = op.value("effects", Json::array());
        CHECK_MSG(effects == wantEffects,
            where + "\n    effects " + effects.dump() + "\n    want " + wantEffects.dump());
        CHECK_MSG(out.stale == op.value("stale", false), where);
        if (op.contains("state"))
            CHECK_MSG(lc.StateString() == op["state"].get<std::string>(),
                where + ": state " + std::string(lc.StateString()));
    }
}
}

DOJO_TEST(Golden_EveryFixtureCase)
{
    std::vector<Json> const cases = LoadCases();
    CHECK(!cases.empty());
    if (cases.empty())
        return;
    CHECK(cases[0]["kind"] == "defaults");
    Json const worker = cases[0]["worker"];
    int decodes = 0;
    int sessions = 0;
    for (std::size_t i = 1; i < cases.size(); ++i)
    {
        if (cases[i]["kind"] == "decode")
        {
            ++decodes;
            RunDecode(cases[i]);
        }
        else
        {
            ++sessions;
            RunSession(cases[i], worker);
        }
    }
    CHECK(decodes >= 40);
    CHECK(sessions >= 25);
}
