// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_GUILD_RAID_LEAD_H
#define MOD_LEEROY_GUILD_RAID_LEAD_H

#include "LeeroyGuildCommands.h"

#include <string>

class Player;

namespace Leeroy::Guild
{
// Leeroy.RaidLead.Enable, as the last .reload config read it.
bool RaidLeadEnabled();
// One bot of an acknowledged bot_do "order" job, at the mutation boundary (TickBotCommands): "" applied, else the
// skip reason. Only bots in the group the owner leads, behind the Realm, training, offline-raid and pending-job fences.
std::string ApplyRaidOrder(Player* bot, BotDoCmd const& command);
// World tick: lift each hold when his group's fight starts or ends, after kRaidHoldMs, or once the switch is off.
void TickRaidLead();
// raid_lead: her raid icon on his target, the switch probe and his group's state. World thread.
void RegisterRaidLeadHandler();
}

#endif
