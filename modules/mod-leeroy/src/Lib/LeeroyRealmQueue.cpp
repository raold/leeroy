// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmQueue.h"
#include <algorithm>
#include <set>
#include <utility>

namespace Leeroy::Realm
{
bool ActionQueue::Push(Entry entry)
{
    if (_entries.size() >= Capacity || entry.id.empty() || entry.kind.empty() || entry.cursor != 0
        || entry.guids.empty() || entry.guids.size() > 150 || !entry.payload.is_object()
        || std::find(entry.guids.begin(), entry.guids.end(), 0) != entry.guids.end()
        || std::set<std::uint32_t>(entry.guids.begin(), entry.guids.end()).size() != entry.guids.size()
        || std::any_of(_entries.begin(), _entries.end(), [&](auto const& item) { return item.id == entry.id; }))
        return false;
    _entries.push_back(std::move(entry));
    return true;
}

void ActionQueue::Drain(TickBudget& budget, std::function<bool(Entry&)> const& step)
{
    if (!step)
        return;
    while (!_entries.empty() && budget.Take())
    {
        auto& entry = _entries.front();
        auto const cursor = entry.cursor;
        if (step(entry))
            _entries.pop_front();
        else if (entry.cursor <= cursor || entry.cursor >= entry.guids.size())
            break; // Waiting or malformed progress must not spin within a tick.
    }
}

void ActionQueue::DropAll(std::string const& reason,
    std::function<void(Entry const&, std::string const&)> const& dropped)
{
    auto removed = std::move(_entries);
    _entries.clear();
    if (dropped)
        for (auto const& entry : removed)
            dropped(entry, reason);
}
void ActionQueue::DropIf(std::function<bool(Entry const&)> const& predicate, std::string const& reason,
    std::function<void(Entry const&, std::string const&)> const& dropped)
{
    if (!predicate) return;
    std::deque<Entry> kept, removed;
    while (!_entries.empty())
    {
        auto entry = std::move(_entries.front());
        _entries.pop_front();
        (predicate(entry) ? removed : kept).push_back(std::move(entry));
    }
    _entries = std::move(kept);
    if (dropped) for (auto const& entry : removed) dropped(entry, reason);
}
}
