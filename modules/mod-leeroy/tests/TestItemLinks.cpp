// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyItemLinks.h"
#include "LeeroyText.h"
#include "TestHarness.h"

#include <map>
#include <string>

using namespace Leeroy;

namespace
{
ItemLookup Items(std::map<std::uint32_t, ItemLinkInfo> items)
{
    return [items = std::move(items)](std::uint32_t id) -> std::optional<ItemLinkInfo> {
        auto const it = items.find(id);
        if (it == items.end())
            return std::nullopt;
        return it->second;
    };
}

ItemLookup Armory()
{
    return Items({
        {49623, {"Shadowmourne", 5}},
        {50363, {"Deathbringer's Will", 4}},
        {2589, {"Linen Cloth", 1}},
        {6948, {"Hearthstone", 1}},
        {1, {"Pipe|Name", 2}},
        {2, {"|||", 3}},
        {3, {"Odd Quality", 9}},
    });
}

std::string const kShadowmourne = "|cffff8000|Hitem:49623:0:0:0:0:0:0:0:80|h[Shadowmourne]|h|r";
std::string const kWill = "|cffa335ee|Hitem:50363:0:0:0:0:0:0:0:80|h[Deathbringer's Will]|h|r";

std::size_t Count(std::string const& text, std::string const& what)
{
    std::size_t n = 0;
    for (std::size_t at = text.find(what); at != std::string::npos; at = text.find(what, at + what.size()))
        ++n;
    return n;
}
}

LEEROY_TEST(ItemLinks_QualityColours)
{
    CHECK_EQ(ItemQualityColor(0), 0xff9d9d9du);
    CHECK_EQ(ItemQualityColor(1), 0xffffffffu);
    CHECK_EQ(ItemQualityColor(2), 0xff1eff00u);
    CHECK_EQ(ItemQualityColor(3), 0xff0070ddu);
    CHECK_EQ(ItemQualityColor(4), 0xffa335eeu);
    CHECK_EQ(ItemQualityColor(5), 0xffff8000u);
    CHECK_EQ(ItemQualityColor(6), 0xffe6cc80u);
    CHECK_EQ(ItemQualityColor(7), 0xffe6cc80u);
    CHECK_EQ(ItemQualityColor(8), 0u);
}

LEEROY_TEST(ItemLinks_BuildsTheClientsNineFields)
{
    CHECK_EQ(BuildItemLink(49623, {"Shadowmourne", 5}, 80), kShadowmourne);
    CHECK_EQ(BuildItemLink(2589, {"Linen Cloth", 1}, 83),
        std::string("|cffffffff|Hitem:2589:0:0:0:0:0:0:0:83|h[Linen Cloth]|h|r"));
    CHECK_EQ(BuildItemLink(1, {"Grey", 0}, 1), std::string("|cff9d9d9d|Hitem:1:0:0:0:0:0:0:0:1|h[Grey]|h|r"));
    // A name never carries a pipe or a bracket into the link; nothing left, no link.
    CHECK_EQ(BuildItemLink(7, {"Bad|Name [x]", 2}, 80), std::string("|cff1eff00|Hitem:7:0:0:0:0:0:0:0:80|h[BadName x]|h|r"));
    CHECK_EQ(BuildItemLink(7, {"||", 2}, 80), std::string());
    CHECK_EQ(BuildItemLink(7, {"Odd", 8}, 80), std::string());
    CHECK_EQ(BuildItemLink(0, {"Zero", 2}, 80), std::string());
}

LEEROY_TEST(ItemLinks_ExpandsPlaceholders)
{
    auto const items = Armory();
    CHECK_EQ(SanitizeChatTextWithItemLinks("finally got {{item:49623}} lol", items, 80),
        "finally got " + kShadowmourne + " lol");
    CHECK_EQ(SanitizeChatTextWithItemLinks("{{item:50363}}", items, 80), kWill);
    CHECK_EQ(SanitizeChatTextWithItemLinks("{{item:49623}} and {{item:50363}}!", items, 80),
        kShadowmourne + " and " + kWill + "!");
}

LEEROY_TEST(ItemLinks_AtMostTwoLinks)
{
    auto const items = Armory();
    std::string const out =
        SanitizeChatTextWithItemLinks("{{item:49623}} {{item:50363}} {{item:2589}} {{item:6948}}", items, 80);
    CHECK_EQ(out, kShadowmourne + " " + kWill + " [Linen Cloth] [Hearthstone]");
    CHECK_EQ(Count(out, "|H"), 2u);
    CHECK_EQ(Count(SanitizeChatTextWithItemLinks("{{item:49623}} {{item:49623}} {{item:49623}}", items, 80, 255, 1),
                 "|H"),
        1u);
}

LEEROY_TEST(ItemLinks_UnknownIdIsPlainText)
{
    auto const items = Armory();
    CHECK_EQ(SanitizeChatTextWithItemLinks("got {{item:99999}} today", items, 80), std::string("got [unknown item] today"));
    CHECK_EQ(SanitizeChatTextWithItemLinks("{{item:2}}", items, 80), std::string("[unknown item]"));
    CHECK_EQ(SanitizeChatTextWithItemLinks("{{item:3}}", items, 80), std::string("[Odd Quality]"));
    CHECK_EQ(SanitizeChatTextWithItemLinks("{{item:0}} {{item:4294967296}}", items, 80),
        std::string("{{item:0}} {{item:4294967296}}"));
    // An unknown id takes no link slot.
    CHECK_EQ(Count(SanitizeChatTextWithItemLinks("{{item:9}} {{item:49623}} {{item:50363}}", items, 80), "|H"), 2u);
}

LEEROY_TEST(ItemLinks_NotAPlaceholderStaysText)
{
    auto const items = Armory();
    CHECK_EQ(SanitizeChatTextWithItemLinks("{{item:}} {{item:12a}} {item:49623} {{item:49623} {{ item:49623}}", items, 80),
        std::string("{{item:}} {{item:12a}} {item:49623} {{item:49623} {{ item:49623}}"));
    CHECK_EQ(SanitizeChatTextWithItemLinks("{{{item:49623}}}", items, 80), "{" + kShadowmourne + "}");
    CHECK_EQ(SanitizeChatTextWithItemLinks("{{item:12345678901}}", items, 80), std::string("{{item:12345678901}}"));
}

LEEROY_TEST(ItemLinks_EveryOtherPipeIsStripped)
{
    auto const items = Armory();
    // A link the model wrote itself is text, never a link.
    CHECK_EQ(SanitizeChatTextWithItemLinks("|cff1eff00|Hitem:1|h[x]|h|r", items, 80),
        std::string("cff1eff00Hitem:1h[x]hr"));
    // Pipes around or inside a placeholder go before it is read.
    CHECK_EQ(SanitizeChatTextWithItemLinks("|{{item:49623}}|", items, 80), kShadowmourne);
    CHECK_EQ(SanitizeChatTextWithItemLinks("{{it|em:49623}}", items, 80), kShadowmourne);
    // An item name with a pipe loses it, in the link and as plain text.
    std::string const out = SanitizeChatTextWithItemLinks("{{item:1}}", items, 80);
    CHECK_EQ(out, std::string("|cff1eff00|Hitem:1:0:0:0:0:0:0:0:80|h[PipeName]|h|r"));
    CHECK_EQ(Count(out, "|"), 5u);
    CHECK_EQ(SanitizeChatTextWithItemLinks("{{item:1}}", items, 80, 255, 0), std::string("[PipeName]"));
    CHECK_EQ(SanitizeChatTextWithItemLinks(std::string("a\nb\x01{{item:6948}}"), items, 80),
        std::string("a b|cffffffff|Hitem:6948:0:0:0:0:0:0:0:80|h[Hearthstone]|h|r"));
}

LEEROY_TEST(ItemLinks_PlainTextMatchesTheSanitizer)
{
    auto const items = Armory();
    for (std::string const& s : {std::string("a\nb\r\nc\td"), std::string(300, 'x'), std::string("  h\xC3\xA9llo  "),
             std::string(254, 'a') + "\xC3\xA9", std::string(250, 'a') + "     bcdefgh", std::string(" \n\t "),
             std::string("|cff1eff00|Hitem:1|h[x]|h|r")})
    {
        CHECK_EQ(SanitizeChatTextWithItemLinks(s, items, 80), SanitizeChatText(s));
        CHECK_EQ(SanitizeChatTextWithItemLinks(s, items, 80, 10), SanitizeChatText(s, 10));
    }
}

LEEROY_TEST(ItemLinks_ByteLimitDropsLinksNotHalves)
{
    auto const items = Armory();
    // 200 + 1 + 59 = 260 > 255: the link becomes plain text.
    std::string const head(200, 'a');
    std::string const out = SanitizeChatTextWithItemLinks(head + " {{item:49623}}", items, 80);
    CHECK_EQ(out, head + " [Shadowmourne]");
    // Two links where only the first fits beside the text.
    std::string const mid(130, 'b');
    std::string const two = SanitizeChatTextWithItemLinks(mid + " {{item:49623}} {{item:50363}}", items, 80);
    CHECK_EQ(two, mid + " " + kShadowmourne + " [Deathbringer's Will]");
    CHECK(two.size() <= kMaxChatBytes);
    // A plain name past the limit is cut like any text; never a link.
    std::string const full(250, 'c');
    std::string const cut = SanitizeChatTextWithItemLinks(full + " {{item:49623}}", items, 80);
    CHECK_EQ(cut, full + " [Sha");
    CHECK_EQ(Count(cut, "|"), 0u);
    // Exactly at the limit, the link stays.
    std::string const exact(255 - 1 - kShadowmourne.size(), 'd');
    CHECK_EQ(SanitizeChatTextWithItemLinks(exact + " {{item:49623}}", items, 80), exact + " " + kShadowmourne);
    for (std::size_t n = 150; n < 260; ++n)
    {
        std::string const s = SanitizeChatTextWithItemLinks(std::string(n, 'e') + " {{item:49623}} {{item:50363}} x",
            items, 80);
        CHECK(s.size() <= kMaxChatBytes);
        CHECK_EQ(Count(s, "|c"), Count(s, "|r"));
        CHECK_EQ(Count(s, "|H"), Count(s, "|c"));
    }
}

LEEROY_TEST(ItemLinks_EmptyAndNoLookup)
{
    CHECK_EQ(SanitizeChatTextWithItemLinks("", Armory(), 80), std::string());
    CHECK_EQ(SanitizeChatTextWithItemLinks("{{item:49623}}", ItemLookup{}, 80), std::string("[unknown item]"));
}
