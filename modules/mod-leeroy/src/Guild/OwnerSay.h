// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_GUILD_OWNER_SAY_H
#define MOD_LEEROY_GUILD_OWNER_SAY_H

namespace Leeroy::Guild
{
// owner_say: the guild console's line, queued on the owner's own session as if he had typed it, behind the guard in
// LeeroyOwnerSay.h. World thread (OnStartup).
void RegisterOwnerSayHandler();
}

#endif
