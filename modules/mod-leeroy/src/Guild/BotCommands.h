// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_BOT_COMMANDS_H
#define MOD_LEEROY_BOT_COMMANDS_H

#include "LeeroyCommands.h"
#include <vector>

namespace Leeroy::Guild
{
LeeroyCommandResult HandleSetSpec(nlohmann::json const&);
LeeroyCommandResult HandleBotDo(nlohmann::json const&);
LeeroyCommandResult HandleStockItems(nlohmann::json const&);
void RegisterBotCommandHandlers();
void TickBotCommands();
bool HasPendingBotCommands(std::vector<std::uint32_t> const& guids);
bool CancelTrainingBotJobs(std::string const& handle);
}

#endif
