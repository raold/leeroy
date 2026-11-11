// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyDeathLoopPolicy.h"
#include <cmath>

namespace Leeroy::Guild
{
namespace
{
bool Eligible(DeathLoopState const& s)
{
    return s.cast && s.pinned && s.online && !s.grouped && !s.instance && !s.battleground
        && !s.owned && !s.transit && !s.trading && !s.duel && !s.invited && !s.queued;
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
    bool const observed = s.cast && s.pinned && s.online;
    // Temporary busy states stop mutation, not observation. Deaths in a controlled
    // group/instance/BG are not new open-world loop evidence, but do not erase it.
    if (observed && !s.grouped && !s.instance && !s.battleground && s.dead && !h.wasDead)
    {
        h.deaths.push_back(now);
        h.area = DeathArea{s.map,s.x,s.y,s.z};
    }
    bool outside = false;
    if (observed && !s.dead && !s.transit && h.area && std::isfinite(s.x) && std::isfinite(s.y) && std::isfinite(s.z))
    {
        auto const& a = *h.area;
        auto const dx = s.x-a.x, dy = s.y-a.y, dz = s.z-a.z;
        outside = s.map != a.map || dx*dx+dy*dy+dz*dz > kDeathAreaRadius*kDeathAreaRadius;
    }
    if (!outside) h.outsideSince.reset();
    else if (!h.outsideSince || now < *h.outsideSince) h.outsideSince = now;
    else if (now-*h.outsideSince >= kDeathAreaClearMs)
    {
        h.deaths.clear(); h.area.reset(); h.outsideSince.reset();
    }
    if (observed) h.wasDead = s.dead;
}

bool DeathLoopRescueReady(DeathLoopHistory const& h, DeathLoopState const& s, std::uint64_t now)
{
    return Eligible(s) && !s.dead && !s.combat && h.deaths.size() >= 3
        && (!h.lastAttempt || (now >= *h.lastAttempt && now-*h.lastAttempt >= kDeathRescueCooldownMs));
}

bool DeathRescueBudget::Claim(std::uint64_t now)
{
    while (!attempts.empty() && now >= attempts.front() && now-attempts.front() >= kDeathWindowMs)
        attempts.pop_front();
    if (attempts.size() >= kDeathRescuesPerWindow || (!attempts.empty() && now < attempts.back())) return false;
    attempts.push_back(now);
    return true;
}

unsigned ClaimDeathLoopRescue(DeathLoopHistory& h, DeathLoopState const& s,
    std::uint64_t now, DeathRescueBudget& budget)
{
    Expire(h, now);
    if (!DeathLoopRescueReady(h,s,now) || !budget.Claim(now))
        return 0;
    auto count = static_cast<unsigned>(h.deaths.size());
    h.lastAttempt = now;
    h.deaths.clear();
    h.area.reset(); h.outsideSince.reset();
    return count;
}
}
