// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_LOOT_RECEIPTS_H
#define MOD_LEEROY_LOOT_RECEIPTS_H
#include "LeeroyCommands.h"
namespace Leeroy::Guild
{
LeeroyCommandResult BeginLootReceipt(nlohmann::json const& message);
LeeroyCommandResult GetLootReceipt(nlohmann::json const& message);
void TickLootReceipts();
}
#endif
