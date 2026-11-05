// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_TRAINING_SLOT_H
#define MOD_LEEROY_TRAINING_SLOT_H

#include <atomic>
#include <cstdint>
#include <limits>
#include <optional>

namespace Leeroy::Training
{
struct SlotToken
{
    std::uint64_t key = 0, generation = 0;
    bool operator==(SlotToken const&) const = default;
};

// Claim owns a pristine table exclusively; initialize it before Publish. Retire hides a published slot;
// return a reset/spare table before Ready. Generation-stamped CAS rejects late completion of an older pull.
// The tap still supplies the map/world barrier for accessing the table itself.
class SlotPublication
{
public:
    std::optional<SlotToken> Claim(std::uint64_t key)
    {
        auto state = _state.load(std::memory_order_acquire);
        if ((state & 7) != Free || (state >> 3) == (std::numeric_limits<std::uint64_t>::max() >> 3))
            return std::nullopt;
        auto desired = state + 8 + Claimed;
        if (!_state.compare_exchange_strong(state, desired, std::memory_order_acq_rel))
            return std::nullopt;
        _key.store(key, std::memory_order_relaxed);
        return SlotToken{key, desired >> 3};
    }

    bool Publish(SlotToken token) { return Transition(token, Claimed, Published); }
    bool CancelClaim(SlotToken token) { return Transition(token, Claimed, Retired); }
    bool Retire(SlotToken token) { return Transition(token, Published, Retired); }

    bool Ready(SlotToken token)
    {
        if (!Transition(token, Retired, Cleaning))
            return false;
        _key.store(0, std::memory_order_relaxed);
        _state.store(token.generation << 3, std::memory_order_release);
        return true;
    }

    std::optional<SlotToken> Snapshot() const
    {
        auto before = _state.load(std::memory_order_acquire);
        if ((before & 7) != Published)
            return std::nullopt;
        auto key = _key.load(std::memory_order_relaxed);
        if (_state.load(std::memory_order_acquire) != before)
            return std::nullopt;
        return SlotToken{key, before >> 3};
    }

private:
    enum : std::uint64_t { Free = 0, Claimed = 1, Published = 2, Retired = 3, Cleaning = 4 };
    bool Transition(SlotToken token, std::uint64_t from, std::uint64_t to)
    {
        if (!token.generation || token.generation > (std::numeric_limits<std::uint64_t>::max() >> 3)
            || _key.load(std::memory_order_relaxed) != token.key)
            return false;
        auto expected = (token.generation << 3) | from;
        return _state.compare_exchange_strong(expected, (token.generation << 3) | to,
            std::memory_order_acq_rel);
    }
    std::atomic<std::uint64_t> _state{0}, _key{0};
};
}
#endif
