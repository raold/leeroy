// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_LEEROY_LEEROY_COMMANDS_H
#define MOD_LEEROY_LEEROY_COMMANDS_H

#include "LeeroyProtocol.h"

#include <functional>
#include <string>
#include <string_view>
#include <nlohmann/json.hpp>

class Player;

// World thread; returns the reply line (a pong, a cast or an ack).
std::string LeeroyExecuteCommand(Leeroy::Command const& cmd);

// World thread. Queues CMSG_MESSAGECHAT on the human's own session, which handles it on its next update as a line he
// typed, in his original team's language. text (and toName, for a whisper) go as given: the caller has sanitized and
// checked them. false, sending nothing, for a channel without a chat type (raid warning, emote). Used by human_say
// and owner_say.
bool LeeroyQueueHumanChat(Player* human, Leeroy::Channel channel, std::string const& text, std::string const& toName);

// World thread. What a cast member's line says: SanitizeChatText, with the mind's {{item:<id>}} placeholders as item
// links (Leeroy::SanitizeChatTextWithItemLinks: the item template's name and quality at the speaker's level, at most
// two, within 255 bytes). Every other '|' is gone, as before.
std::string LeeroyBotChatText(Player const* speaker, std::string_view text);

// Extension commands (P1d registers its nine here). The bridge has already checked v, id and type; the handler gets
// the whole message, checks its own fields, runs on the world thread and returns the ack's result.
struct LeeroyCommandResult
{
    bool ok = false;
    std::string error;
};

using LeeroyCommandHandler = std::function<LeeroyCommandResult(nlohmann::json const& msg)>;

// World thread (OnStartup). Stores the handler, then calls Leeroy::RegisterExtensionCommand so the bridge thread
// starts accepting the type. false for a core type, a duplicate, an empty type or an empty handler.
bool LeeroyRegisterCommand(std::string const& type, LeeroyCommandHandler handler);

#endif
