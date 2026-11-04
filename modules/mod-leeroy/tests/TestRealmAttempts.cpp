// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmAttempts.h"
#include "TestHarness.h"

#include <cstdint>
#include <tuple>
#include <vector>

using namespace Leeroy::Realm;

namespace
{
using P = EncounterPhase;
constexpr InstanceKey Icc{631, 500};
constexpr std::uint32_t Boss = 3;
constexpr std::uint8_t Mode = 1;

AttemptLedger Witnessed(InstanceKey key = Icc)
{
    AttemptLedger ledger;
    ledger.Synchronize(7, 21, 1);
    ledger.Created(key, true);
    return ledger;
}

// A pull the map update confirms, then (optionally) the wipe intent.
void Pull(AttemptLedger& ledger, std::uint64_t at, bool wipe = true, InstanceKey key = Icc,
    std::uint8_t mode = Mode, P from = P::Resting)
{
    ledger.Intent(key, Boss, mode, from, P::Progress, from, true, at);
    ledger.ObserveActual(key, Boss, mode, P::Progress, at + 20);
    if (wipe) ledger.Intent(key, Boss, mode, P::Progress, P::Resting, P::Progress, true, at + 1000);
}

std::uint32_t Kill(AttemptLedger& ledger, std::uint64_t at, InstanceKey key = Icc, std::uint8_t mode = Mode)
{
    ledger.Intent(key, Boss, mode, P::Progress, P::Done, P::Progress, true, at);
    return ledger.Kill(key, Boss, mode, at + 20);
}
}

LEEROY_TEST(RealmAttempts_CountsEveryConfirmedPullOfAWitnessedInstance)
{
    auto ledger = Witnessed();
    Pull(ledger, 10000); Pull(ledger, 20000);
    Pull(ledger, 30000, false);
    CHECK_EQ(Kill(ledger, 31000), 3u);
    CHECK_EQ(ledger.Kill(Icc, Boss, Mode, 31100), 0u); // A boss is consumed by its first verified kill.
    CHECK_EQ(ledger.Pending(), 0u);
}

LEEROY_TEST(RealmAttempts_FirstPullOfAFreshBossFromUnsetCounts)
{
    auto ledger = Witnessed();
    ledger.Intent(Icc, Boss, Mode, P::Unset, P::Resting, P::Unset, false, 100); // Script initialization.
    Pull(ledger, 1000, false, Icc, Mode, P::Unset);
    CHECK_EQ(Kill(ledger, 2000), 1u);
}

LEEROY_TEST(RealmAttempts_FastKillIsConfirmedByTheTerminalHookItself)
{
    auto ledger = Witnessed();
    ledger.Intent(Icc, Boss, Mode, P::Resting, P::Progress, P::Resting, true, 1000);
    // DONE intent before any map update: its own pre-transition read is Progress.
    ledger.Intent(Icc, Boss, Mode, P::Progress, P::Done, P::Progress, true, 1010);
    CHECK_EQ(ledger.Kill(Icc, Boss, Mode, 1020), 1u);
}

LEEROY_TEST(RealmAttempts_OnlyAWitnessedInstanceUnderTheSameIdentityCounts)
{
    AttemptLedger notCreated; notCreated.Synchronize(7, 21, 1);
    Pull(notCreated, 1000, false);
    CHECK_EQ(Kill(notCreated, 2000), 0u);
    AttemptLedger loaded; loaded.Synchronize(7, 21, 1);
    loaded.Created(Icc, false); // A saved instance: earlier pulls may predate this process.
    Pull(loaded, 1000, false);
    CHECK_EQ(Kill(loaded, 2000), 0u);
    CHECK_EQ(loaded.Instances(), 0u);
    for (auto change : {0, 1, 2})
    {
        auto ledger = Witnessed();
        Pull(ledger, 1000);
        ledger.Synchronize(change == 0 ? 8 : 7, change == 1 ? 22 : 21, change == 2 ? 2 : 1);
        Pull(ledger, 5000, false);
        CHECK_EQ(Kill(ledger, 6000), 0u); // Realm toggle, guild or human change forgets every witness.
    }
    auto same = Witnessed();
    Pull(same, 1000);
    same.Synchronize(7, 21, 1); // An unchanged identity keeps the count.
    Pull(same, 5000, false);
    CHECK_EQ(Kill(same, 6000), 2u);
}

LEEROY_TEST(RealmAttempts_MapReloadInOneLockoutKeepsTheWitness)
{
    auto ledger = Witnessed();
    Pull(ledger, 1000);
    Pull(ledger, 5000, false); // Engaged when everyone left: the core reloads it as NOT_STARTED.
    ledger.Unloaded(Icc);
    ledger.Created(Icc, false);
    Pull(ledger, 900000, false);
    CHECK_EQ(Kill(ledger, 901000), 3u);
    // A fresh creation of a known id is a new instance.
    ledger.Created(Icc, true);
    Pull(ledger, 950000, false);
    CHECK_EQ(Kill(ledger, 951000), 1u);
}

LEEROY_TEST(RealmAttempts_VetoedStartIsNoAttemptButAnUnconfirmedOneIsUnknown)
{
    auto ledger = Witnessed();
    ledger.Intent(Icc, Boss, Mode, P::Resting, P::Progress, P::Resting, true, 1000);
    ledger.ObserveActual(Icc, Boss, Mode, P::Resting, 1020); // The core never applied it.
    Pull(ledger, 2000, false);
    CHECK_EQ(Kill(ledger, 3000), 1u);
    for (std::uint64_t late : {6001u, 999u})
    {
        auto stale = Witnessed();
        stale.Intent(Icc, Boss, Mode, P::Resting, P::Progress, P::Resting, true, 1000);
        stale.ObserveActual(Icc, Boss, Mode, P::Progress, late); // Too late, or a clock reversal.
        CHECK_EQ(Kill(stale, 7000), 0u);
    }
    auto expired = Witnessed();
    expired.Intent(Icc, Boss, Mode, P::Resting, P::Progress, P::Resting, true, 1000);
    CHECK_EQ(expired.Unconfirmed(Icc).size(), 1u);
    expired.Intent(Icc, 9, Mode, P::Resting, P::Resting, P::Resting, true, 6001); // Any later call expires it.
    CHECK_EQ(expired.Pending(), 0u);
    CHECK_EQ(Kill(expired, 7000), 0u);
    auto unloaded = Witnessed();
    unloaded.Intent(Icc, Boss, Mode, P::Resting, P::Progress, P::Resting, true, 1000);
    unloaded.Unloaded(Icc); unloaded.Created(Icc, false);
    Pull(unloaded, 2000, false);
    CHECK_EQ(Kill(unloaded, 3000), 0u);
}

LEEROY_TEST(RealmAttempts_UnqualifiedOrOutOfModelTransitionsMakeTheBossUnknown)
{
    auto unqualified = Witnessed();
    unqualified.Intent(Icc, Boss, Mode, P::Resting, P::Progress, P::Resting, false, 1000);
    unqualified.ObserveActual(Icc, Boss, Mode, P::Progress, 1020);
    Pull(unqualified, 5000, false);
    CHECK_EQ(Kill(unqualified, 6000), 0u);
    for (auto const& [old, next, actual] : std::vector<std::tuple<P, P, P>>{
        {P::Progress, P::Special, P::Progress}, {P::Special, P::Progress, P::Special},
        {P::Resting, P::Done, P::Resting}, {P::Unset, P::Done, P::Unset}, {P::Done, P::Resting, P::Done},
        {P::Resting, P::Progress, P::Done}, {P::Progress, P::Resting, P::Resting}})
    {
        auto ledger = Witnessed();
        Pull(ledger, 1000);
        ledger.Intent(Icc, Boss, Mode, old, next, actual, true, 2000);
        Pull(ledger, 3000, false);
        CHECK_EQ(Kill(ledger, 4000), 0u);
    }
    auto missedWipe = Witnessed();
    Pull(missedWipe, 1000, false);
    Pull(missedWipe, 2000, false); // A start while engaged: a wipe went unseen.
    CHECK_EQ(Kill(missedWipe, 3000), 0u);
    auto noEngagement = Witnessed();
    Pull(noEngagement, 1000); // Closed by its wipe; the DONE has no open pull.
    CHECK_EQ(Kill(noEngagement, 3000), 0u);
    auto other = Witnessed();
    other.Intent(Icc, 7, Mode, P::Progress, P::Special, P::Progress, true, 500); // Another boss only.
    Pull(other, 1000, false);
    CHECK_EQ(Kill(other, 2000), 1u);
}

LEEROY_TEST(RealmAttempts_ModesAndInstancesAreSeparateAndTheCapIsExact)
{
    auto ledger = Witnessed();
    Pull(ledger, 1000); Pull(ledger, 2000, true, Icc, 3);
    Pull(ledger, 3000, false, Icc, 3);
    CHECK_EQ(Kill(ledger, 4000, Icc, 3), 2u); // 25 heroic pulls only.
    Pull(ledger, 5000, false);
    CHECK_EQ(Kill(ledger, 6000), 2u);
    auto capped = Witnessed();
    for (std::uint32_t pull = 0; pull < kMaxAttempts - 1; ++pull) Pull(capped, 1000 + pull * 2000);
    Pull(capped, 900000, false);
    CHECK_EQ(Kill(capped, 901000), kMaxAttempts);
    auto over = Witnessed();
    for (std::uint32_t pull = 0; pull < kMaxAttempts; ++pull) Pull(over, 1000 + pull * 2000);
    Pull(over, 900000, false); // The hundredth pull: unknown, never 99.
    CHECK_EQ(Kill(over, 901000), 0u);
}

LEEROY_TEST(RealmAttempts_CapacitiesForgetConservatively)
{
    AttemptLedger ledger; ledger.Synchronize(7, 21, 1);
    ledger.Created(Icc, true);
    for (std::uint32_t id = 1; id < AttemptLedger::InstanceCapacity; ++id) ledger.Created({36, id}, true);
    Pull(ledger, 1000); // Touched: this oldest creation is now the most recent use.
    ledger.Created({36, 99999}, true); // Evicts {36, 1}, the least recently used.
    CHECK_EQ(ledger.Instances(), AttemptLedger::InstanceCapacity);
    Pull(ledger, 2000, false);
    CHECK_EQ(Kill(ledger, 3000), 2u);
    Pull(ledger, 4000, false, {36, 1});
    CHECK_EQ(Kill(ledger, 5000, {36, 1}), 0u);
    Pull(ledger, 6000, false, {36, 2});
    CHECK_EQ(Kill(ledger, 7000, {36, 2}), 1u);
    auto bosses = Witnessed();
    for (std::uint32_t boss = 100; boss < 100 + AttemptLedger::BossCapacity; ++boss)
        bosses.Intent(Icc, boss, Mode, P::Unset, P::Resting, P::Unset, true, 10);
    bosses.Intent(Icc, 999, Mode, P::Unset, P::Resting, P::Unset, true, 10); // Overflow: the whole instance.
    Pull(bosses, 1000, false);
    CHECK_EQ(Kill(bosses, 2000), 0u);
}
