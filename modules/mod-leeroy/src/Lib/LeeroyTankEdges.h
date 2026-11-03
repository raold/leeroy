// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_LEEROY_LEEROY_TANK_EDGES_H
#define MOD_LEEROY_LEEROY_TANK_EDGES_H

#include <cstdint>
#include <set>
#include <vector>

namespace Leeroy
{
// Who began and who ended a dungeon-clear run since the last look. mod-dungeon-clear's status observer sees neither
// edge: `dc on` primes its change detector with the first status, and every way out of a run takes the tank off the
// list the detector walks. One thread at a time: the world thread polls it.
class TankEdges
{
public:
    struct Edges
    {
        std::vector<std::uint32_t> started, ended;   // ascending guids
    };

    // `active`: the tanks running a clear now, in any order.
    Edges Update(std::vector<std::uint32_t> const& active);
    // Forgets who was running: the next Update reports them all as started and nobody as ended.
    void Reset() { _known.clear(); }

private:
    std::set<std::uint32_t> _known;
};
}

#endif
