// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_BOT_COMMANDS_H
#define MOD_LEEROY_BOT_COMMANDS_H

#include "LeeroyCommands.h"

namespace Leeroy::Guild
{
LeeroyCommandResult HandleSetSpec(nlohmann::json const&);
LeeroyCommandResult HandleBotDo(nlohmann::json const&);
LeeroyCommandResult HandleStockItems(nlohmann::json const&);
void RegisterBotCommandHandlers();
void TickBotCommands();
}

#endif
