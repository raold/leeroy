// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_BENCHMARK_LOOT_H
#define MOD_LEEROY_BENCHMARK_LOOT_H
#include "LeeroyRaidRecovery.h"
#include <map>
#include <mutex>
#include <optional>
#include <shared_mutex>
#include <vector>

namespace Leeroy::Training
{
struct BenchmarkLootScope
{
    std::uint64_t token = 0, group = 0;
    std::uint32_t groupLow = 0, map = 0, instance = 0;
    std::map<std::uint32_t, Guild::RaidBodyLease> bodies;
    bool returning = false;
};
struct BenchmarkLootActor
{
    std::uint32_t guid = 0, map = 0, instance = 0;
    std::uint64_t group = 0;
    std::uintptr_t player = 0, ai = 0, session = 0;
    std::vector<std::uint32_t> roster;
    std::uint32_t currentHuman = 0;
};
enum class BenchmarkRewardKind { Other, Creature, GameObject, Inventory };
struct BenchmarkReward
{
    BenchmarkRewardKind kind = BenchmarkRewardKind::Other;
    std::uint32_t map = 0, instance = 0, groupLow = 0;
    bool exactLoot = false;
};
enum class BenchmarkRewardDecision { Ignore, Suppress, Contaminated };
BenchmarkRewardDecision ClassifyBenchmarkReward(BenchmarkLootScope const&, BenchmarkLootActor const&,
    BenchmarkReward const&);

// One bounded immutable ownership snapshot, copied by the world thread. Callbacks never dereference the job.
// The callback runs under the lock so a revoked/replaced generation cannot race reward mutation.
class BenchmarkLootGuard
{
public:
    bool Install(BenchmarkLootScope scope);
    bool Bind(std::uint64_t token, std::uint32_t instance);
    void Returning(std::uint64_t token);
    void Revoke(std::uint64_t token);
    bool Contaminated(std::uint64_t token) const;
    template<class Apply> BenchmarkRewardDecision Observe(BenchmarkLootActor const& actor,
        BenchmarkReward const& reward, Apply&& apply)
    {
        std::unique_lock lock(_mutex);
        if (!_scope) return BenchmarkRewardDecision::Ignore;
        auto decision = ClassifyBenchmarkReward(*_scope, actor, reward);
        if (decision == BenchmarkRewardDecision::Contaminated) _contaminated = true;
        if (decision == BenchmarkRewardDecision::Suppress) apply();
        return decision;
    }
private:
    mutable std::shared_mutex _mutex;
    std::optional<BenchmarkLootScope> _scope;
    bool _contaminated = false;
};
BenchmarkLootGuard& BenchmarkRewards();
}
#endif
