// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyTrainingSlot.h"
#include "LeeroyTrainingProtocol.h"
#include "LeeroyPullRecorder.h"
#include "TestHarness.h"
#include <atomic>
#include <memory>
#include <thread>
#include <vector>

using namespace Leeroy::Training;

LEEROY_TEST(Training_ArmPolicy)
{
    ArmInputs base{true, true, true, true, false, true, 631, 0};
    CHECK(ShouldArm(base));
    for (unsigned flag = 0; flag < 3; ++flag)
    {
        auto in = base;
        if (flag == 0) in.isRaid = false;
        if (flag == 1) in.telemetryOn = false;
        if (flag == 2) in.mindArmed = false;
        CHECK(!ShouldArm(in));
    }
    auto in = base;
    in.trackedOnMap = false;
    CHECK(!ShouldArm(in));
    in.registered = true;
    CHECK(ShouldArm(in));
    in.bossAllowed = false;
    CHECK(!ShouldArm(in));
    for (auto [map, boss] : kNonFightSteps)
    {
        in = base;
        in.map = map;
        in.bossId = boss;
        CHECK(!ShouldArm(in));
    }
    CHECK(!ShouldAbort(0, 1199999));
    CHECK(ShouldAbort(0, 1200000));
    CHECK(!ShouldAbort(100, 99));
    CHECK(ShouldAbort(100, std::numeric_limits<std::uint64_t>::max()));
}

LEEROY_TEST(Training_SlotPublicationHidesUnpreparedTablesAndRejectsStaleCompletion)
{
    SlotPublication slot;
    auto first = slot.Claim(123);
    CHECK(first.has_value());
    if (!first) return;
    CHECK(!slot.Snapshot());
    CHECK(!slot.Claim(456));
    CHECK(!slot.Ready(*first));
    CHECK(slot.Publish(*first));
    CHECK(slot.Snapshot() == first);
    CHECK(!slot.Publish(*first));
    CHECK(!slot.Ready(*first));
    CHECK(slot.Retire(*first));
    CHECK(!slot.Snapshot());
    CHECK(!slot.Claim(456)); // Must replace/reset table before making its slot available.
    CHECK(slot.Ready(*first));
    auto second = slot.Claim(123); // Same instance, different pull generation.
    CHECK(second.has_value());
    if (!second) return;
    CHECK(second->generation > first->generation);
    CHECK(slot.Publish(*second));
    CHECK(!slot.Retire(*first));
    CHECK(!slot.Ready(*first));
    CHECK(slot.Snapshot() == second);
    CHECK(slot.Retire(*second));
    CHECK(slot.Ready(*second));
    auto cancelled = slot.Claim(789);
    CHECK(cancelled.has_value());
    if (!cancelled) return;
    CHECK(slot.CancelClaim(*cancelled));
    CHECK(!slot.Publish(*cancelled));
    CHECK(slot.Ready(*cancelled));
    auto mainland = slot.Claim(0); // Map0/instance0 is a real outdoor dummy location; phase encodes vacancy.
    CHECK(mainland.has_value());
    if (!mainland) return;
    CHECK(slot.Publish(*mainland));
    CHECK(slot.Snapshot() == mainland);
}

LEEROY_TEST(Training_SlotConcurrentClaimsHaveOneOwner)
{
    SlotPublication slot;
    std::atomic<unsigned> arrived{0}, winners{0};
    std::atomic<bool> go{false};
    std::vector<std::thread> threads;
    for (unsigned i = 1; i <= 16; ++i)
        threads.emplace_back([&, i]
        {
            arrived.fetch_add(1);
            while (!go.load()) std::this_thread::yield();
            if (auto token = slot.Claim(i))
            {
                winners.fetch_add(1);
                slot.Publish(*token);
            }
        });
    while (arrived.load() != 16) std::this_thread::yield();
    go.store(true);
    for (auto& thread : threads) thread.join();
    CHECK_EQ(winners.load(), 1);
    CHECK(slot.Snapshot().has_value());
}

LEEROY_TEST(Training_ArmOnlyReheadersPristinePreparedTable)
{
    auto table = std::make_unique<PullTable>();
    PullHeader first{631, 1, 2, 0, 10, 1000, Source::Tracked};
    CHECK(!table->Arm(first));
    table->Reset({});
    CHECK(table->Arm(first));
    CHECK(!table->Arm(first));
    CHECK_EQ(table->Header().instanceId, 1);
    table->Member(1, 1, 0);
    table->Close(Outcome::Killed, 2000, 10);
    CHECK(!table->Arm(first));
    table->Reset({});
    table->Member(1, 1, 0);
    CHECK(!table->Arm(first));
    table->Reset({});
    table->SetBoss(9);
    CHECK(!table->Arm(first));
    table->Reset({});
    table->MarkAssisted();
    CHECK(!table->Arm(first));
    table->Reset({});
    CHECK(table->Arm(first));
    CHECK_EQ(table->MemberCount(), 0);
    CHECK_EQ(table->BossEntry(), 0);
}
