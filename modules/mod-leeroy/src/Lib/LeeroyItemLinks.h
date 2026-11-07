// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_LEEROY_LEEROY_ITEM_LINKS_H
#define MOD_LEEROY_LEEROY_ITEM_LINKS_H

#include "LeeroyText.h"

#include <cstddef>
#include <cstdint>
#include <functional>
#include <optional>
#include <string>
#include <string_view>

namespace Leeroy
{
// What the item template says of an item: its name (Name1) and quality.
struct ItemLinkInfo
{
    std::string name;
    std::uint32_t quality = 0;
};

// The item template of an id, or nullopt for none (the module asks sObjectMgr->GetItemTemplate).
using ItemLookup = std::function<std::optional<ItemLinkInfo>(std::uint32_t itemId)>;

inline constexpr std::size_t kMaxItemLinks = 2;
inline constexpr std::string_view kUnknownItemText = "[unknown item]";

// The 3.3.5 client's colour of an item quality (ItemQualityColors in SharedDefines.h), or 0 for a quality it has none
// for.
std::uint32_t ItemQualityColor(std::uint32_t quality);

// "|c<colour>|Hitem:<id>:0:0:0:0:0:0:0:<level>|h[<name>]|h|r": the nine fields the core's item link parser reads
// (HyperlinkTags.cpp: id, enchant, three gems, a zero, random property, suffix seed and render level). The name is
// sanitized and loses any bracket. Empty for an id of 0, a quality without a colour or a name with nothing left.
std::string BuildItemLink(std::uint32_t itemId, ItemLinkInfo const& info, std::uint32_t level);

// A line from the mind with its {{item:<id>}} placeholders. SanitizeChatText runs first, uncapped, so no '|' the text
// carried survives; then each placeholder becomes its item's link, at most maxLinks of them, and the line is cut to
// maxBytes. A placeholder past maxLinks is "[Name]", and so is one whose link would carry the line over maxBytes (the
// last first); an id the lookup does not know is "[unknown item]". A cut never falls inside a link. Text without a
// placeholder comes out exactly as SanitizeChatText(text, maxBytes).
std::string SanitizeChatTextWithItemLinks(std::string_view text, ItemLookup const& lookup, std::uint32_t level,
    std::size_t maxBytes = kMaxChatBytes, std::size_t maxLinks = kMaxItemLinks);
}

#endif
