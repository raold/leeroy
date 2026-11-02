// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_GEAR_UP_JOB_H
#define MOD_LEEROY_GEAR_UP_JOB_H

#include "LeeroyCommands.h"

namespace Leeroy::Guild
{
LeeroyCommandResult HandleGearUp(nlohmann::json const&);
LeeroyCommandResult HandleRemoveFriend(nlohmann::json const&);
void TickGearUp();
void RegisterGearUpHandler();
void RegisterSocialCommands();
}

#endif
