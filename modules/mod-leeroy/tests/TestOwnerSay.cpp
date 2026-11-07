// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyOwnerSay.h"
#include "TestHarness.h"

#include <cstdint>
#include <fstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

using namespace Leeroy::Guild;
using json = nlohmann::json;

namespace
{
std::vector<json> Rows(char const* name)
{
    std::ifstream file(std::string(LEEROY_TEST_FIXTURES "/") + name);
    if (!file)
        throw std::runtime_error(std::string("cannot open fixture ") + name);
    std::vector<json> rows;
    for (std::string line; std::getline(file, line);)
        if (!line.empty())
            rows.push_back(json::parse(line));
    if (rows.empty())
        throw std::runtime_error(std::string("empty fixture ") + name);
    return rows;
}

constexpr std::uint32_t kHuman = 1, kMember = 1101;

OwnerSayCmd Line(std::string channel, std::string text = "hi", std::uint32_t toGuid = 0)
{
    return {std::move(channel), std::move(text), toGuid};
}

// He is in his guild and leads a raid; the whisper target is an online cast member.
OwnerSayFacts Everything()
{
    OwnerSayFacts facts;
    facts.humanGuid = kHuman;
    facts.inGuild = facts.inGroup = facts.inRaid = facts.targetCast = facts.targetOnline = true;
    return facts;
}

std::string Error(OwnerSayCmd const& cmd, OwnerSayFacts const& facts, OwnerSayLimiter const& limiter = {},
    std::uint64_t nowMs = 0)
{
    return std::string(CheckOwnerSay(cmd, facts, limiter, nowMs).error);
}
}

LEEROY_TEST(OwnerSay_ParseFollowsTheFixture)
{
    std::size_t valid = 0, invalid = 0;
    for (auto const& row : Rows("owner_say_protocol.jsonl"))
    {
        std::string const name = row.at("name").get<std::string>();
        json const& msg = row.at("msg");
        std::string error = "stale";
        auto const cmd = ParseOwnerSay(msg, error);
        if (row.at("valid").get<bool>())
        {
            ++valid;
            if (!cmd)
            {
                LeeroyTest::Fail(__FILE__, __LINE__, name + " refused: " + error);
                continue;
            }
            CHECK_EQ(error, std::string());
            CHECK_EQ(cmd->channel, msg.at("channel").get<std::string>());
            CHECK_EQ(cmd->text, msg.at("text").get<std::string>());
            CHECK_EQ(cmd->toGuid, msg.value("to_guid", std::uint32_t(0)));
        }
        else
        {
            ++invalid;
            if (cmd)
                LeeroyTest::Fail(__FILE__, __LINE__, name + " parsed");
            CHECK_EQ(name + ": " + error, name + ": " + row.at("error").get<std::string>());
        }
    }
    CHECK_EQ(valid, std::size_t(5));
    CHECK_EQ(invalid, std::size_t(13));
    CHECK_EQ(kOwnerSayType, std::string_view("owner_say"));
}

LEEROY_TEST(OwnerSay_TextRules)
{
    std::size_t rows = 0;
    for (auto const& row : Rows("owner_say_text.jsonl"))
    {
        ++rows;
        std::string const name = row.at("name").get<std::string>();
        std::string const verdict = row.at("verdict").get<std::string>();
        auto const [error, sent] = OwnerSayText(row.at("text").get<std::string>());
        CHECK_EQ(name + ": " + std::string(error), name + ": " + (verdict == "ok" ? std::string() : verdict));
        CHECK_EQ(name + ": " + sent, name + ": " + row.value("sent", std::string()));
        if (verdict == "ok")
            CHECK(row.contains("sent"));
    }
    CHECK_EQ(rows, std::size_t(17));
}

LEEROY_TEST(OwnerSay_ChannelNeedsHisGuildOrGroup)
{
    OwnerSayFacts facts = Everything();
    for (char const* channel : {"guild", "officer", "party", "raid", "whisper"})
        CHECK_EQ(Error(Line(channel, "hi", std::string_view(channel) == "whisper" ? kMember : 0), facts),
            std::string());

    facts.inGuild = false;
    CHECK_EQ(Error(Line("guild"), facts), std::string("not_in_guild"));
    CHECK_EQ(Error(Line("officer"), facts), std::string("not_in_guild"));
    CHECK_EQ(Error(Line("party"), facts), std::string());
    CHECK_EQ(Error(Line("whisper", "hi", kMember), facts), std::string());

    facts = Everything();
    facts.inGroup = facts.inRaid = false;
    CHECK_EQ(Error(Line("party"), facts), std::string("not_in_group"));
    CHECK_EQ(Error(Line("raid"), facts), std::string("not_in_raid"));
    CHECK_EQ(Error(Line("guild"), facts), std::string());

    // A party, not a raid: party passes, raid does not. Inside a raid, party still passes (his own subgroup).
    facts = Everything();
    facts.inRaid = false;
    CHECK_EQ(Error(Line("party"), facts), std::string());
    CHECK_EQ(Error(Line("raid"), facts), std::string("not_in_raid"));
    CHECK_EQ(Error(Line("party"), Everything()), std::string());
    CHECK_EQ(Error(Line("raid"), Everything()), std::string());

    OwnerSayVerdict const verdict = CheckOwnerSay(Line("raid", "  pull   in 5 "), Everything(), {}, 0);
    CHECK_EQ(std::string(verdict.error), std::string());
    CHECK_EQ(verdict.text, std::string("pull in 5"));
}

LEEROY_TEST(OwnerSay_WhisperOnlyToAnOnlineCastMember)
{
    OwnerSayFacts facts = Everything();
    CHECK_EQ(Error(Line("whisper", "hi", kHuman), facts), std::string("self"));

    // He is never in the cast, so self is named before not_cast.
    facts.targetCast = facts.targetOnline = false;
    CHECK_EQ(Error(Line("whisper", "hi", kHuman), facts), std::string("self"));
    CHECK_EQ(Error(Line("whisper", "hi", kMember), facts), std::string("not_cast"));

    facts.targetCast = true;
    CHECK_EQ(Error(Line("whisper", "hi", kMember), facts), std::string("target_offline"));

    facts.targetOnline = true;
    CHECK_EQ(Error(Line("whisper", "hi", kMember), facts), std::string());

    // A whisper needs neither his guild nor a group.
    facts.inGuild = facts.inGroup = facts.inRaid = false;
    CHECK_EQ(Error(Line("whisper", "hi", kMember), facts), std::string());
}

LEEROY_TEST(OwnerSay_LimiterGapAndWindow)
{
    OwnerSayLimiter limiter;
    CHECK(limiter.Allows(0));
    limiter.Record(0);
    CHECK(!limiter.Allows(0));
    CHECK(!limiter.Allows(1499));
    CHECK(limiter.Allows(1500));

    OwnerSayLimiter window;
    for (std::uint64_t i = 0; i < kOwnerSayPerWindow; ++i)
    {
        CHECK(window.Allows(i * kOwnerSayGapMs));
        window.Record(i * kOwnerSayGapMs);
    }
    CHECK(!window.Allows(12000));
    CHECK(!window.Allows(29999));
    CHECK(window.Allows(30000));
    window.Record(30000);
    // 1500..10500 and 30000 are eight lines; the gap holds at 31499, and at 31500 the line at 1500 leaves the window.
    CHECK(!window.Allows(31499));
    CHECK(window.Allows(31500));

    CHECK_EQ(Error(Line("guild"), Everything(), limiter, 1499), std::string("rate_limited"));
    CHECK_EQ(Error(Line("guild"), Everything(), limiter, 1500), std::string());
    CHECK_EQ(Error(Line("guild"), Everything(), window, 29999), std::string("rate_limited"));
}

LEEROY_TEST(OwnerSay_ContentErrorsComeFirstAndSpendNothing)
{
    OwnerSayLimiter limited;
    limited.Record(0);
    CHECK(!limited.Allows(100));
    CHECK_EQ(Error(Line("guild", "   "), Everything(), limited, 100), std::string("empty_text"));
    CHECK_EQ(Error(Line("guild", std::string(256, 'a')), Everything(), limited, 100), std::string("too_long"));
    CHECK_EQ(Error(Line("guild", " .gm on"), Everything(), limited, 100), std::string("command_prefix"));
    CHECK_EQ(Error(Line("guild", "hi"), Everything(), limited, 100), std::string("rate_limited"));

    // Text, then the channel's facts, then the whisper target, the limiter last.
    OwnerSayFacts nothing;
    nothing.humanGuid = kHuman;
    CHECK_EQ(Error(Line("guild", ""), nothing, limited, 100), std::string("empty_text"));
    CHECK_EQ(Error(Line("guild"), nothing, limited, 100), std::string("not_in_guild"));
    CHECK_EQ(Error(Line("raid"), nothing, limited, 100), std::string("not_in_raid"));
    CHECK_EQ(Error(Line("whisper", "hi", kMember), nothing, limited, 100), std::string("not_cast"));

    // Check never records: two passing checks at the same instant both pass, and the limiter is as it was.
    OwnerSayLimiter fresh;
    CHECK_EQ(Error(Line("guild"), Everything(), fresh, 5000), std::string());
    CHECK_EQ(Error(Line("guild"), Everything(), fresh, 5000), std::string());
    CHECK(fresh.Allows(5000));
    CHECK_EQ(Error(Line("guild"), Everything(), limited, 1500), std::string());
    CHECK(!limited.Allows(1499));
    CHECK(limited.Allows(1500));
}
