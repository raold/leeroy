// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_HAZARD_JOB_H
#define MOD_LEEROY_HAZARD_JOB_H
#include <cstdint>
class Player;
namespace Leeroy::Guild
{
// Leeroy.Hazards.Enable: the owner's group's cast bots keep out of ground hazards out of combat too, and their
// follow spots and exits avoid the hazards the last tick saw (pure policy LeeroyHazardPolicy). World thread.
void ReloadHazards();
// OnStartup: hands playerbots the follow-spot and exit filter; a playerbots without the hook keeps only the
// non-combat avoid aoe.
void InstallHazardFilter();
void TickHazards(std::uint32_t diffMs);
// RaidHabits, after its strategy reset: a member who qualifies gets its non-combat avoid aoe back at once.
void ApplyHazards(Player* bot);
}
#endif
