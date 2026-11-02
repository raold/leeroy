// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyBenchmarkLoot.h"
#include "TestHarness.h"
#include <atomic>
#include <thread>

using namespace Leeroy::Training;
namespace
{
BenchmarkLootScope Scope()
{
    return {71, 9001, 1, 631, 88, {{101, {11, 12, 13}}, {102, {21, 22, 23}}}, false};
}
BenchmarkLootActor Actor() { return {101, 631, 88, 9001, 11, 12, 13, {101, 102}}; }
BenchmarkReward Reward() { return {BenchmarkRewardKind::Creature, 631, 88, 1, true}; }
}

LEEROY_TEST(benchmark_reward_requires_exact_body_group_instance_and_source)
{
    auto scope = Scope();
    auto actor = Actor();
    auto reward = Reward();
    CHECK(ClassifyBenchmarkReward(scope, actor, reward) == BenchmarkRewardDecision::Suppress);
    reward.kind = BenchmarkRewardKind::GameObject;
    CHECK(ClassifyBenchmarkReward(scope, actor, reward) == BenchmarkRewardDecision::Suppress);
    for (auto field : {&BenchmarkLootActor::guid, &BenchmarkLootActor::map, &BenchmarkLootActor::instance})
    {
        auto other = actor;
        ++(other.*field);
        CHECK(ClassifyBenchmarkReward(scope, other, reward) == BenchmarkRewardDecision::Ignore);
    }
    for (auto field : {&BenchmarkLootActor::player, &BenchmarkLootActor::ai, &BenchmarkLootActor::session})
    {
        auto other = actor;
        ++(other.*field);
        CHECK(ClassifyBenchmarkReward(scope, other, reward) == BenchmarkRewardDecision::Ignore);
    }
    actor.group = 9002;
    CHECK(ClassifyBenchmarkReward(scope, actor, reward) == BenchmarkRewardDecision::Ignore);
}

LEEROY_TEST(benchmark_never_suppresses_shared_rewards_for_a_newly_configured_human)
{
    BenchmarkLootGuard guard;
    CHECK(guard.Install(Scope()));
    auto actor = Actor();
    actor.currentHuman = 102; // Another captured member, not the currently looting actor.
    int erased = 0;
    CHECK(guard.Observe(actor, Reward(), [&] { ++erased; }) == BenchmarkRewardDecision::Contaminated);
    CHECK_EQ(erased, 0);
    CHECK(guard.Contaminated(71));
    actor.currentHuman = actor.guid;
    CHECK(guard.Observe(actor, Reward(), [&] { ++erased; }) == BenchmarkRewardDecision::Contaminated);
    actor.guid = 999; // Foreign actor stays outside this guard entirely.
    CHECK(guard.Observe(actor, Reward(), [&] { ++erased; }) == BenchmarkRewardDecision::Ignore);
    CHECK_EQ(erased, 0);
}

LEEROY_TEST(benchmark_unproven_reward_is_contaminated_without_erasing_it)
{
    auto scope = Scope();
    auto actor = Actor();
    auto reward = Reward();
    for (auto field : {&BenchmarkReward::map, &BenchmarkReward::instance, &BenchmarkReward::groupLow})
    {
        auto foreign = reward;
        ++(foreign.*field);
        CHECK(ClassifyBenchmarkReward(scope, actor, foreign) == BenchmarkRewardDecision::Contaminated);
    }
    reward.groupLow = 0; // Unclaimed/public GO is not this job's loot to erase.
    CHECK(ClassifyBenchmarkReward(scope, actor, reward) == BenchmarkRewardDecision::Contaminated);
    reward = Reward();
    reward.exactLoot = false;
    CHECK(ClassifyBenchmarkReward(scope, actor, reward) == BenchmarkRewardDecision::Contaminated);
    BenchmarkLootGuard guard;
    CHECK(guard.Install(scope));
    int erased = 0;
    CHECK(guard.Observe(actor, reward, [&] { ++erased; }) == BenchmarkRewardDecision::Contaminated);
    CHECK_EQ(erased, 0);
    CHECK(guard.Contaminated(71));
}

LEEROY_TEST(benchmark_foreign_actor_and_inventory_container_never_mutate_or_contaminate)
{
    BenchmarkLootGuard guard;
    CHECK(guard.Install(Scope()));
    int erased = 0;
    auto actor = Actor();
    actor.guid = 300;
    CHECK(guard.Observe(actor, Reward(), [&] { ++erased; }) == BenchmarkRewardDecision::Ignore);
    for (auto kind : {BenchmarkRewardKind::Other, BenchmarkRewardKind::Inventory})
    {
        auto reward = Reward();
        reward.kind = kind;
        CHECK(guard.Observe(Actor(), reward, [&] { ++erased; }) == BenchmarkRewardDecision::Ignore);
    }
    CHECK_EQ(erased, 0);
    CHECK(!guard.Contaminated(71));
}

LEEROY_TEST(benchmark_reward_rejects_an_outsider_or_duplicated_live_roster)
{
    auto actor = Actor();
    actor.roster = {101, 333};
    CHECK(ClassifyBenchmarkReward(Scope(), actor, Reward()) == BenchmarkRewardDecision::Contaminated);
    actor.roster = {101, 101};
    CHECK(ClassifyBenchmarkReward(Scope(), actor, Reward()) == BenchmarkRewardDecision::Contaminated);
    actor.roster = {101};
    CHECK(ClassifyBenchmarkReward(Scope(), actor, Reward()) == BenchmarkRewardDecision::Contaminated);
}

LEEROY_TEST(benchmark_return_scope_matches_only_its_own_groupless_captured_body)
{
    auto actor = Actor();
    actor.group = 0;
    actor.roster.clear();
    CHECK(ClassifyBenchmarkReward(Scope(), actor, Reward()) == BenchmarkRewardDecision::Ignore);
    auto scope = Scope();
    scope.returning = true;
    CHECK(ClassifyBenchmarkReward(scope, actor, Reward()) == BenchmarkRewardDecision::Suppress);
    actor.group = 9002;
    CHECK(ClassifyBenchmarkReward(scope, actor, Reward()) == BenchmarkRewardDecision::Ignore);
    actor.group = 0;
    ++actor.session;
    CHECK(ClassifyBenchmarkReward(scope, actor, Reward()) == BenchmarkRewardDecision::Ignore);
}

LEEROY_TEST(benchmark_loot_generations_cannot_replace_or_clear_another_scope)
{
    BenchmarkLootGuard guard;
    CHECK(guard.Install(Scope()));
    auto next = Scope();
    next.token = 72;
    CHECK(!guard.Install(next));
    guard.Revoke(72);
    int suppressed = 0;
    CHECK(guard.Observe(Actor(), Reward(), [&] { ++suppressed; }) == BenchmarkRewardDecision::Suppress);
    guard.Revoke(71);
    CHECK(guard.Observe(Actor(), Reward(), [&] { ++suppressed; }) == BenchmarkRewardDecision::Ignore);
    CHECK_EQ(suppressed, 1);
    CHECK(guard.Install(next));
    guard.Revoke(71);
    CHECK(guard.Observe(Actor(), Reward(), [&] { ++suppressed; }) == BenchmarkRewardDecision::Suppress);
}

LEEROY_TEST(benchmark_loot_snapshot_rejects_incomplete_or_unbounded_ownership)
{
    BenchmarkLootGuard guard;
    auto scope = Scope();
    scope.group = 0;
    CHECK(!guard.Install(scope));
    scope = Scope();
    scope.bodies.at(101).Revoke();
    CHECK(!guard.Install(scope));
    scope = Scope();
    scope.bodies.clear();
    CHECK(!guard.Install(scope));
    for (unsigned i = 1; i <= 26; ++i) scope.bodies.emplace(i, Leeroy::Guild::RaidBodyLease{11, 12, 13});
    CHECK(!guard.Install(scope));
}

LEEROY_TEST(benchmark_pending_entry_marks_rewards_without_suppressing_and_binding_keeps_the_failure)
{
    BenchmarkLootGuard guard;
    auto scope = Scope();
    scope.instance = 0;
    CHECK(guard.Install(scope));
    int erased = 0;
    CHECK(guard.Observe(Actor(), Reward(), [&] { ++erased; }) == BenchmarkRewardDecision::Contaminated);
    CHECK_EQ(erased, 0);
    CHECK(guard.Contaminated(71));
    CHECK(guard.Bind(71, 88));
    CHECK(guard.Contaminated(71));
    CHECK(!guard.Bind(71, 89));
    CHECK(!guard.Bind(72, 88));
    CHECK(guard.Observe(Actor(), Reward(), [&] { ++erased; }) == BenchmarkRewardDecision::Suppress);
    CHECK_EQ(erased, 1);
}

LEEROY_TEST(benchmark_pending_entry_ignores_origin_rewards_and_new_sessions)
{
    BenchmarkLootGuard guard;
    auto scope = Scope();
    scope.instance = 0;
    CHECK(guard.Install(scope));
    auto actor = Actor();
    actor.map = 1;
    actor.instance = 0;
    int erased = 0;
    CHECK(guard.Observe(actor, Reward(), [&] { ++erased; }) == BenchmarkRewardDecision::Ignore);
    actor = Actor();
    ++actor.session;
    CHECK(guard.Observe(actor, Reward(), [&] { ++erased; }) == BenchmarkRewardDecision::Ignore);
    CHECK(!guard.Contaminated(71));
    CHECK(guard.Bind(71, 88));
    CHECK(guard.Observe(Actor(), Reward(), [&] { ++erased; }) == BenchmarkRewardDecision::Suppress);
    CHECK_EQ(erased, 1);
}

LEEROY_TEST(benchmark_loot_revocation_waits_for_the_scoped_mutation_to_finish)
{
    BenchmarkLootGuard guard;
    CHECK(guard.Install(Scope()));
    std::atomic<bool> inside{false}, release{false}, revoked{false};
    std::thread callback([&]
    {
        guard.Observe(Actor(), Reward(), [&]
        {
            inside = true;
            while (!release.load()) std::this_thread::yield();
        });
    });
    while (!inside.load()) std::this_thread::yield();
    std::thread revoke([&] { guard.Revoke(71); revoked = true; });
    bool early = revoked.load();
    release = true;
    callback.join();
    revoke.join();
    CHECK(!early);
    CHECK(revoked.load());
    int mutations = 0;
    guard.Observe(Actor(), Reward(), [&] { ++mutations; });
    CHECK_EQ(mutations, 0);
}
