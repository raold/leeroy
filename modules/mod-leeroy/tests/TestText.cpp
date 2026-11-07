// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyText.h"
#include "TestHarness.h"

using namespace Leeroy;

LEEROY_TEST(Text_StripsNewlinesPipesAndControls)
{
    CHECK_EQ(SanitizeChatText("a\nb\r\nc\td"), "a b c d");
    CHECK_EQ(SanitizeChatText("|cff1eff00|Hitem:1|h[x]|h|r"), "cff1eff00Hitem:1h[x]hr");
    CHECK_EQ(SanitizeChatText(std::string("a\x01" "b\x7f" "c")), "abc");
}

LEEROY_TEST(Text_ClampsOnCodepointBoundary)
{
    std::string s(254, 'a');
    s += "\xC3\xA9";
    CHECK_EQ(SanitizeChatText(s).size(), 254u);
    CHECK_EQ(SanitizeChatText(std::string(300, 'x')).size(), 255u);
    CHECK_EQ(SanitizeChatText("abc", 2), "ab");
}

LEEROY_TEST(Text_DropsInvalidUtf8KeepsValid)
{
    CHECK_EQ(SanitizeChatText(std::string("ok\xFF") + "\xC3" + "(end"), "ok(end");
    CHECK_EQ(SanitizeChatText("  h\xC3\xA9llo  "), "h\xC3\xA9llo");
    CHECK_EQ(SanitizeChatText(" \n\t "), "");
}

LEEROY_TEST(Text_CommandPrefix)
{
    CHECK(StartsWithCommandPrefix(".gm on"));
    CHECK(StartsWithCommandPrefix("  !roll"));
    CHECK(!StartsWithCommandPrefix("hi .gm"));
    CHECK(!StartsWithCommandPrefix(""));
}

LEEROY_TEST(Text_LoopbackHosts)
{
    CHECK(IsLoopbackHost("127.0.0.1"));
    CHECK(IsLoopbackHost("127.1.2.3"));
    CHECK(IsLoopbackHost("::1"));
    CHECK(IsLoopbackHost("localhost"));
    CHECK(!IsLoopbackHost("0.0.0.0"));
    CHECK(!IsLoopbackHost(""));
    CHECK(!IsLoopbackHost("::"));
    CHECK(!IsLoopbackHost("192.168.1.10"));
    CHECK(!IsLoopbackHost("127.0.0.256"));
    CHECK(!IsLoopbackHost("127.0.0"));
}

LEEROY_TEST(Text_MappedSanitizerKeepsEachBytesSource)
{
    std::vector<std::size_t> from;
    CHECK_EQ(SanitizeChatTextMapped("  a|b \n c\xC3\xA9", from), SanitizeChatText("  a|b \n c\xC3\xA9"));
    // "a b c" + e-acute: a at 2, b at 4, the space from the run at 5, c at 8, the two bytes at 9 and 10.
    std::vector<std::size_t> const expected = {2, 4, 5, 8, 9, 10};
    CHECK(from == expected);
    CHECK_EQ(SanitizeChatTextMapped(std::string(300, 'x'), from).size(), 300u);
    CHECK_EQ(from.size(), 300u);
}
