// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_LEEROY_LEEROY_TEXT_H
#define MOD_LEEROY_LEEROY_TEXT_H

#include <cstddef>
#include <string>
#include <string_view>

namespace Leeroy
{
inline constexpr std::size_t kMaxChatBytes = 255;

std::string SanitizeChatText(std::string_view text, std::size_t maxBytes = kMaxChatBytes);
bool StartsWithCommandPrefix(std::string_view text);
bool IsLoopbackHost(std::string_view host);
}

#endif
