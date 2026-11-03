// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyLootLedger.h"
#include "TestHarness.h"

#include <atomic>
#include <chrono>
#include <cstdint>
#include <thread>
#include <vector>

using namespace Leeroy::Guild;
using namespace std::chrono_literals;

namespace
{
LootSourceKey const key{631, 80, 1001};
LootSourceObservation const original{100, 200, 300};
LootSourceLedgerState::Time const start{};
}

LEEROY_TEST(loot_ledger_retains_identity_and_separates_maps_instances_entries)
{
    LootSourceLedgerState ledger;
    ledger.Note(key, original, start);
    ledger.Note({649, 80, 1001}, {101, 201, 301}, start);
    ledger.Note({631, 81, 1001}, {102, 202, 302}, start);
    ledger.Note({631, 80, 1002}, {103, 203, 303}, start);
    CHECK(ledger.Find(key, start) == original);
    CHECK_EQ(ledger.Find({649, 80, 1001}, start)->source, 101u);
    CHECK_EQ(ledger.Find({631, 81, 1001}, start)->source, 102u);
    CHECK_EQ(ledger.Find({631, 80, 1002}, start)->source, 103u);
    CHECK(!ledger.Find({631, 82, 1001}, start));
}

LEEROY_TEST(loot_ledger_repeat_callbacks_do_not_extend_a_generation_lifetime)
{
    LootSourceLedgerState ledger;
    ledger.Note(key, original, start);
    ledger.Note(key, original, start + 2h);
    CHECK(ledger.Find(key, start + 3h - 1ns) == original);
    CHECK(!ledger.Find(key, start + 3h));
    ledger.Expire(start + 3h);
    CHECK(!ledger.Find(key, start + 3h));
}

LEEROY_TEST(loot_ledger_distinct_corpses_poison_the_entry_instead_of_replacing_it)
{
    LootSourceLedgerState ledger;
    ledger.Note(key, original, start);
    LootSourceObservation const other{101, original.group, original.generation};
    ledger.Note(key, other, start + 2h);
    CHECK(!ledger.Find(key, start + 2h));
    ledger.Note(key, original, start + 3h);
    ledger.Note(key, other, start + 4h);
    CHECK(!ledger.Find(key, start + 5h - 1ns));
    ledger.Expire(start + 5h);
    CHECK(!ledger.Find(key, start + 5h));
    // Expiry intentionally forgets history: the protocol has no token to distinguish an old command after this.
    ledger.Note(key, other, start + 5h);
    CHECK(ledger.Find(key, start + 5h) == other);
}

LEEROY_TEST(loot_ledger_reused_source_generation_or_recipient_group_is_ambiguous)
{
    for (LootSourceObservation const changed : {LootSourceObservation{100, 200, 301},
                                               LootSourceObservation{100, 201, 300}})
    {
        LootSourceLedgerState ledger;
        ledger.Note(key, original, start);
        ledger.Note(key, changed, start + 1min);
        CHECK(!ledger.Find(key, start + 1min));
        ledger.Note(key, original, start + 2min);
        CHECK(!ledger.Find(key, start + 2min));
    }
}

LEEROY_TEST(loot_ledger_creature_and_chest_guid_collision_is_ambiguous)
{
    LootSourceLedgerState ledger;
    ledger.Note(key, {0xF130000001000064ull, 200, 300}, start);
    ledger.Note(key, {0xF110000001000064ull, 200, 300}, start);
    CHECK(!ledger.Find(key, start));
}

LEEROY_TEST(loot_ledger_world_revalidation_failure_cannot_restore_the_old_observation)
{
    LootSourceLedgerState ledger;
    ledger.Note(key, original, start);
    ledger.Invalidate(key, start + 1h);
    ledger.Note(key, original, start + 2h);
    ledger.Invalidate(key, start + 3h);
    CHECK(!ledger.Find(key, start + 4h - 1ns));
    ledger.Note(key, original, start + 4h);
    CHECK(ledger.Find(key, start + 4h) == original);
}

LEEROY_TEST(loot_ledger_expired_or_missing_invalidation_does_not_create_a_tombstone)
{
    LootSourceLedgerState ledger;
    ledger.Invalidate(key, start);
    ledger.Note(key, original, start);
    ledger.Invalidate(key, start + 3h);
    ledger.Note(key, {101, 201, 301}, start + 3h);
    CHECK_EQ(ledger.Find(key, start + 3h)->source, 101u);
}

LEEROY_TEST(loot_ledger_capacity_drops_oldest_insert_not_lowest_key_or_latest_reopen)
{
    LootSourceLedgerState ledger;
    ledger.Note({631, 80, 9000}, original, start);
    for (std::uint32_t i = 1; i < LootSourceLedgerState::Capacity; ++i)
        ledger.Note({631, 80, i}, {i, 200, 300}, start);
    ledger.Note({631, 80, 9000}, original, start + 1h);
    ledger.Note({631, 80, 10000}, {10000, 200, 300}, start + 1h);
    CHECK(!ledger.Find({631, 80, 9000}, start + 1h));
    CHECK(ledger.Find({631, 80, 1}, start + 1h));
    CHECK(ledger.Find({631, 80, 10000}, start + 1h));
    ledger.Note({631, 80, 10001}, {10001, 200, 300}, start + 1h);
    CHECK(!ledger.Find({631, 80, 1}, start + 1h));
    CHECK(ledger.Find({631, 80, 2}, start + 1h));
}

LEEROY_TEST(loot_ledger_expiry_prunes_before_capacity_eviction)
{
    LootSourceLedgerState ledger;
    for (std::uint32_t i = 1; i <= LootSourceLedgerState::Capacity; ++i)
        ledger.Note({631, 80, i}, {i, 200, 300}, start);
    ledger.Note({631, 80, 2000}, {2000, 200, 300}, start + 3h);
    CHECK(!ledger.Find({631, 80, 1}, start + 3h));
    CHECK(ledger.Find({631, 80, 2000}, start + 3h));
    // Only the new generation should still exist one nanosecond before its own expiry.
    ledger.Expire(start + 6h - 1ns);
    CHECK(ledger.Find({631, 80, 2000}, start + 6h - 1ns));
    ledger.Expire(start + 6h);
    CHECK(!ledger.Find({631, 80, 2000}, start + 6h));
}

LEEROY_TEST(loot_ledger_empty_guid_cannot_create_or_replace_an_observation)
{
    LootSourceLedgerState ledger;
    ledger.Note(key, {}, start);
    CHECK(!ledger.Find(key, start));
    ledger.Note(key, original, start);
    ledger.Note(key, {}, start + 1h);
    CHECK(ledger.Find(key, start + 1h) == original);
}

LEEROY_TEST(loot_ledger_concurrent_map_callbacks_and_world_reads_keep_a_single_identity)
{
    LootSourceLedgerState ledger;
    std::atomic<bool> inconsistent{false};
    std::vector<std::thread> workers;
    for (int worker = 0; worker < 8; ++worker)
        workers.emplace_back([&ledger, &inconsistent]
        {
            for (int i = 0; i < 200; ++i)
            {
                ledger.Note(key, original, start);
                if (ledger.Find(key, start) != original)
                    inconsistent = true;
                ledger.Expire(start);
            }
        });
    for (auto& worker : workers)
        worker.join();
    CHECK(!inconsistent.load());
    CHECK(ledger.Find(key, start) == original);
}

LEEROY_TEST(loot_ledger_concurrent_conflicting_callbacks_never_choose_a_winner)
{
    LootSourceLedgerState ledger;
    std::vector<std::thread> workers;
    for (std::uint64_t worker = 1; worker <= 8; ++worker)
        workers.emplace_back([&ledger, worker]
        {
            for (int i = 0; i < 100; ++i)
                ledger.Note(key, {worker, 200, 300}, start);
        });
    for (auto& worker : workers)
        worker.join();
    CHECK(!ledger.Find(key, start));
    ledger.Note(key, original, start + 1h);
    CHECK(!ledger.Find(key, start + 1h));
}
