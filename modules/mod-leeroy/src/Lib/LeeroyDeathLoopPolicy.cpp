// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyDeathLoopPolicy.h"

namespace Leeroy::Guild
{
namespace
{
bool Eligible(DeathLoopState const& s)
{
    return s.cast && s.pinned && s.online && !s.grouped && !s.instance && !s.battleground
        && !s.owned && !s.transit && !s.trading && !s.duel;
}
void Expire(DeathLoopHistory& h, std::uint64_t now)
{
    while (!h.deaths.empty() && (h.deaths.front() > now || now - h.deaths.front() > kDeathWindowMs))
        h.deaths.pop_front();
}
}

void ObserveDeathLoop(DeathLoopHistory& h, DeathLoopState const& s, std::uint64_t now)
{
    Expire(h, now);
    if (!Eligible(s)) h.deaths.clear();
    else if (s.dead && !h.wasDead) h.deaths.push_back(now);
    // A blocked death cannot be counted again when a lease/group ends while still dead.
    h.wasDead = s.dead;
}

unsigned ClaimDeathLoopRescue(DeathLoopHistory& h, DeathLoopState const& s,
    std::uint64_t now, unsigned& attempted)
{
    Expire(h, now);
    if (!Eligible(s) || s.dead || s.combat || h.deaths.size() < 3 || attempted >= kDeathRescuesPerPass
        || (h.lastAttempt && (now < *h.lastAttempt || now - *h.lastAttempt < kDeathRescueCooldownMs)))
        return 0;
    auto count = static_cast<unsigned>(h.deaths.size());
    h.lastAttempt = now;
    h.deaths.clear();
    ++attempted;
    return count;
}
}
