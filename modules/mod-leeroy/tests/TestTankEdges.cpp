// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyTankEdges.h"
#include "TestHarness.h"

#include <cstdint>
#include <vector>

using namespace Leeroy;
using Guids = std::vector<std::uint32_t>;

LEEROY_TEST(TankEdges_NobodyRunningIsNoEdge)
{
    TankEdges tanks;
    auto const e = tanks.Update({});
    CHECK(e.started.empty()); CHECK(e.ended.empty());
}

LEEROY_TEST(TankEdges_StartIsReportedOnce)
{
    TankEdges tanks;
    CHECK(tanks.Update({7}).started == Guids({7}));
    auto const e = tanks.Update({7});
    CHECK(e.started.empty()); CHECK(e.ended.empty());
}

LEEROY_TEST(TankEdges_EndIsReportedOnce)
{
    TankEdges tanks;
    tanks.Update({7});
    auto const e = tanks.Update({});
    CHECK(e.started.empty()); CHECK(e.ended == Guids({7}));
    CHECK(tanks.Update({}).ended.empty());
}

LEEROY_TEST(TankEdges_BothEdgesInOnePoll)
{
    TankEdges tanks;
    tanks.Update({5, 7});
    auto const e = tanks.Update({7, 9});
    CHECK(e.started == Guids({9})); CHECK(e.ended == Guids({5}));
}

LEEROY_TEST(TankEdges_AnyOrderAndRepeats)
{
    TankEdges tanks;
    CHECK(tanks.Update({9, 3, 9, 5}).started == Guids({3, 5, 9}));
    CHECK(tanks.Update({3, 5, 9, 9}).started.empty());
    CHECK(tanks.Update({9}).ended == Guids({3, 5}));
}

LEEROY_TEST(TankEdges_ANewRunIsANewStart)
{
    TankEdges tanks;
    tanks.Update({7});
    tanks.Update({});
    CHECK(tanks.Update({7}).started == Guids({7}));
}

LEEROY_TEST(TankEdges_ResetReportsWhoIsRunning)
{
    TankEdges tanks;
    tanks.Update({5, 7});
    tanks.Reset();                         // a new mind knows of no run
    auto const e = tanks.Update({7});
    CHECK(e.started == Guids({7}));
    CHECK(e.ended.empty());                // nobody told it 5 was running, so nobody tells it 5 stopped
}
