// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyTankEdges.h"

#include <algorithm>
#include <iterator>
#include <utility>

namespace Leeroy
{
TankEdges::Edges TankEdges::Update(std::vector<std::uint32_t> const& active)
{
    std::set<std::uint32_t> now(active.begin(), active.end());
    Edges edges;
    std::set_difference(now.begin(), now.end(), _known.begin(), _known.end(), std::back_inserter(edges.started));
    std::set_difference(_known.begin(), _known.end(), now.begin(), now.end(), std::back_inserter(edges.ended));
    _known = std::move(now);
    return edges;
}
}
