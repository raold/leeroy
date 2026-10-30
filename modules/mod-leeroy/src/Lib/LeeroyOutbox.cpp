// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyOutbox.h"

#include <algorithm>

namespace Leeroy
{
Outbox::Outbox(std::size_t eventCapacity) : _eventCapacity(eventCapacity)
{
}

std::size_t Outbox::PushEvent(std::string line, bool droppable)
{
    std::lock_guard<std::mutex> lock(_mutex);
    std::size_t dropped = 0;
    if (_eventCapacity > 0 && _events.size() >= _eventCapacity)
    {
        auto it = std::find_if(_events.begin(), _events.end(), [](auto const& e) { return e.second; });
        if (it != _events.end())
            _events.erase(it);
        else
            _events.pop_front();
        ++dropped;
        ++_totalDropped;
    }
    _events.emplace_back(std::move(line), droppable);
    return dropped;
}

void Outbox::PushControl(std::string line)
{
    std::lock_guard<std::mutex> lock(_mutex);
    _control.push_back(std::move(line));
}

std::size_t Outbox::PopBatch(std::vector<std::string>& out, std::size_t maxLines, bool eventsEnabled)
{
    std::lock_guard<std::mutex> lock(_mutex);
    std::size_t popped = 0;
    while (popped < maxLines && !_control.empty())
    {
        out.push_back(std::move(_control.front()));
        _control.pop_front();
        ++popped;
    }
    while (eventsEnabled && popped < maxLines && !_events.empty())
    {
        out.push_back(std::move(_events.front().first));
        _events.pop_front();
        ++popped;
    }
    return popped;
}

void Outbox::ClearControl()
{
    std::lock_guard<std::mutex> lock(_mutex);
    _control.clear();
}

std::size_t Outbox::EventCount() const
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _events.size();
}

std::uint64_t Outbox::TotalDropped() const
{
    std::lock_guard<std::mutex> lock(_mutex);
    return _totalDropped;
}
}
