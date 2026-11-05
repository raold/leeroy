// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmQueue.h"
#include "TestHarness.h"
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
