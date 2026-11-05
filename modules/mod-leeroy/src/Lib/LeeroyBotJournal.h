// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_BOT_JOURNAL_H
#define MOD_LEEROY_BOT_JOURNAL_H

#include <cstdint>
#include <functional>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace Leeroy::Training
{
enum class Engine : std::uint8_t { Combat, NonCombat };
std::vector<std::string_view> SiblingGroup(std::string_view strategy);
std::optional<std::string> StockValue(std::string_view value);

// World-thread journal. Snapshot before mutation. The pure restore methods consume their entries only
// after calculating all changes successfully. A runtime caller must restore from a copy and keep the
// original journal until it verifies every requested engine/value change actually succeeded.
class BotJournal
{
public:
    using Present = std::function<bool(Engine, std::string_view)>;
    void NoteStrategy(Engine, std::string_view strategy, Present const&);
    void NoteValue(std::string_view value, std::string current);
    std::vector<std::pair<Engine, std::string>> RestoreStrategy(std::string_view strategy, Present const&);
    std::optional<std::string> RestoreValue(std::string_view value);
    std::vector<std::string> Strategies() const;
    std::vector<std::string> Values() const;
    void Reset(std::vector<std::string_view> const& values);
    bool Empty() const { return _strategies.empty() && _values.empty(); }

private:
    using Key = std::pair<Engine, std::string>;
    struct Entry
    {
        std::string requested;
        std::vector<std::pair<std::string, bool>> members;
    };
    std::map<Key, Entry> _strategies;
    std::map<std::string, std::string, std::less<>> _values;
};
}
#endif
