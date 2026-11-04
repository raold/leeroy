// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyBotJournal.h"
#include <set>

namespace Leeroy::Training
{
std::vector<std::string_view> SiblingGroup(std::string_view strategy)
{
    if (strategy == "dps assist" || strategy == "dps aoe" || strategy == "tank assist")
        return {"dps assist", "dps aoe", "tank assist"};
    return {strategy};
}

std::optional<std::string> StockValue(std::string_view value)
{
    if (value == "mana save level") return "1";
    if (value == "wait for attack time") return "10";
    if (value == "focus heal targets") return "";
    if (value == "formation") return "chaos";
    return std::nullopt;
}

void BotJournal::NoteStrategy(Engine engine, std::string_view strategy, Present const& present)
{
    auto group = SiblingGroup(strategy);
    Key key{engine, std::string(group.front())};
    if (_strategies.contains(key)) return;
    Entry entry{std::string(strategy), {}};
    for (auto member : group)
        entry.members.emplace_back(member, present(engine, member));
    _strategies.emplace(std::move(key), std::move(entry));
}

void BotJournal::NoteValue(std::string_view value, std::string current)
{
    _values.try_emplace(std::string(value), std::move(current));
}

std::vector<std::pair<Engine, std::string>> BotJournal::RestoreStrategy(std::string_view strategy,
    Present const& present)
{
    auto group = SiblingGroup(strategy);
    std::vector<std::pair<Engine, std::string>> changes;
    std::vector<Key> consumed;
    for (auto engine : {Engine::Combat, Engine::NonCombat})
    {
        Key key{engine, std::string(group.front())};
        auto found = _strategies.find(key);
        if (found == _strategies.end()) continue;
        std::vector<std::string> remove, add;
        for (auto const& [member, wasPresent] : found->second.members)
        {
            bool nowPresent = present(engine, member);
            if (nowPresent && !wasPresent) remove.push_back("-" + member);
            if (!nowPresent && wasPresent) add.push_back("+" + member);
        }
        for (auto& param : remove) changes.emplace_back(engine, std::move(param));
        for (auto& param : add) changes.emplace_back(engine, std::move(param));
        consumed.push_back(std::move(key));
    }
    // Exceptions from a later engine callback must leave all snapshots available for retry.
    for (auto const& key : consumed) _strategies.erase(key);
    return changes;
}

std::optional<std::string> BotJournal::RestoreValue(std::string_view value)
{
    auto found = _values.find(value);
    if (found == _values.end()) return StockValue(value);
    std::string original = found->second;
    _values.erase(found);
    return original;
}

std::vector<std::string> BotJournal::Strategies() const
{
    std::set<std::string> names;
    for (auto const& [key, entry] : _strategies) names.insert(entry.requested);
    return {names.begin(), names.end()};
}

std::vector<std::string> BotJournal::Values() const
{
    std::vector<std::string> names;
    for (auto const& [name, value] : _values) names.push_back(name);
    return names;
}

void BotJournal::Reset(std::vector<std::string_view> const& values)
{
    _strategies.clear();
    for (auto name : values)
        if (auto found = _values.find(name); found != _values.end()) _values.erase(found);
}
}
