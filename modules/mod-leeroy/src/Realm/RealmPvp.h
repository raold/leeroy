// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_PVP_H
#define MOD_LEEROY_REALM_PVP_H
#include <cstdint>
#include "LeeroyQueueFencePolicy.h"
namespace Leeroy::Realm
{
void RegisterRealmPvpHandlers();
void AddRealmPvpScripts();
// Any-thread, immutable native arena-watch membership only. Call before the
// passive observer inbox lock; never consult world-thread holder maps here.
bool RealmPvpArenaObserverOwns(std::uint32_t guid, std::uintptr_t session);
// Read-only foreign WG exposure fence. True also means history/identity is
// unavailable; public membership absence cannot clear private grace or kicks.
bool RealmPvpForeignWintergraspHistory(std::uint32_t guid, std::uintptr_t session);
// Exact thread-local native call authority; a hold alone grants nothing.
bool RealmPvpQueueCallOwns(std::uint32_t guid, std::uintptr_t session, QueueKind kind);
}
#endif
