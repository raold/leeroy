// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_HAZARD_JOB_H
#define MOD_LEEROY_HAZARD_JOB_H
#include "LeeroyHazardPolicy.h"
#include <cstdint>
#include <vector>
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
// World thread, read-only, whatever Leeroy.Hazards.Enable says: the ground hazards this job would see around each
// anchor on the reference's map, measured as it measures them. The mechanic responder keeps its spots clear of them.
std::vector<HazardZone> ScanHazardZones(Player* reference, std::vector<HazardPoint> const& anchors);
}
#endif
