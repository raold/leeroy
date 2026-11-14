// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_RAID_ASSEMBLY_H
#define MOD_LEEROY_RAID_ASSEMBLY_H

#include "LeeroyCommands.h"
#include "DBCEnums.h"

class Group;
class Player;

namespace Leeroy::Guild
{
// The raid night's own group mechanics, also the owner's `.playerbots bot addN` (GuildAdd.cpp). World thread.
// The leader's group, created with him as its leader when he has none; nullptr when the core refuses.
Group* LeaderGroup(Player* leader);
// What a member takes on joining the human's raid: him as master, reset strategies, the arrow formation, for
// damage a 4 s wait for attack so the tank opens pulls and, with Leeroy.Hazards.Enable, non-combat avoid aoe.
void RaidHabits(Player* bot, Player* leader);
// Dungeon-clear's entry to that raid map and difficulty (quests, attunements); nothing without dungeon-clear.
void GrantRaidEntry(Player* bot, uint32 map, Difficulty difficulty);
LeeroyCommandResult HandleClearLockout(nlohmann::json const&);
LeeroyCommandResult HandleFormRaid(nlohmann::json const&);
LeeroyCommandResult HandleSetLootMaster(nlohmann::json const&);
LeeroyCommandResult HandleSummon(nlohmann::json const&);
LeeroyCommandResult HandleRaidControl(nlohmann::json const&);
void TickSummons();
void RegisterRaidAssemblyHandlers();
}

#endif
