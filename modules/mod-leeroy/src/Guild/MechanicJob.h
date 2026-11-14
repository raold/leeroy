// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_MECHANIC_JOB_H
#define MOD_LEEROY_MECHANIC_JOB_H

#include <cstdint>

namespace Leeroy::Guild
{
// Leeroy.Mechanics.Enable: when a boss starts casting a spell the mind's raid playbooks list for that instance
// (raid_mechanics), the cast bots of the owner's group and of a running offline raid it binds move per its kind
// (pure policy LeeroyMechanicPolicy). World thread, except the spell and aura hooks and playerbots' calls into the
// guard, which run on map threads and do geometry only.
void ReloadMechanics();
// OnStartup: hands playerbots the guard; a playerbots without the hook (local patch 0012) moves nobody.
void InstallMechanicGuard();
void TickMechanics(std::uint32_t diffMs);
// raid_mechanics {map, difficulty, mechanics}: one instance's table from the connected mind.
void RegisterMechanicsHandler();
// The cast-start, cast-completion, cast-cancel and aura hooks.
void AddMechanicScripts();
// World thread: the raid lead's spread, run out or back off order is about to move this bot; the responder leaves
// it alone for a while, so neither refuses nor undoes the other.
void MechanicYield(std::uint32_t guid);
}

#endif
