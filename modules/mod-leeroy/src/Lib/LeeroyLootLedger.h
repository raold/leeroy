// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_LOOT_LEDGER_H
#define MOD_LEEROY_LOOT_LEDGER_H

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <map>
#include <mutex>
#include <optional>
#include <tuple>

namespace Leeroy::Guild
{
using LootSourceKey = std::tuple<std::uint32_t, std::uint32_t, std::uint32_t>;

struct LootSourceObservation
{
    std::uint64_t source = 0;
    std::uint64_t group = 0;
    std::uint64_t generation = 0;

    bool operator==(LootSourceObservation const&) const = default;
};

// No game objects are retained. An entry identifies one observed loot generation, not merely one template entry.
// There is no generation token in give_loot: after expiry/eviction/restart an old command cannot be distinguished
// from a new one. Within this bounded observation window, collisions/reuse fail closed instead of replacing loot.
class LootSourceLedgerState
{
public:
    using Clock = std::chrono::steady_clock;
    using Time = Clock::time_point;
    static constexpr auto Lifetime = std::chrono::hours(3);
    static constexpr std::size_t Capacity = 1024;

    void Note(LootSourceKey const& key, LootSourceObservation source, Time now = Clock::now())
    {
        if (!source.source)
            return;
        std::lock_guard<std::mutex> lock(_mutex);
        ExpireLocked(now);
        auto found = _entries.find(key);
        if (found != _entries.end())
        {
            if (!found->second.ambiguous && found->second.source != source)
            {
                found->second.ambiguous = true;
                // Preserve the ambiguity for a full window after its first detection. Reopens cannot refresh it.
                found->second.expires = now + Lifetime;
            }
            return;
        }
        if (_entries.size() == Capacity)
        {
            auto oldest = std::min_element(_entries.begin(), _entries.end(), [](auto const& a, auto const& b)
            {
                return a.second.order < b.second.order;
            });
            _entries.erase(oldest);
        }
        _entries.emplace(key, Entry{source, now + Lifetime, ++_order, false});
    }

    std::optional<LootSourceObservation> Find(LootSourceKey const& key, Time now = Clock::now()) const
    {
        std::lock_guard<std::mutex> lock(_mutex);
        auto const found = _entries.find(key);
        if (found == _entries.end() || found->second.ambiguous || now >= found->second.expires)
            return std::nullopt;
        return found->second.source;
    }

    // A source can change between the map-thread observation and the next world tick. Do not leave that old
    // observation usable even if the object later returns to the same apparent state.
    void Invalidate(LootSourceKey const& key, Time now = Clock::now())
    {
        std::lock_guard<std::mutex> lock(_mutex);
        auto const found = _entries.find(key);
        if (found != _entries.end() && !found->second.ambiguous && now < found->second.expires)
        {
            found->second.ambiguous = true;
            found->second.expires = now + Lifetime;
        }
    }

    void Expire(Time now = Clock::now())
    {
        std::lock_guard<std::mutex> lock(_mutex);
        ExpireLocked(now);
    }

private:
    struct Entry
    {
        LootSourceObservation source;
        Time expires;
        std::uint64_t order;
        bool ambiguous;
    };

    void ExpireLocked(Time now)
    {
        for (auto it = _entries.begin(); it != _entries.end();)
            if (now >= it->second.expires)
                it = _entries.erase(it);
            else
                ++it;
    }

    mutable std::mutex _mutex;
    std::map<LootSourceKey, Entry> _entries;
    std::uint64_t _order = 0;
};
}

#endif
