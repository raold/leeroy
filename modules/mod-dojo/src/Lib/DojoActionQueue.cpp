// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#include "DojoActionQueue.h"

#include <algorithm>
#include <utility>

namespace Dojo
{
std::string_view QueueOutcomeName(QueueOutcome outcome)
{
    switch (outcome)
    {
        case QueueOutcome::Queued: return "queued";
        case QueueOutcome::Superseded: return "superseded";
        case QueueOutcome::Kept: return "kept";
        case QueueOutcome::Idle: return "idle";
        case QueueOutcome::Dropped: return "dropped";
    }
    return "?";
}

ActionQueue::ActionQueue(std::size_t actors, std::uint32_t commandDelayMs)
    : _commandDelayMs(commandDelayMs), _pending(actors)
{
}

std::vector<QueueEvent> ActionQueue::Submit(std::size_t actor, std::size_t action, std::uint64_t actionId,
    std::string producer, std::string origin, std::uint64_t nowMs)
{
    QueuedCommand command{actionId, actor, action, std::move(producer), std::move(origin), nowMs, nowMs + _commandDelayMs};
    if (actor >= _pending.size())
        return {{QueueOutcome::Dropped, command, 0, nowMs, "no such actor"}};
    std::optional<QueuedCommand>& slot = _pending[actor];
    if (action == 0)
    {
        if (slot)
            return {{QueueOutcome::Kept, *slot, actionId, nowMs, "noop keeps the pending command"}};
        command.dueMs = nowMs;
        return {{QueueOutcome::Idle, command, 0, nowMs, "noop with nothing pending"}};
    }
    std::vector<QueueEvent> events;
    if (slot)
        events.push_back({QueueOutcome::Superseded, *slot, actionId, nowMs, "replaced before it executed"});
    slot = command;
    events.push_back({QueueOutcome::Queued, command, 0, nowMs, ""});
    return events;
}

std::optional<QueuedCommand> ActionQueue::Pending(std::size_t actor) const
{
    return actor < _pending.size() ? _pending[actor] : std::nullopt;
}

std::vector<QueuedCommand> ActionQueue::TakeDue(std::uint64_t nowMs)
{
    std::vector<QueuedCommand> due;
    for (std::optional<QueuedCommand>& slot : _pending)
        if (slot && slot->dueMs <= nowMs)
        {
            due.push_back(std::move(*slot));
            slot.reset();
        }
    std::stable_sort(due.begin(), due.end(), [](QueuedCommand const& a, QueuedCommand const& b) { return a.dueMs < b.dueMs; });
    return due;
}

std::vector<QueueEvent> ActionQueue::Drop(std::size_t actor, std::string const& reason, std::uint64_t nowMs)
{
    if (actor >= _pending.size() || !_pending[actor])
        return {};
    QueueEvent event{QueueOutcome::Dropped, *_pending[actor], 0, nowMs, reason};
    _pending[actor].reset();
    return {event};
}
}
