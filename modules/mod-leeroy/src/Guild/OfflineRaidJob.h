// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_OFFLINE_RAID_JOB_H
#define MOD_LEEROY_OFFLINE_RAID_JOB_H

#include "LeeroyCommands.h"
#include <cstdint>

namespace Leeroy::Guild
{
LeeroyCommandResult HandleStartOfflineRaid(nlohmann::json const& message);
void TickOfflineRaid(std::uint32_t diffMs);
bool OfflineRaidOwns(std::uint32_t guid);
void SweepLeftoverGroups();
void RegisterOfflineRaidHandler();
}

#endif
