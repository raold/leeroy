// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_QUEUE_H
#define MOD_LEEROY_REALM_QUEUE_H

#include "LeeroyRealmPolicy.h"
#include <deque>
#include <functional>
#include <string>
#include <vector>
#include <nlohmann/json.hpp>

namespace Leeroy::Realm
{
class ActionQueue
{
public:
    struct Entry
    {
        std::string id, kind;
        std::vector<std::uint32_t> guids;
        std::size_t cursor = 0;
        nlohmann::json payload;
    };
    static constexpr std::size_t Capacity = 500;
    bool Push(Entry entry);
    // One budget unit per callback. A completed entry is removed; otherwise
    // an advanced cursor continues under the budget, and a waiting head yields
    // until next tick. Callbacks must never recursively mutate this queue.
    void Drain(TickBudget& budget, std::function<bool(Entry&)> const& step);
    void DropAll(std::string const& reason, std::function<void(Entry const&, std::string const&)> const& dropped);
    std::size_t Size() const { return _entries.size(); }
private:
    std::deque<Entry> _entries;
};
}
#endif
