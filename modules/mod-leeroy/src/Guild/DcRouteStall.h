// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_DC_ROUTE_STALL_H
#define MOD_LEEROY_DC_ROUTE_STALL_H

class Player;

namespace Leeroy::Guild
{
// Dungeon-clear gave up on its route and waits for a `dc skip`: the leader's stall reason is set and is not
// a door's own (a door it cannot open pauses the run instead). It clears the reason itself as soon as it
// moves again. Its own translation unit: dungeon-clear's DcActionShared.h declares DcApproachState as a
// class, its definition is a struct, and mod-leeroy builds with -Werror (clang's -Wmismatched-tags).
bool DcRouteStalled(Player* leader);
}

#endif
