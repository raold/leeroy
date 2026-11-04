// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#include "DojoProtocol.h"
#include "TestHarness.h"

#include <limits>
#include <string>

using namespace Dojo;

DOJO_TEST(Protocol_InvalidUtf8IsMalformed)
{
    Decoded const d = DecodeRequest("{\"v\":1,\"type\":\"status\",\"id\":\"q\",\"session\":\"\xff\"}");
    CHECK(!d.request);
    CHECK_EQ(d.code, std::string(Err::Malformed));
}

DOJO_TEST(Protocol_LineLimitCountsBytes)
{
    std::string const line = "{\"v\":1,\"type\":\"close\",\"id\":\"c\",\"session\":\"s\",\"reason\":\""
        "\xc3\xa9\xc3\xa9\xc3\xa9\"}";
    Bounds tight;
    tight.maxLineBytes = line.size() - 1;
    CHECK_EQ(DecodeRequest(line, tight).code, std::string(Err::LineTooLong));
    tight.maxLineBytes = line.size();
    CHECK(DecodeRequest(line, tight).request.has_value());
}

DOJO_TEST(Protocol_DefaultBoundIs256KiB)
{
    static_assert(kMaxLineBytes == 262144);
    std::string const head = "{\"v\":1,\"type\":\"close\",\"id\":\"c\",\"session\":\"s\",\"reason\":\"";
    std::string line = head + std::string(kMaxLineBytes - head.size() - 2, 'x') + "\"}";
    CHECK_EQ(line.size(), kMaxLineBytes);
    CHECK_EQ(DecodeRequest(line).code, std::string(Err::Malformed));   // fits the line, not the reason
    CHECK_EQ(DecodeRequest(line + " ").code, std::string(Err::LineTooLong));
}

DOJO_TEST(Protocol_FingerprintIgnoresKeyOrderAndSpacing)
{
    auto const a = DecodeRequest(R"({"v":1,"type":"status","id":"q","session":"s1"})");
    auto const b = DecodeRequest(R"({ "session" : "s1", "id": "q", "type": "status", "v": 1 })");
    auto const c = DecodeRequest(R"({"v":1,"type":"status","id":"q","session":"s2"})");
    CHECK(a.request && b.request && c.request);
    if (a.request && b.request && c.request)
    {
        CHECK_EQ(a.request->fingerprint, b.request->fingerprint);
        CHECK(a.request->fingerprint != c.request->fingerprint);
    }
}

DOJO_TEST(Protocol_DeepNestingIsMalformed)
{
    std::string line = R"({"v":1,"type":"status","id":"q","session":)";
    line += std::string(5000, '[') + std::string(5000, ']') + "}";
    Decoded const d = DecodeRequest(line);
    CHECK_EQ(d.code, std::string(Err::Malformed));
    CHECK(!d.id);
}

DOJO_TEST(Protocol_EncodeIsOneCompactLine)
{
    Json const j{{"a", "x\ny"}, {"b", Json::array({1, 2.5})}};
    std::string const text = Encode(j);
    CHECK(text.find('\n') == std::string::npos);
    CHECK_EQ(text, std::string(R"({"a":"x\ny","b":[1,2.5]})"));
    CHECK(HasNonFinite(Json{{"x", Json::array({1.0, std::numeric_limits<double>::quiet_NaN()})}}));
    CHECK(!HasNonFinite(j));
    bool threw = false;
    try
    {
        Encode(Json(std::string("bad\xff")));
    }
    catch (Json::type_error const&)
    {
        threw = true;
    }
    CHECK(threw);
}

DOJO_TEST(Protocol_ErrorShape)
{
    Json const e = MakeError(Err::Busy, std::string("r1"), "m");
    CHECK_EQ(e.dump(), std::string(R"({"code":"busy","id":"r1","message":"m","ok":false,"type":"error","v":1})"));
    CHECK(MakeError(Err::Malformed, std::nullopt, "m")["id"].is_null());
    CHECK_EQ(MakeError(Err::Aborted, std::nullopt, "m", AbortReason::Fault)["reason"], "fault");
}

DOJO_TEST(Protocol_BoundsRoundTrip)
{
    Bounds b;
    b.maxEvents = 3;
    CHECK(Bounds::FromWire(b.ToWire()) == b);
    CHECK_EQ(b.ToWire()["in_flight"], 1);
    CHECK_EQ(b.ToWire()["recent_episodes"], kRecentEpisodes);
}

DOJO_TEST(Protocol_IntegerRanges)
{
    CHECK(IsInt(Json(kMaxSeed), 0, kMaxSeed));
    CHECK(!IsInt(Json(kMaxSeed + 1), 0, kMaxSeed));
    CHECK(!IsInt(Json(-1), 0, 10));
    CHECK(!IsInt(Json(true), 0, 10));
    CHECK(!IsInt(Json(1.0), 0, 10));
    CHECK(IsInt(Json(std::numeric_limits<std::uint64_t>::max()), 0, std::numeric_limits<std::int64_t>::max()) == false);
    CHECK(IsNumber(Json(1)));
    CHECK(!IsNumber(Json(false)));
    CHECK(!IsNumber(Json(std::numeric_limits<double>::infinity())));
    CHECK(IsIdText(std::string(64, 'a')));
    CHECK(!IsIdText(std::string(65, 'a')));
    CHECK(!IsIdText("a b"));
}
