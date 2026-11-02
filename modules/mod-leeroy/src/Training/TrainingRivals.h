// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_TRAINING_RIVALS_H
#define MOD_LEEROY_TRAINING_RIVALS_H
#include <cstdint>
class Player;
namespace Leeroy::Training
{
bool RivalIdentity(Player* player);
// World-thread snapshot. ownRaid permits the caller's published raid reservation, never a dummy.
bool RivalAvailable(Player* player, std::uint32_t level, bool ownRaid = false);
bool RivalBindingsSafe(Player* player, std::uint32_t targetMap = 0,
    std::uint32_t ownedInstance = 0, std::uint32_t ownedDifficulty = 0);
}
#endif
