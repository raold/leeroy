// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_BODIES_H
#define MOD_LEEROY_REALM_BODIES_H

namespace Leeroy::Realm
{
// Called once by the Realm script loader. Registration itself performs no body mutation.
void RegisterRealmBodyHandlers();
void AddRealmBodyScripts();
}

#endif
