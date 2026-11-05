// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyBenchmarkLoot.h"
#include <set>
#include <utility>

namespace Leeroy::Training
{
BenchmarkRewardDecision ClassifyBenchmarkReward(BenchmarkLootScope const& scope, BenchmarkLootActor const& actor,
    BenchmarkReward const& reward)
{
    if (reward.kind != BenchmarkRewardKind::Creature && reward.kind != BenchmarkRewardKind::GameObject)
        return BenchmarkRewardDecision::Ignore;
    auto body = scope.bodies.find(actor.guid);
    if (!scope.token || !actor.instance || actor.map != scope.map || (scope.instance && actor.instance != scope.instance)
        || body == scope.bodies.end() || !body->second.Matches(actor.player, actor.ai, actor.session))
        return BenchmarkRewardDecision::Ignore;
    if (actor.currentHuman && scope.bodies.contains(actor.currentHuman))
        return BenchmarkRewardDecision::Contaminated; // Shared rewards may also reach a newly configured human.
    if (actor.group != scope.group)
    {
        if (!scope.returning || actor.group) return BenchmarkRewardDecision::Ignore;
    }
    else
    {
        std::set<std::uint32_t> roster(actor.roster.begin(), actor.roster.end());
        if (roster.size() != actor.roster.size() || roster.size() != scope.bodies.size())
            return BenchmarkRewardDecision::Contaminated;
        for (auto guid : roster)
            if (!scope.bodies.contains(guid)) return BenchmarkRewardDecision::Contaminated;
    }
    if (!scope.instance) return BenchmarkRewardDecision::Contaminated; // Pending entry can never suppress a reward.
    return reward.exactLoot && reward.map == scope.map && reward.instance == scope.instance
        && reward.groupLow == scope.groupLow ? BenchmarkRewardDecision::Suppress : BenchmarkRewardDecision::Contaminated;
}

bool BenchmarkLootGuard::Install(BenchmarkLootScope scope)
{
    if (!scope.token || !scope.group || !scope.groupLow || scope.bodies.empty()
        || scope.bodies.size() > 25) return false;
    for (auto const& [guid, body] : scope.bodies)
        if (!guid || !body.ai || !body.Matches(body.player, body.ai, body.session)) return false;
    std::unique_lock lock(_mutex);
    if (_scope) return false; // No replacement, including a retry that would erase contamination evidence.
    _scope = std::move(scope);
    _contaminated = false;
    return true;
}
bool BenchmarkLootGuard::Bind(std::uint64_t token, std::uint32_t instance)
{
    std::unique_lock lock(_mutex);
    if (!_scope || _scope->token != token || !instance || (_scope->instance && _scope->instance != instance)) return false;
    _scope->instance = instance; // One-way binding; never clears pending-entry contamination.
    return true;
}
void BenchmarkLootGuard::Returning(std::uint64_t token)
{
    std::unique_lock lock(_mutex);
    if (_scope && _scope->token == token) _scope->returning = true;
}
void BenchmarkLootGuard::Revoke(std::uint64_t token)
{
    std::unique_lock lock(_mutex);
    if (_scope && _scope->token == token) { _scope.reset(); _contaminated = false; }
}
bool BenchmarkLootGuard::Contaminated(std::uint64_t token) const
{
    std::shared_lock lock(_mutex);
    return _scope && _scope->token == token && _contaminated;
}
BenchmarkLootGuard& BenchmarkRewards() { static BenchmarkLootGuard guard; return guard; }
}
