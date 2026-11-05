// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_LEEROY_LEEROY_EVENTS_H
#define MOD_LEEROY_LEEROY_EVENTS_H

#include "Define.h"
#include "LeeroyProtocol.h"

#include <string>

class Player;

// Any thread. Publishes a `chat` event for a line no chat hook sees (guild, officer, party and raid lines a cast
// member says through the bridge): the speaker's GUID, name, map and zone, via_bridge from LeeroyBridgeSpeech.
void LeeroyPublishChat(Player* speaker, Leeroy::Channel channel, std::string const& text, uint32 toGuid);

// While one is alive on a thread, chat and text_emote events published on that thread carry via_bridge = true.
// Player::Say/Yell/Whisper and HandleTextEmoteOpcode fire their hooks synchronously, inside the scope.
class LeeroyBridgeSpeech
{
public:
    LeeroyBridgeSpeech();
    ~LeeroyBridgeSpeech();

    LeeroyBridgeSpeech(LeeroyBridgeSpeech const&) = delete;
    LeeroyBridgeSpeech& operator=(LeeroyBridgeSpeech const&) = delete;

    static bool Active();
};

// Suppress only the synchronous whisper/achievement publications on this
// thread. Separate from bridge-origin provenance; nested scopes remain quiet.
class LeeroyQuietScope
{
public:
    LeeroyQuietScope();
    ~LeeroyQuietScope();
    LeeroyQuietScope(LeeroyQuietScope const&) = delete;
    LeeroyQuietScope& operator=(LeeroyQuietScope const&) = delete;
    static bool Active();
};

// Suppress only this exact synchronous chat tuple while a caller obtains its
// native acceptance receipt. After this scope ends, the caller may explicitly
// LeeroyPublishChat only for the accepted, revalidated line. Nothing is buffered
// or replayed, and unrelated/unscoped chat keeps its normal behavior. Nested
// scopes restore the previous suppression; this scope is distinct from Quiet.
class LeeroyDeferredChat
{
public:
    LeeroyDeferredChat(uint32 fromGuid, Leeroy::Channel channel, uint32 toGuid);
    ~LeeroyDeferredChat();
    LeeroyDeferredChat(LeeroyDeferredChat const&) = delete;
    LeeroyDeferredChat& operator=(LeeroyDeferredChat const&) = delete;
    static bool ActiveFor(uint32 fromGuid, Leeroy::Channel channel, uint32 toGuid);
private:
    uint32 _fromGuid, _toGuid;
    Leeroy::Channel _channel;
    LeeroyDeferredChat* _previous;
};

// World thread. The `afk` event has no hook (Player::ToggleAFK has none): OnUpdate polls the human and the online
// cast once a second and publishes each change; the reset after a greeting re-reports everyone who is AFK.
void LeeroyPollAfk();
void LeeroyResetAfk();

void AddSC_leeroy_events();

#endif
