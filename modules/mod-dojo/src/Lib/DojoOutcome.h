// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_DOJO_DOJO_OUTCOME_H
#define MOD_DOJO_DOJO_OUTCOME_H

#include <cstdint>
#include <optional>
#include <string_view>
#include <vector>

// An arena episode's outcome and its rewards (D17, D24), decided by the worker from the actors' state at one world
// tick: pure, no clock and no AzerothCore includes. dojo/leeroy_dojo/outcome.py is the same rules, and both run the
// shared cases in tests/fixtures/outcome_cases.json.
//
// A team is alive while any member is. No team alive: simultaneous death, no winner. Exactly one team alive: death,
// and that team wins. Otherwise the match limit, when reached, is a draw; a death at the limit's tick still decides.
// The terminal reward is +1 to the winning team, -1 to every other and 0 on a draw. Shaping is
// scale * (Phi(next) - Phi(current)) with Phi = team 0's mean remaining health fraction minus team 1's (the original
// roster, the dead counted as 0); Phi is 0 at every terminal state and team 1 gets the opposite sign.
namespace Dojo
{
inline constexpr double kShapingScale = 0.05;

enum class OutcomeReason : std::uint8_t { None, Death, SimultaneousDeath, TimeLimit };

std::string_view OutcomeReasonName(OutcomeReason reason);   // "", death, simultaneous_death, time_limit

struct Standing
{
    std::uint32_t team = 0;
    bool alive = true;
    double health = 1.0;   // fraction of maximum health
};

struct Outcome
{
    OutcomeReason reason = OutcomeReason::None;
    std::optional<std::uint32_t> winner;   // the winning team, for a death only

    bool Over() const { return reason != OutcomeReason::None; }
};

struct Reward
{
    double terminal = 0;
    double shaping = 0;
};

Outcome DecideOutcome(std::vector<Standing> const& actors, bool timeUp);

// Team 0's mean health fraction minus team 1's, the dead counted as 0, rounded to 6 decimals.
double Potential(std::vector<Standing> const& actors);

// One reward per actor for the interval from a state whose potential was `before` to `actors` now.
std::vector<Reward> Rewards(std::vector<Standing> const& actors, double before, Outcome const& outcome,
    double scale = kShapingScale);
}

#endif
