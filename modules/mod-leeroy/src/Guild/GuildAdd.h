// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_GUILD_ADD_H
#define MOD_LEEROY_GUILD_ADD_H

namespace Leeroy::Guild
{
// The owner's `.playerbots bot add5|add10|add25|add40`, filled from free members of his own guild instead of
// playerbots' addclass characters (LeeroyGuildAddPolicy.h). Anyone else's command reaches playerbots unchanged.
void AddGuildAddScripts();
// guild_add_roster: the posted raid lineup the owner's add picker follows while Leeroy.GuildAdd.FollowRoster is on.
// World thread (OnStartup).
void RegisterGuildAddRosterHandler();
// Re-reads Leeroy.GuildAdd.FollowRoster (OnAfterConfigLoad, so .reload config applies it).
void ReloadGuildAdd();
}

#endif
