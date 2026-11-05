// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#include "DojoOutcome.h"

#include <algorithm>
#include <cmath>
#include <set>

namespace Dojo
{
namespace
{
double Round6(double value)
{
    double const r = std::round(value * 1e6) / 1e6;
    return r == 0.0 ? 0.0 : r;
}

double Clamp01(double value)
{
    return std::isfinite(value) ? std::clamp(value, 0.0, 1.0) : 0.0;
}

double MeanHealth(std::vector<Standing> const& actors, std::uint32_t team)
{
    double sum = 0;
    std::size_t members = 0;
    for (Standing const& a : actors)
        if (a.team == team)
        {
            sum += a.alive ? Clamp01(a.health) : 0.0;
            ++members;
        }
    return members ? sum / double(members) : 0.0;
}
}

std::string_view OutcomeReasonName(OutcomeReason reason)
{
    switch (reason)
    {
        case OutcomeReason::None: return "";
        case OutcomeReason::Death: return "death";
        case OutcomeReason::SimultaneousDeath: return "simultaneous_death";
        case OutcomeReason::TimeLimit: return "time_limit";
    }
    return "";
}

Outcome DecideOutcome(std::vector<Standing> const& actors, bool timeUp)
{
    std::set<std::uint32_t> teams, alive;
    for (Standing const& a : actors)
    {
        teams.insert(a.team);
        if (a.alive)
            alive.insert(a.team);
    }
    Outcome out;
    if (teams.empty())
        return out;
    if (alive.empty())
        out.reason = OutcomeReason::SimultaneousDeath;
    else if (alive.size() == 1 && teams.size() > 1)
    {
        out.reason = OutcomeReason::Death;
        out.winner = *alive.begin();
    }
    else if (timeUp)
        out.reason = OutcomeReason::TimeLimit;
    return out;
}

double Potential(std::vector<Standing> const& actors)
{
    return Round6(MeanHealth(actors, 0) - MeanHealth(actors, 1));
}

std::vector<Reward> Rewards(std::vector<Standing> const& actors, double before, Outcome const& outcome, double scale)
{
    double const after = outcome.Over() ? 0.0 : Potential(actors);
    double const delta = after - before;
    std::vector<Reward> out;
    out.reserve(actors.size());
    for (Standing const& a : actors)
    {
        Reward r;
        double const sign = a.team == 0 ? 1.0 : -1.0;
        r.shaping = Round6(scale * sign * delta);
        if (outcome.winner)
            r.terminal = a.team == *outcome.winner ? 1.0 : -1.0;
        out.push_back(r);
    }
    return out;
}
}
