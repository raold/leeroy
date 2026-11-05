// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyBotJournal.h"
#include "TestHarness.h"
#include <map>
#include <set>
#include <stdexcept>

using namespace Leeroy::Training;

namespace
{
struct FakeBot
{
    std::map<Engine, std::set<std::string>> strategies;
    bool Present(Engine engine, std::string_view name) const
    {
        auto found = strategies.find(engine);
        return found != strategies.end() && found->second.count(std::string(name)) != 0;
    }
    void Change(Engine engine, std::string const& param)
    {
        auto name = param.substr(1);
        if (param.front() == '-') strategies[engine].erase(name);
        else
        {
            for (auto sibling : SiblingGroup(name)) strategies[engine].erase(std::string(sibling));
            strategies[engine].insert(name);
        }
    }
    void Apply(std::vector<std::pair<Engine, std::string>> const& changes)
    {
        for (auto const& [engine, param] : changes) Change(engine, param);
    }
};
}

LEEROY_TEST(Training_StrategyJournalRestoresEvictedSiblingsAndOriginalValues)
{
    FakeBot bot{{{Engine::Combat, {"dps assist", "save mana", "avoid aoe"}}}};
    auto present = [&](Engine engine, std::string_view name) { return bot.Present(engine, name); };
    BotJournal journal;
    journal.NoteStrategy(Engine::Combat, "dps aoe", present);
    bot.Change(Engine::Combat, "+dps aoe");
    journal.NoteStrategy(Engine::Combat, "threat", present);
    bot.Change(Engine::Combat, "+threat");
    journal.NoteStrategy(Engine::Combat, "save mana", present);
    bot.Change(Engine::Combat, "-save mana");
    journal.NoteStrategy(Engine::Combat, "tank assist", present);
    bot.Change(Engine::Combat, "+tank assist");
    auto changes = journal.RestoreStrategy("dps aoe", present);
    CHECK_EQ(changes.size(), 2);
    CHECK_EQ(changes[0].second, "-tank assist");
    CHECK_EQ(changes[1].second, "+dps assist");
    bot.Apply(changes);
    bot.Apply(journal.RestoreStrategy("threat", present));
    bot.Apply(journal.RestoreStrategy("save mana", present));
    CHECK(bot.strategies[Engine::Combat] == std::set<std::string>({"dps assist", "save mana", "avoid aoe"}));
    CHECK(journal.Empty());
    CHECK(journal.RestoreStrategy("dps aoe", present).empty());
}

LEEROY_TEST(Training_JournalPreservesEachEngineAndUnknownUnownedStrategy)
{
    FakeBot bot{{{Engine::Combat, {"dps assist"}}, {Engine::NonCombat, {"tank assist", "follow"}}}};
    auto present = [&](Engine engine, std::string_view name) { return bot.Present(engine, name); };
    BotJournal journal;
    for (auto engine : {Engine::Combat, Engine::NonCombat})
    {
        journal.NoteStrategy(engine, "dps aoe", present);
        bot.Change(engine, "+dps aoe");
    }
    CHECK(journal.Strategies() == std::vector<std::string>({"dps aoe"}));
    bot.Apply(journal.RestoreStrategy("dps aoe", present));
    CHECK(bot.strategies[Engine::Combat] == std::set<std::string>({"dps assist"}));
    CHECK(bot.strategies[Engine::NonCombat] == std::set<std::string>({"tank assist", "follow"}));
    CHECK(journal.RestoreStrategy("follow", present).empty());
    CHECK(journal.Empty());
}

LEEROY_TEST(Training_ValueJournalAndStrategyReset)
{
    BotJournal journal;
    journal.NoteValue("mana save level", "1");
    journal.NoteValue("mana save level", "2");
    CHECK(journal.RestoreValue("mana save level") == std::optional<std::string>("1"));
    CHECK(journal.RestoreValue("mana save level") == std::optional<std::string>("1"));
    CHECK(StockValue("wait for attack time") == std::optional<std::string>("10"));
    CHECK(StockValue("focus heal targets") == std::optional<std::string>(""));
    CHECK(StockValue("formation") == std::optional<std::string>("chaos"));
    CHECK(!StockValue("boost"));
    journal.NoteValue("formation", "arrow");
    journal.NoteValue("wait for attack time", "4");
    journal.NoteStrategy(Engine::Combat, "threat", [](auto, auto) { return false; });
    journal.Reset({"formation"});
    CHECK(journal.Strategies().empty());
    CHECK(journal.Values() == std::vector<std::string>({"wait for attack time"}));
    CHECK(journal.RestoreValue("wait for attack time") == std::optional<std::string>("4"));
    CHECK(journal.Empty());
}

LEEROY_TEST(Training_JournalCallbackFailuresRetainAllEntries)
{
    BotJournal journal;
    bool threw = false;
    try
    {
        journal.NoteStrategy(Engine::Combat, "dps aoe", [](auto, std::string_view name)
        {
            if (name == "dps aoe") throw std::runtime_error("synthetic read failure");
            return false;
        });
    }
    catch (std::runtime_error const&) { threw = true; }
    CHECK(threw);
    CHECK(journal.Empty());
    for (auto engine : {Engine::Combat, Engine::NonCombat})
        journal.NoteStrategy(engine, "threat", [](auto, auto) { return false; });
    threw = false;
    try
    {
        journal.RestoreStrategy("threat", [](Engine engine, auto)
        {
            if (engine == Engine::NonCombat) throw std::runtime_error("synthetic read failure");
            return true;
        });
    }
    catch (std::runtime_error const&) { threw = true; }
    CHECK(threw);
    CHECK(!journal.Empty());
    auto changes = journal.RestoreStrategy("threat", [](auto, auto) { return true; });
    CHECK_EQ(changes.size(), 2);
    CHECK(journal.Empty());
}

LEEROY_TEST(Training_JournalCopyAllowsRuntimeFailureRetry)
{
    BotJournal journal;
    journal.NoteValue("formation", "arrow");
    journal.NoteStrategy(Engine::Combat, "threat", [](auto, auto) { return false; });
    auto attempted = journal;
    attempted.RestoreStrategy("threat", [](auto, auto) { return true; });
    attempted.RestoreValue("formation");
    CHECK(attempted.Empty());
    CHECK(!journal.Empty()); // A failed AI apply keeps this original for a later verified retry.
    CHECK(journal.Values() == std::vector<std::string>({"formation"}));
    CHECK(journal.Strategies() == std::vector<std::string>({"threat"}));
}
