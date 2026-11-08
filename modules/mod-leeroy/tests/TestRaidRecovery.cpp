// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRaidRecovery.h"
#include "TestHarness.h"
#include <limits>
#include <array>
#include <stdexcept>

using namespace Leeroy::Guild;

LEEROY_TEST(benchmark_one_final_reset_fault_does_not_strand_the_other_released_members)
{
    std::array<RaidFinalReset, 3> receipts;
    std::array<int, 3> attempts{};
    int failures = 0;
    for (unsigned i = 0; i < receipts.size(); ++i)
        if (!receipts[i].TryRun([&, i]
            {
                ++attempts[i];
                if (!i) throw std::runtime_error("first member reset failed");
            })) ++failures;
    CHECK_EQ(failures, 1);
    CHECK(!receipts[0].Complete());
    CHECK(receipts[1].Complete() && receipts[2].Complete());
    for (unsigned i = 0; i < receipts.size(); ++i)
        CHECK(receipts[i].TryRun([&, i] { ++attempts[i]; }));
    CHECK(receipts[0].Complete());
    CHECK_EQ(attempts[0], 2);
    CHECK_EQ(attempts[1], 1);
    CHECK_EQ(attempts[2], 1);
}

LEEROY_TEST(benchmark_final_reset_receipt_survives_partial_reset_and_retries_without_a_master)
{
    RaidFinalReset reset;
    bool masterReleased = true;
    int strategies = 0, engines = 0;
    try
    {
        reset.Run([&] { ++strategies; ++engines; throw std::runtime_error("engine reset failed"); });
    }
    catch (std::runtime_error const&) { }
    CHECK(masterReleased);
    CHECK(!reset.Complete());
    reset.Run([&] { ++strategies; ++engines; });
    CHECK(reset.Complete());
    CHECK_EQ(strategies, 2);
    CHECK_EQ(engines, 2);
    reset.Run([&] { ++strategies; ++engines; });
    CHECK_EQ(strategies, 2);
    CHECK_EQ(engines, 2);
}

LEEROY_TEST(benchmark_reset_failure_does_not_renew_the_cleanup_deadline_or_create_a_second_terminal)
{
    RaidFinalReset reset;
    RaidCleanupTerminal terminal;
    try { reset.Run([] { throw std::runtime_error("strategy reset failed"); }); }
    catch (std::runtime_error const&) { }
    CHECK(!reset.Complete());
    CHECK(terminal.Observe(false, 60000) == RaidCleanupReport::Failed);
    reset.Run([] { });
    CHECK(reset.Complete());
    CHECK(terminal.Observe(true, 60001) == RaidCleanupReport::None);
}

LEEROY_TEST(benchmark_binding_admission_preserves_existing_saves)
{
    CHECK(!BenchmarkBindAllowed(631, 77, 0, true, 631)); // No permanent target-map lockout may be cleared.
    CHECK(!BenchmarkBindAllowed(631, 77, 1, false, 631));
    CHECK(!BenchmarkBindAllowed(533, 44, 0, false, 631)); // Joining also drops foreign temporary saves.
    CHECK(BenchmarkBindAllowed(533, 44, 1, true, 631));
    CHECK(!BenchmarkBindAllowed(533, 0, 0, true, 631));
}

LEEROY_TEST(benchmark_cleanup_permits_only_its_exact_temporary_trial_binding)
{
    CHECK(BenchmarkBindAllowed(631, 88, 1, false, 631, 88, 1));
    CHECK(BenchmarkBindAllowed(631, 88, 1, true, 631, 88, 1));
    CHECK(!BenchmarkBindAllowed(631, 89, 1, false, 631, 88, 1));
    CHECK(!BenchmarkBindAllowed(631, 88, 0, false, 631, 88, 1));
    CHECK(!BenchmarkBindAllowed(533, 44, 1, false, 631, 88, 1));
    CHECK(BenchmarkBindAllowed(533, 44, 1, true, 631, 88, 1));
    CHECK(!BenchmarkBindAllowed(631, 89, 1, true, 631, 88, 1));
}

LEEROY_TEST(benchmark_cleanup_requires_the_exact_player_ai_and_session_incarnation)
{
    RaidBodyLease body{101, 201, 301};
    CHECK(body.Matches(101, 201, 301));
    CHECK(!body.Matches(102, 201, 301));
    CHECK(!body.Matches(101, 202, 301));
    CHECK(!body.Matches(101, 201, 302));
    CHECK(!RaidBodyLease{}.Matches(0, 0, 0));
    RaidBodyLease driver{501, 0, 601};
    CHECK(driver.Matches(501, 0, 601));
    CHECK(!driver.Matches(501, 0, 602));
}

LEEROY_TEST(benchmark_logout_revocation_rejects_reused_pointer_values)
{
    RaidBodyLease old{101, 201, 301};
    RaidBodyLease other{102, 202, 302};
    old.Revoke();
    old.Revoke();
    CHECK(!old.Matches(101, 201, 301));
    CHECK(other.Matches(102, 202, 302));
    // Only a newly admitted job may create a new lease after the old recovery was reconciled.
    RaidBodyLease next{101, 201, 301};
    CHECK(next.Matches(101, 201, 301));
}

LEEROY_TEST(benchmark_failed_terminal_is_bounded_but_never_implies_recovery_complete)
{
    RaidCleanupTerminal terminal;
    CHECK(terminal.Observe(false, 59999) == RaidCleanupReport::None);
    CHECK(!terminal.Reported());
    CHECK(terminal.Observe(false, 60000) == RaidCleanupReport::Failed);
    CHECK(terminal.Reported());
    CHECK(terminal.Observe(false, std::numeric_limits<std::uint32_t>::max()) == RaidCleanupReport::None);
    CHECK(terminal.Observe(true, 90000) == RaidCleanupReport::None); // Later recovery cannot publish another terminal.
}

LEEROY_TEST(benchmark_successful_cleanup_has_exactly_one_terminal_even_at_deadline)
{
    RaidCleanupTerminal terminal;
    CHECK(terminal.Observe(true, 60000) == RaidCleanupReport::Complete);
    CHECK(terminal.Observe(true, 60001) == RaidCleanupReport::None);
    CHECK(terminal.Observe(false, 60001) == RaidCleanupReport::None);
}

LEEROY_TEST(offline_cleanup_reports_failure_without_claiming_recovery_or_reporting_twice)
{
    RaidCleanupTerminal offline;
    CHECK(offline.Observe(false, 59999) == RaidCleanupReport::None);
    CHECK(offline.Observe(false, 60000) == RaidCleanupReport::Failed);
    CHECK(offline.Reported());
    CHECK(offline.Observe(false, 90000) == RaidCleanupReport::None);
    CHECK(offline.Observe(true, 90001) == RaidCleanupReport::None);
}
