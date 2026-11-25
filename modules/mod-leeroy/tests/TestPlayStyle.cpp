// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyPlayStyle.h"
#include "LeeroyGuildCommands.h"
#include "TestHarness.h"
#include <algorithm>
#include <map>
#include <string>
#include <vector>
using namespace Leeroy::PlayStyle;

LEEROY_TEST(PlayStyle_StrictWireBounds)
{
    for (auto text : {"0:0", "0:1", "2:0", "3:1", "6:1"})
        CHECK(Parse(text) && Leeroy::Guild::BotDoParamAllowed("playstyle", text));
    for (auto text : {"1:0", "7:0", "-2:1", "02:1", "6:2", "6:1 ", "potions", "6:1,4:0"})
        CHECK(!Parse(text) && !Leeroy::Guild::BotDoParamAllowed("playstyle", text));
    CHECK(Leeroy::Guild::BotDoParamAllowed("restore", "playstyle"));
}
LEEROY_TEST(PlayStyle_OnlyAddsUseAndDamageHoldWithinBounds)
{
    for (unsigned seconds = 0; seconds < 256; ++seconds)
        for (bool active : {false, true})
            for (bool potions : {false, true})
                for (unsigned delay = 2; delay <= 6; ++delay)
                {
                    Values before{active, static_cast<std::uint8_t>(seconds), potions};
                    auto after = Plan(before, {static_cast<std::uint8_t>(delay), true}, true);
                    CHECK(after.potions);
                    if (active && seconds > 6) CHECK(after.wait == active && after.seconds == seconds);
                    else CHECK(after.wait && after.seconds >= 2 && after.seconds <= 6
                        && (!active || after.seconds >= seconds));
                    auto healer = Plan(before, {static_cast<std::uint8_t>(delay), false}, false);
                    CHECK(healer == before);
                }
}
LEEROY_TEST(PlayStyle_IndependentAdmissionFences)
{
    Facts good{true,true,true,true,false,false,false,false,false};
    CHECK(Refusal(good).empty());
    for (auto field : {&Facts::enabled,&Facts::owned,&Facts::alive,&Facts::quiet})
    { auto bad = good; bad.*field = false; CHECK(!Refusal(bad).empty()); }
    for (auto field : {&Facts::instance,&Facts::raid,&Facts::foreign,&Facts::pvp,&Facts::busy})
    { auto bad = good; bad.*field = true; CHECK(!Refusal(bad).empty()); }
}
LEEROY_TEST(PlayStyle_JournalRestoresExactBaselineAndKeepsCallerChanges)
{
    Values baseline{false,10,false};
    Values style = Plan(baseline,{4,true},true);
    Journal journal;
    journal.Note(baseline,style);
    CHECK(journal.Restore(style,"*") == baseline);
    CHECK(journal.Owns()); // Planning is not verification; a failed native write keeps the journal.
    Values caller{true,6,false};
    auto restore = journal.Restore(caller,"*");
    CHECK(restore.seconds == 6 && restore.wait && !restore.potions);
    journal.Restored("wait for attack");
    CHECK(!journal.Owns("wait for attack") && journal.Owns("wait for attack time"));
    journal.Restored("*");
    CHECK(!journal.Owns());
}
LEEROY_TEST(PlayStyle_ExplicitEqualReplacementMustUnwindBeforeNewOwnerWrites)
{
    Values baseline{false,10,false};
    auto style = Plan(baseline,{4,true},true);
    Journal journal;
    journal.Note(baseline,style);
    // Adapter's bot_do/Training boundary: unwind, verify, forget, then caller's same desired state.
    auto live = journal.Restore(style,"*");
    CHECK(live == baseline);
    journal.Restored("*");
    live = style;
    CHECK(journal.Restore(live,"*") == style);
}
LEEROY_TEST(PlayStyle_CleanupSeesPvpDuringCombatAndExactBodyLife)
{
    Facts f{true,true,true,false,false,false,false,false,false};
    CHECK(!CleanupDue(f,false,false));
    f.pvp = true;
    CHECK(CleanupDue(f,false,false));
    f.pvp = false;
    CHECK(CleanupDue(f,true,false));
    CHECK(CleanupDue(f,false,true));
    CHECK(SameLife({1,2,3},{1,2,3}));
    CHECK(!SameLife({1,2,3},{4,2,3}));
    CHECK(!SameLife({1,2,3},{1,4,3}));
    CHECK(!SameLife({1,2,3},{1,2,4}));
    CHECK(!SameLife({0,2,3},{0,2,3}));
}
LEEROY_TEST(PlayStyle_RoleChangeRemovesAddedHoldEvenWhenTimerWasAlreadyEqual)
{
    Facts f{true,true,true,true,false,false,false,false,false};
    for (std::uint8_t delay = 2; delay <= 6; ++delay)
    {
        Values baseline{false,delay,false};
        auto applied = Plan(baseline,{delay,false},true);
        Journal journal;
        journal.Note(baseline,applied);
        CHECK(applied.wait && journal.Owns("wait for attack"));
        CHECK(!journal.Owns("wait for attack time"));
        CHECK(CleanupDue(f,false,journal.OwnsOpeningHold())); // Native tank/healer transition.
        CHECK(journal.Restore(applied,"*") == baseline);
        journal.Restored("*");
        CHECK(!journal.OwnsOpeningHold());

        // A pre-existing equal hold belongs to its caller; adding potions does not adopt it.
        baseline.wait = true;
        applied = Plan(baseline,{delay,true},true);
        journal.Note(baseline,applied);
        CHECK(journal.Owns("potions") && !journal.OwnsOpeningHold());
        CHECK(!CleanupDue(f,false,journal.OwnsOpeningHold()));
    }
}

namespace
{
using nlohmann::json;
json TableMessage(json styles) { return json{{"v", 1}, {"type", "playstyle_table"}, {"id", "t"}, {"ts", 0}, {"styles", std::move(styles)}}; }
bool TableParses(json styles)
{
    std::string error;
    auto parsed = Leeroy::Guild::ParsePlayStyleTable(TableMessage(std::move(styles)), error);
    CHECK(parsed.has_value() == error.empty());
    CHECK(parsed || error == "bad field: styles");
    return parsed.has_value();
}
}

LEEROY_TEST(PlayStyle_TableWireIsStrictAndBounded)
{
    CHECK(TableParses(json::array()));
    CHECK(TableParses(json::array({{{"guid", 1101u}, {"param", "4:1"}}, {{"guid", 1102u}, {"param", "0:0"}}})));
    json full = json::array();
    for (std::uint32_t guid = 1; guid <= kTableMax; ++guid) full.push_back({{"guid", guid}, {"param", "2:1"}});
    CHECK(TableParses(full));
    full.push_back({{"guid", 9999u}, {"param", "2:1"}});
    CHECK(!TableParses(full));
    for (auto bad : {json::array({{{"guid", 1101u}, {"param", "7:1"}}}), json::array({{{"guid", 0u}, {"param", "2:1"}}}),
             json::array({{{"guid", 1101u}, {"param", "2:1"}}, {{"guid", 1101u}, {"param", "3:0"}}}),
             json::array({{{"guid", 1101u}}}), json::array({{{"param", "2:1"}}}),
             json::array({{{"guid", -1}, {"param", "2:1"}}}), json::array({{{"guid", 4294967296ull}, {"param", "2:1"}}}),
             json::array({{{"guid", "1101"}, {"param", "2:1"}}}), json::array({{{"guid", 1101u}, {"param", 21}}}),
             json::array({json::array({1101, "2:1"})}), json::array({1101})})
        CHECK(!TableParses(bad));
    std::string error;
    CHECK(!Leeroy::Guild::ParsePlayStyleTable(json{{"v", 1}, {"type", "playstyle_table"}, {"id", "t"}}, error));
    CHECK(error == "bad field: styles");
    CHECK(!Leeroy::Guild::ParsePlayStyleTable(TableMessage({{"guid", 1u}}), error));
    std::string parsedError = "stale";
    auto parsed = Leeroy::Guild::ParsePlayStyleTable(
        TableMessage(json::array({{{"guid", 1102u}, {"param", "6:0"}}, {{"guid", 1101u}, {"param", "0:1"}}})), parsedError);
    CHECK(parsed && parsedError.empty() && parsed->styles.size() == 2);
    if (parsed)
    {
        CHECK(parsed->styles[0].guid == 1102 && parsed->styles[0].request == (Request{6, false}));
        CHECK(parsed->styles[1].guid == 1101 && parsed->styles[1].request == (Request{0, true}));
    }
}

LEEROY_TEST(PlayStyle_TableReplacementKeepsUnchangedEntriesAndDropsTheRest)
{
    Table held = ReplaceTable({}, {{1, {4, true}}, {2, {0, true}}, {3, {6, false}}}, 1000);
    CHECK(held.size() == 3 && held.at(1).dueMs == 1000 && held.at(3).request == (Request{6, false}));
    TableTried(held.at(1), 1000);
    TableTried(held.at(2), 1000);
    Table next = ReplaceTable(held, {{1, {4, true}}, {2, {2, true}}, {4, {3, true}}}, 5000);
    CHECK(next.size() == 3 && !next.contains(3));
    CHECK(next.at(1).dueMs == 1000 + kTableRetryMs); // unchanged: keeps its wait
    CHECK(next.at(2).dueMs == 5000 && next.at(2).request == (Request{2, true})); // changed: due now
    CHECK(next.at(4).dueMs == 5000);
    CHECK(TableUndo(Holder::Table, next.contains(3)) && !TableUndo(Holder::Table, next.contains(1)));
    CHECK(!TableUndo(Holder::Other, false) && !TableUndo(Holder::None, false));
    CHECK(ReplaceTable(next, {}, 9000).empty()); // an empty table lists nobody: every table journal is undone
}

LEEROY_TEST(PlayStyle_TableGateNeedsBothSwitchesATableAndHimOutsideARaid)
{
    TableGate open{true, true, true, true, false};
    CHECK(TableRefusal(open).empty());
    auto with = [&](auto field, bool value) { auto g = open; g.*field = value; return TableRefusal(g); };
    CHECK(with(&TableGate::enabled, false) == "playstyle_off");
    CHECK(with(&TableGate::table, false) == "playstyle_table_off");
    CHECK(with(&TableGate::held, false) == "no_table");
    CHECK(with(&TableGate::human, false) == "human_offline");
    CHECK(with(&TableGate::humanRaid, true) == "human_raid");
}

LEEROY_TEST(PlayStyle_TableLeavesAnotherWritersJournalAndRedoesItsOwnOnlyOnChange)
{
    Slot slot{{4, true}, 100};
    CHECK(!TableDue(slot, {}, 99) && TableDue(slot, {}, 100));
    CHECK(!TableDue(slot, {Holder::Other, {4, true}}, 1000000));    // an explicit bot_do's style stays its own
    CHECK(!TableDue(slot, {Holder::Table, {4, true}}, 1000000));    // carried out and still in place
    CHECK(TableDue(slot, {Holder::Table, {3, true}}, 100));         // the mind changed it: undo, then carry out anew
    CHECK(!TableDue(slot, {Holder::Table, {3, true}}, 99));
}

LEEROY_TEST(PlayStyle_TableTriesStayInBudgetAndReachEveryMember)
{
    std::vector<TableEntry> entries;
    for (std::uint32_t guid = 1; guid <= 150; ++guid) entries.push_back({guid, {3, true}});
    Table table = ReplaceTable({}, entries, 0);
    std::map<std::uint32_t, int> seen;
    auto none = [](std::uint32_t) { return Held{}; };
    std::int64_t now = 0;
    for (int pass = 0; pass < 10; ++pass, now += 1000)
    {
        auto tries = TableTries(table, none, now);
        CHECK(tries.size() == kTableTriesPerPass || (pass == 9 && tries.size() == 150 - 9 * kTableTriesPerPass));
        for (auto guid : tries) { ++seen[guid]; TableTried(table.at(guid), now); }
    }
    CHECK(seen.size() == 150);
    CHECK(std::all_of(seen.begin(), seen.end(), [](auto const& kv) { return kv.second == 1; }));
    CHECK(TableTries(table, none, now).empty());                   // everyone waits out its retry
    CHECK(TableTries(table, none, kTableRetryMs).size() == kTableTriesPerPass); // the first ones are due again
}

// More entries stay due than a retry period's budget (members offline or in a battleground for a long time): the
// oldest due go first, so the higher guids are reached as often as the lower ones.
LEEROY_TEST(PlayStyle_TableTriesRotateWhenMoreStayDueThanTheBudget)
{
    std::vector<TableEntry> entries;
    for (std::uint32_t guid = 1; guid <= kTableMax; ++guid) entries.push_back({guid, {4, false}});
    Table table = ReplaceTable({}, entries, 0);
    std::map<std::uint32_t, int> tried;
    auto none = [](std::uint32_t) { return Held{}; };
    for (std::int64_t now = 0; now < 64000; now += 1000)
        for (auto guid : TableTries(table, none, now))
        {
            ++tried[guid];
            TableTried(table.at(guid), now);
        }
    CHECK(tried.size() == kTableMax);
    CHECK(std::all_of(tried.begin(), tried.end(), [](auto const& kv) { return kv.second == 2; }));
    CHECK(tried.at(kTableMax) == tried.at(1));
}

// The scenario the table exists for, against the old request-at-his-login model. 150 cast members; at his login half
// are busy (a battleground, dead, a Realm hold) and come free over the next ten minutes. The old way tried each once at
// his login; the table tries again until each one is free.
LEEROY_TEST(PlayStyle_TableStylesMembersBusyAtHisLoginOnceTheyAreFree)
{
    constexpr std::uint32_t kMembers = 150;
    auto freeAt = [](std::uint32_t guid) -> std::int64_t { return guid % 2 ? 0 : (guid % 10 + 1) * 60000; };
    std::map<std::uint32_t, Held> held;
    std::vector<TableEntry> entries;
    for (std::uint32_t guid = 1; guid <= kMembers; ++guid) entries.push_back({guid, {static_cast<std::uint8_t>(guid % 5 + 2), guid % 3 == 0}});
    Table table = ReplaceTable({}, entries, 0);
    std::size_t legacy = 0;
    for (std::uint32_t guid = 1; guid <= kMembers; ++guid) legacy += freeAt(guid) <= 0;
    std::size_t tries = 0;
    for (std::int64_t now = 0; now <= 15 * 60000; now += 1000)
        for (auto guid : TableTries(table, [&](std::uint32_t g) { return held[g]; }, now))
        {
            ++tries;
            TableTried(table.at(guid), now);
            if (now >= freeAt(guid)) held[guid] = {Holder::Table, table.at(guid).request};
        }
    std::size_t styled = 0;
    for (auto const& [guid, h] : held) styled += h.holder == Holder::Table;
    CHECK(legacy == kMembers / 2);
    CHECK(styled == kMembers);
    CHECK(tries < kMembers * 50);      // bounded: one try a member every kTableRetryMs at most
}
