// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_OWNERSHIP_H
#define MOD_LEEROY_REALM_OWNERSHIP_H
#include <cstdint>
namespace Leeroy::Realm
{
// World-thread, read-only. Includes admitted/queued, pending and retained
// unresolved operations even when Realm is disabled. No gameplay waiver or
// terminal event alone releases this exclusion.
bool RealmOwns(std::uint32_t guid);
}
#endif
