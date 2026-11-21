// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#pragma once

#include "LeeroyCommands.h"
#include <vector>

class Player;

namespace Leeroy::Guild
{
struct DungeonReceipt
{
    std::string id;
    std::uint64_t revision = 0;
    nlohmann::json data;
};
bool DungeonReceiptIdValid(std::string const& id);
bool BeginDungeonReceipt(DungeonReceipt&, std::string const& id, std::uint32_t guild, std::uint32_t human,
    std::uint32_t leader, std::vector<std::uint32_t> const& roster);
bool SaveDungeonReceipt(DungeonReceipt&, nlohmann::json const& next, std::int64_t now = NowUnixMs());
bool DungeonRecoveryConflicts(std::vector<std::uint32_t> const& guids, bool requiresSlot);
bool DungeonReturned(std::string const& id, std::vector<std::uint32_t> const& guids);
bool ObserveDungeonHandback(DungeonReceipt& receipt, Player& player);
bool DungeonReturnsPersisted(DungeonReceipt& receipt);
void TickDungeonRecovery(std::uint32_t diff);
LeeroyCommandResult PublishDungeonReceipt(std::string const& id);
}
