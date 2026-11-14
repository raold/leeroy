// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_GUILD_ADD_H
#define MOD_LEEROY_GUILD_ADD_H

#include <string>

class Player;

namespace Leeroy::Guild
{
// The owner's `.playerbots bot add5|add10|add25|add40`, filled from free members of his own guild instead of
// playerbots' addclass characters (LeeroyGuildAddPolicy.h). Anyone else's command reaches playerbots unchanged.
void AddGuildAddScripts();
// The owner's join_group string (Strings.cpp), under the add command's own fences for a party of his: why the pick
// would skip this member, or the Realm or an offline raid holding someone in his group (empty when she may join);
// and the add command's teleport beside him.
std::string GuildAddJoinRefusal(Player* human, Player* bot);
bool GuildAddBring(Player* bot, Player* human);
// guild_add_roster: the posted raid lineup the owner's add picker follows while Leeroy.GuildAdd.FollowRoster is on.
// World thread (OnStartup).
void RegisterGuildAddRosterHandler();
// Re-reads Leeroy.GuildAdd.FollowRoster (OnAfterConfigLoad, so .reload config applies it).
void ReloadGuildAdd();
}

#endif
