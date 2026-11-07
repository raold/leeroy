// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_LEEROY_LEEROY_TEXT_H
#define MOD_LEEROY_LEEROY_TEXT_H

#include <cstddef>
#include <string>
#include <string_view>
#include <vector>

namespace Leeroy
{
inline constexpr std::size_t kMaxChatBytes = 255;

std::string SanitizeChatText(std::string_view text, std::size_t maxBytes = kMaxChatBytes);
// SanitizeChatText without a cap, and for each byte of the result the index in `text` it was copied from (the space a
// whitespace run becomes: the run's first character). A span of the result is contiguous in `text` when its first and
// last bytes' indexes differ by its length less one.
std::string SanitizeChatTextMapped(std::string_view text, std::vector<std::size_t>& from);
bool StartsWithCommandPrefix(std::string_view text);
bool IsLoopbackHost(std::string_view host);
}

#endif
