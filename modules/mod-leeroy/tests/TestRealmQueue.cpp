// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmQueue.h"
#include "TestHarness.h"
#include <map>
#include <stdexcept>

using namespace Leeroy::Realm;
namespace
{
ActionQueue::Entry Job(std::string id, std::vector<std::uint32_t> guids = {1})
{
    return {std::move(id), "realm_move", std::move(guids), 0, nlohmann::json::object()};
}
}

LEEROY_TEST(RealmQueue_RejectsMalformedAndDuplicateEntriesWithoutDisplacingHead)
{
    ActionQueue queue;
    CHECK(queue.Push(Job("original")));
    CHECK(!queue.Push(Job("original", {2})));
    CHECK(!queue.Push(Job("")));
    CHECK(!queue.Push(Job("zero", {0})));
    CHECK(!queue.Push(Job("duplicates", {2, 2})));
    CHECK(!queue.Push(Job("empty", {})));
    auto invalid = Job("invalid");
    invalid.kind.clear(); CHECK(!queue.Push(invalid));
    invalid = Job("invalid"); invalid.cursor = 1; CHECK(!queue.Push(invalid));
    invalid = Job("invalid"); invalid.payload = nullptr; CHECK(!queue.Push(invalid));
    CHECK_EQ(queue.Size(), std::size_t{1});
    TickBudget budget(5);
    queue.Drain(budget, [](auto& entry) { CHECK_EQ(entry.id, std::string("original")); return true; });
    CHECK_EQ(queue.Size(), std::size_t{0});
}

LEEROY_TEST(RealmQueue_AcceptsProtocolMaximumRosterAndRejectsLarger)
{
    ActionQueue queue;
    std::vector<std::uint32_t> guids;
    for (std::uint32_t guid = 1; guid <= 150; ++guid) guids.push_back(guid);
    CHECK(queue.Push(Job("maximum", guids)));
    guids.push_back(151);
    CHECK(!queue.Push(Job("oversized", guids)));
    CHECK_EQ(queue.Size(), std::size_t{1});
}

LEEROY_TEST(RealmQueue_CapacityRefusesNewWorkWithoutDroppingAcceptedWork)
{
    ActionQueue queue;
    for (std::size_t i = 0; i < ActionQueue::Capacity; ++i)
        CHECK(queue.Push(Job(std::to_string(i))));
    CHECK(!queue.Push(Job("overflow")));
    std::size_t dropped = 0;
    queue.DropAll("off", [&](auto const& entry, auto const& reason)
    {
        CHECK_EQ(entry.id, std::to_string(dropped++));
        CHECK_EQ(reason, std::string("off"));
    });
    CHECK_EQ(dropped, ActionQueue::Capacity);
    CHECK(queue.Push(Job("after-clear")));
}

LEEROY_TEST(RealmQueue_LongJobSharesOneTickBudgetAndPreservesCursorAcrossTicks)
{
    ActionQueue queue;
    CHECK(queue.Push(Job("long", {1, 2, 3, 4, 5, 6, 7})));
    CHECK(queue.Push(Job("next", {8})));
    std::vector<std::uint32_t> acted;
    auto step = [&](auto& entry)
    {
        acted.push_back(entry.guids.at(entry.cursor));
        return ++entry.cursor == entry.guids.size();
    };
    TickBudget budget(5);
    queue.Drain(budget, step);
    CHECK((acted == std::vector<std::uint32_t>{1, 2, 3, 4, 5}));
    CHECK_EQ(queue.Size(), std::size_t{2});
    CHECK_EQ(budget.Left(), 0u);
    queue.Drain(budget, step);
    CHECK_EQ(acted.size(), std::size_t{5});
    budget.Reset();
    queue.Drain(budget, step);
    CHECK((acted == std::vector<std::uint32_t>{1, 2, 3, 4, 5, 6, 7, 8}));
    CHECK_EQ(queue.Size(), std::size_t{0});
    CHECK_EQ(budget.Left(), 2u);
}

LEEROY_TEST(RealmQueue_WaitingHeadIsRetainedAndNeverSpinsOrOvertaken)
{
    ActionQueue queue;
    CHECK(queue.Push(Job("waiting")));
    CHECK(queue.Push(Job("later")));
    TickBudget budget(5);
    unsigned calls = 0;
    queue.Drain(budget, [&](auto& entry)
    {
        ++calls;
        CHECK_EQ(entry.id, std::string("waiting"));
        return false;
    });
    CHECK_EQ(calls, 1u);
    CHECK_EQ(budget.Left(), 4u);
    CHECK_EQ(queue.Size(), std::size_t{2});
    budget.Reset();
    queue.Drain(budget, [&](auto&) { ++calls; return true; });
    CHECK_EQ(calls, 3u);
    CHECK_EQ(queue.Size(), std::size_t{0});
}

LEEROY_TEST(RealmQueue_MalformedProgressCannotSpinOrDisappear)
{
    for (bool pastEnd : {false, true})
    {
        ActionQueue queue;
        CHECK(queue.Push(Job("bad-progress", {1, 2})));
        TickBudget budget(5);
        unsigned calls = 0;
        queue.Drain(budget, [&](auto& entry)
        {
            ++calls;
            entry.cursor = pastEnd ? 100 : 0;
            return false;
        });
        CHECK_EQ(calls, 1u);
        CHECK_EQ(queue.Size(), std::size_t{1});
    }
}

LEEROY_TEST(RealmQueue_ExceptionConsumesBudgetButRetainsExactHead)
{
    ActionQueue queue;
    CHECK(queue.Push(Job("unknown")));
    TickBudget budget(5);
    bool caught = false;
    try
    {
        queue.Drain(budget, [](auto&) -> bool { throw std::runtime_error("unknown outcome"); });
    }
    catch (std::runtime_error const&) { caught = true; }
    CHECK(caught);
    CHECK_EQ(queue.Size(), std::size_t{1});
    CHECK_EQ(budget.Left(), 4u);
    queue.DropAll("shutdown", [](auto const& entry, auto const& reason)
    {
        CHECK_EQ(entry.id, std::string("unknown"));
        CHECK_EQ(reason, std::string("shutdown"));
    });
}

LEEROY_TEST(RealmQueue_DropReportsExactRemainingCursorOnceAndClearsQueue)
{
    ActionQueue queue;
    CHECK(queue.Push(Job("partial", {1, 2, 3})));
    CHECK(queue.Push(Job("unstarted", {4})));
    TickBudget budget(1);
    queue.Drain(budget, [](auto& entry) { ++entry.cursor; return false; });
    unsigned calls = 0;
    auto dropped = [&](auto const& entry, auto const& reason)
    {
        CHECK_EQ(reason, std::string("config_changed"));
        CHECK_EQ(entry.cursor, entry.id == "partial" ? std::size_t{1} : std::size_t{0});
        ++calls;
    };
    queue.DropAll("config_changed", dropped);
    queue.DropAll("config_changed", dropped);
    CHECK_EQ(calls, 2u);
    CHECK_EQ(queue.Size(), std::size_t{0});
}

LEEROY_TEST(RealmQueue_ARearmLeavesOutMembersFencedForTheirOwnReasonAtItsAck)
{
    // 2026-11-10/11: his group's members (busy:raid_owner) refused whole overnight rearms of the cast.
    std::map<std::uint32_t, std::string> const fences{{102, "busy:raid_owner"}, {104, "busy:body_unavailable"}};
    std::vector<std::uint32_t> asked;
    auto fence = [&](std::uint32_t guid)
    {
        asked.push_back(guid);
        auto found = fences.find(guid);
        return found == fences.end() ? std::string{} : found->second;
    };
    auto const rearm = PartitionAtAck("rearm", {101, 102, 103, 104, 105}, fence);
    CHECK(rearm.error.empty());
    CHECK((rearm.admitted == std::vector<std::uint32_t>{101, 103, 105}));
    CHECK((rearm.fenced == std::vector<std::uint32_t>{102, 104}));
    CHECK((asked == std::vector<std::uint32_t>{101, 102, 103, 104, 105}));
    // Every member fenced: refused whole with the first reason, as before.
    auto const none = PartitionAtAck("rearm", {102, 104}, fence);
    CHECK_EQ(none.error, std::string("busy:raid_owner"));
    CHECK(none.admitted.empty() && none.fenced.empty());
    // Every other kind is refused whole at its first fenced member, asking nobody after it.
    asked.clear();
    for (auto kind : {"leave_pvp", "realm_move", "duel", "guild_grow"})
    {
        asked.clear();
        auto const whole = PartitionAtAck(kind, {101, 102, 103}, fence);
        CHECK_EQ(whole.error, std::string("busy:raid_owner"));
        CHECK(whole.admitted.empty() && whole.fenced.empty());
        CHECK((asked == std::vector<std::uint32_t>{101, 102}));
    }
    CHECK(PartitionAtAck("leave_pvp", {101, 103}, fence).error.empty());
}

LEEROY_TEST(RealmQueue_AJobWideFenceStillRefusesAWholeRearmAtItsAck)
{
    // busy:training_cleanup (a training cleanup pending anywhere), the Realm's own holder of a member
    // and anything not on the member list refuse the rearm whole at once, as before #235.
    for (auto reason : {"busy:training_cleanup", "busy:realm_owner", "realm_off", "busy:operation_identity",
        "bad field: affected_bodies", "some_future_reason"})
    {
        std::vector<std::uint32_t> asked;
        auto const whole = PartitionAtAck("rearm", {101, 102, 103}, [&](std::uint32_t guid)
        {
            asked.push_back(guid);
            return guid == 102 ? std::string(reason) : std::string{};
        });
        CHECK_EQ(whole.error, std::string(reason));
        CHECK(whole.admitted.empty() && whole.fenced.empty());
        CHECK((asked == std::vector<std::uint32_t>{101, 102}));
    }
    // A member's own fence before the job's: the job's still refuses the whole rearm.
    auto const mixed = PartitionAtAck("rearm", {101, 102, 103}, [](std::uint32_t guid)
    {
        return guid == 101 ? std::string("busy:combat") : guid == 102 ? std::string("busy:training_cleanup") : std::string{};
    });
    CHECK_EQ(mixed.error, std::string("busy:training_cleanup"));
}
