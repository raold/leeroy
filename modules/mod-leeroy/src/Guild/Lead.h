// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_GUILD_LEAD_H
#define MOD_LEEROY_GUILD_LEAD_H

#include <cstdint>

namespace Leeroy::Guild
{
// Leeroy.Lead.Enable: a cast member of his group leads it in the open world while he follows (lead_do, pure policy
// LeeroyLead.h). World thread.
void ReloadLead();
// OnStartup: registers lead_do.
void RegisterLeadHandler();
// Once a second: the wait-for-him rule, new rpg kept to the activity, and the lead's end (every change given back).
void TickLead(std::uint32_t diffMs);
// At a logout: her lead ends with that life (the others follow him again); a follower is let go.
void ForgetLead(std::uint32_t guid);
}

#endif
