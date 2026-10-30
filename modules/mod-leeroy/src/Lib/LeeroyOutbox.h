// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_LEEROY_LEEROY_OUTBOX_H
#define MOD_LEEROY_LEEROY_OUTBOX_H

#include "LeeroyProtocol.h"

#include <cstddef>
#include <cstdint>
#include <deque>
#include <mutex>
#include <string>
#include <utility>
#include <vector>

namespace Leeroy
{
class Outbox
{
public:
    explicit Outbox(std::size_t eventCapacity = kEventQueueCapacity);

    // Returns how many events were dropped to make room (0 or 1).
    std::size_t PushEvent(std::string line, bool droppable);
    // Acks, pongs, errors, greeting: never dropped.
    void PushControl(std::string line);
    // Appends control lines first, then events (only when eventsEnabled), up to maxLines in total.
    std::size_t PopBatch(std::vector<std::string>& out, std::size_t maxLines, bool eventsEnabled);
    void ClearControl();
    std::size_t EventCount() const;
    std::uint64_t TotalDropped() const;

private:
    mutable std::mutex _mutex;
    std::size_t _eventCapacity;
    std::deque<std::pair<std::string, bool>> _events;
    std::deque<std::string> _control;
    std::uint64_t _totalDropped = 0;
};
}

#endif
