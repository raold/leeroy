// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_RAID_ASSEMBLY_H
#define MOD_LEEROY_RAID_ASSEMBLY_H

#include "LeeroyCommands.h"

namespace Leeroy::Guild
{
LeeroyCommandResult HandleClearLockout(nlohmann::json const&);
LeeroyCommandResult HandleFormRaid(nlohmann::json const&);
LeeroyCommandResult HandleSetLootMaster(nlohmann::json const&);
LeeroyCommandResult HandleSummon(nlohmann::json const&);
LeeroyCommandResult HandleRaidControl(nlohmann::json const&);
void TickSummons();
void RegisterRaidAssemblyHandlers();
}

#endif
