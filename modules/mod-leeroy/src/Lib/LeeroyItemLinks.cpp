// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyItemLinks.h"

#include <algorithm>
#include <array>
#include <cstdio>
#include <limits>
#include <vector>

namespace Leeroy
{
namespace
{
// ItemQualityColors (SharedDefines.h): grey, white, green, blue, purple, orange, artifact, heirloom.
constexpr std::array<std::uint32_t, 8> kQualityColors = {
    0xff9d9d9d, 0xffffffff, 0xff1eff00, 0xff0070dd, 0xffa335ee, 0xffff8000, 0xffe6cc80, 0xffe6cc80};

constexpr std::string_view kTokenOpen = "{{item:";
constexpr std::string_view kTokenClose = "}}";
constexpr std::size_t kMaxIdDigits = 10;
constexpr std::size_t kMaxNameBytes = 128;

bool IsCont(unsigned char c)
{
    return (c & 0xC0) == 0x80;
}

// The item's name as a link may show it: sanitized, without brackets.
std::string LinkName(std::string_view raw)
{
    std::string out;
    for (char c : SanitizeChatText(raw, kMaxNameBytes))
        if (c != '[' && c != ']')
            out += c;
    while (!out.empty() && out.back() == ' ')
        out.pop_back();
    while (!out.empty() && out.front() == ' ')
        out.erase(out.begin());
    return out;
}

// The id of the placeholder at text[at], with its length, or 0 when none starts there.
std::uint32_t ParseToken(std::string_view text, std::size_t at, std::size_t& length)
{
    if (text.substr(at, kTokenOpen.size()) != kTokenOpen)
        return 0;
    std::size_t i = at + kTokenOpen.size();
    std::uint64_t value = 0;
    std::size_t digits = 0;
    while (i < text.size() && text[i] >= '0' && text[i] <= '9')
    {
        if (++digits > kMaxIdDigits)
            return 0;
        value = value * 10 + static_cast<std::uint64_t>(text[i] - '0');
        ++i;
    }
    if (digits == 0 || value == 0 || value > std::numeric_limits<std::uint32_t>::max()
        || text.substr(i, kTokenClose.size()) != kTokenClose)
        return 0;
    length = i + kTokenClose.size() - at;
    return static_cast<std::uint32_t>(value);
}

struct Segment
{
    std::string text;
    std::string plain;   // a link's "[Name]"; unused for text
    bool link = false;
};
}

std::uint32_t ItemQualityColor(std::uint32_t quality)
{
    return quality < kQualityColors.size() ? kQualityColors[quality] : 0;
}

std::string BuildItemLink(std::uint32_t itemId, ItemLinkInfo const& info, std::uint32_t level)
{
    std::uint32_t const colour = ItemQualityColor(info.quality);
    std::string const name = LinkName(info.name);
    if (!itemId || !colour || name.empty())
        return {};
    char head[64];
    std::snprintf(head, sizeof(head), "|c%08x|Hitem:%u:0:0:0:0:0:0:0:%u|h[", static_cast<unsigned>(colour),
        static_cast<unsigned>(itemId), static_cast<unsigned>(level));
    return std::string(head) + name + "]|h|r";
}

std::string SanitizeChatTextWithItemLinks(std::string_view text, ItemLookup const& lookup, std::uint32_t level,
    std::size_t maxBytes, std::size_t maxLinks)
{
    std::string const clean = SanitizeChatText(text, std::numeric_limits<std::size_t>::max());

    std::vector<Segment> segments;
    std::size_t links = 0;
    std::size_t total = 0;
    std::string run;
    auto const flush = [&] {
        if (!run.empty())
        {
            total += run.size();
            segments.push_back({std::move(run), {}, false});
            run.clear();
        }
    };

    for (std::size_t i = 0; i < clean.size();)
    {
        std::size_t length = 0;
        std::uint32_t const id = ParseToken(clean, i, length);
        if (!id)
        {
            run += clean[i++];
            continue;
        }
        i += length;
        std::optional<ItemLinkInfo> const info = lookup ? lookup(id) : std::nullopt;
        std::string const name = info ? LinkName(info->name) : std::string();
        if (name.empty())
        {
            run += kUnknownItemText;
            continue;
        }
        std::string plain = "[" + name + "]";
        std::string link = links < maxLinks ? BuildItemLink(id, *info, level) : std::string();
        if (link.empty())
        {
            run += plain;
            continue;
        }
        flush();
        ++links;
        total += link.size();
        segments.push_back({std::move(link), std::move(plain), true});
    }
    flush();

    // Over the limit, links become their plain names, the last first.
    for (auto it = segments.rbegin(); total > maxBytes && it != segments.rend(); ++it)
    {
        if (!it->link)
            continue;
        total = total - it->text.size() + it->plain.size();
        it->text = std::move(it->plain);
        it->link = false;
    }

    std::string out;
    out.reserve(std::min(total, maxBytes));
    for (Segment const& segment : segments)
    {
        if (out.size() + segment.text.size() <= maxBytes)
        {
            out += segment.text;
            continue;
        }
        if (!segment.link)
        {
            std::size_t cut = maxBytes - out.size();
            while (cut > 0 && IsCont(static_cast<unsigned char>(segment.text[cut])))
                --cut;
            out.append(segment.text, 0, cut);
        }
        break;
    }
    while (!out.empty() && out.back() == ' ')
        out.pop_back();
    return out;
}
}
