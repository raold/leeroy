// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_DOJO_DOJO_ACTION_QUEUE_H
#define MOD_DOJO_DOJO_ACTION_QUEUE_H

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

// D30's pending tactical commands, one per actor. No AzerothCore includes; times are milliseconds on whatever clock
// the caller passes (ordinary time in plan Task 6, simulation time once the step driver arrives).
//
// Rules: a command becomes due commandDelayMs after its input. A new command replaces an actor's pending command that
// has not been taken for execution yet; the replaced one is recorded as superseded by the new action id. A noop never
// enters the queue and leaves a pending command as it is. TakeDue hands out due commands oldest first (actor order on
// a tie) and removes them, so an executing command is never superseded: stopping a cast in progress takes an explicit
// stop-cast, movement or the game's own rules. Drop removes an actor's pending command (release, logout, shutdown).
namespace Dojo
{
struct QueuedCommand
{
    std::uint64_t actionId = 0;
    std::size_t actor = 0;
    std::size_t action = 0;      // catalog index, never the noop
    std::string producer;        // who asked: "console", "learner", ...
    std::string origin;          // the request that carried it
    std::uint64_t inputMs = 0;
    std::uint64_t dueMs = 0;
};

enum class QueueOutcome
{
    Queued,       // the command is now pending
    Superseded,   // the command was replaced before it executed
    Kept,         // a noop left this pending command in place
    Idle,         // a noop with nothing pending
    Dropped,      // removed without executing, or refused (no such actor)
};

std::string_view QueueOutcomeName(QueueOutcome outcome);

struct QueueEvent
{
    QueueOutcome outcome = QueueOutcome::Queued;
    QueuedCommand command;       // the command the event is about (for Idle, the noop itself)
    std::uint64_t byActionId = 0;   // Superseded: the replacement; Kept: the noop
    std::uint64_t atMs = 0;
    std::string reason;
};

class ActionQueue
{
public:
    ActionQueue(std::size_t actors, std::uint32_t commandDelayMs);

    // action 0 is the noop.
    std::vector<QueueEvent> Submit(std::size_t actor, std::size_t action, std::uint64_t actionId, std::string producer,
        std::string origin, std::uint64_t nowMs);
    std::optional<QueuedCommand> Pending(std::size_t actor) const;
    std::vector<QueuedCommand> TakeDue(std::uint64_t nowMs);
    std::vector<QueueEvent> Drop(std::size_t actor, std::string const& reason, std::uint64_t nowMs);

    std::size_t Actors() const { return _pending.size(); }
    std::uint32_t CommandDelayMs() const { return _commandDelayMs; }

private:
    std::uint32_t _commandDelayMs;
    std::vector<std::optional<QueuedCommand>> _pending;
};
}

#endif
