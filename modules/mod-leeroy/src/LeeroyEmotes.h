// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_LEEROY_LEEROY_EMOTES_H
#define MOD_LEEROY_LEEROY_EMOTES_H

#include "Define.h"

#include <optional>
#include <string_view>

// The 252 TEXT_EMOTE_* values of SharedDefines.h by name ("hug", "mount_special"), both ways. Any thread.
// Names compare lowercased with every '_' removed on both sides, so "MountSpecial" finds "mount_special".
std::optional<uint32> LeeroyTextEmoteId(std::string_view name);
std::string_view LeeroyTextEmoteName(uint32 id);   // "" when unknown

#endif
