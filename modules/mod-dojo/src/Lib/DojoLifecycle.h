// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_DOJO_DOJO_LIFECYCLE_H
#define MOD_DOJO_DOJO_LIFECYCLE_H

#include "DojoProtocol.h"

#include <cstdint>
#include <deque>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace Dojo
{
// booting -> ready -> resetting -> awaiting_action -> stepping -> awaiting_action | terminal, with aborted and
// closing exits.
enum class State : std::uint8_t { Booting, Ready, Resetting, AwaitingAction, Stepping, Terminal, Aborted, Closing };

std::string_view StateName(State state);

// What one event produced: response lines for the connection (in order), effects for the worker adapter
// (begin_reset, begin_step, abort, release, close_connection) and whether a completion was ignored as stale.
struct Output
{
    std::vector<Json> send;
    std::vector<Json> effects;
    bool stale = false;
};

// The worker's lifecycle state machine: pure, single-threaded, no I/O and no clock. Completions and callbacks carry
// the generation they started under; an older generation is ignored, so abandoned work cannot touch a new episode.
// Completions also carry the token of their begin_reset or begin_step effect, so a late or repeated completion of an
// earlier operation can never finish the one in flight. At most one reset or step is in flight; up to
// Bounds::maxCopies exact duplicates of it, the original included, are answered again when it completes and further
// copies get busy; an exact duplicate of a recent result is replayed from a bounded cache, and a different payload
// under a known id is a conflicting_duplicate. Every request line gets exactly one response line.
// dojo/leeroy_dojo/lifecycle.py is the same machine; the shared golden fixture checks that they agree.
class Lifecycle
{
public:
    // info: worker, scenario, schemas, actors, actions, clock and rng, echoed in hello_result.
    // Throws std::invalid_argument when it is not a usable worker description.
    explicit Lifecycle(Json info, Bounds bounds = {}, bool booted = true);

    State GetState() const { return _state; }
    std::string_view StateString() const { return StateName(_state); }
    std::optional<std::string> const& Session() const { return _session; }
    std::optional<std::string> const& Episode() const { return _episode; }
    std::optional<std::int64_t> Decision() const { return _decision; }
    std::uint64_t Generation() const { return _generation; }
    std::uint64_t LastToken() const { return _tokens; }   // the token of the latest begin effect, 0 before any
    std::optional<std::string> InFlight() const;
    Bounds const& GetBounds() const { return _bounds; }
    bool AcceptsCallback(std::uint64_t generation) const;

    Output BootComplete();
    Output Connect(std::string session);   // throws std::logic_error when a session is connected
    Output Disconnect();
    Output Fault();
    Output Drained();
    Output Handle(Decoded const& decoded);
    Output CompleteReset(std::uint64_t generation, std::uint64_t token, Json const& payload);
    Output CompleteStep(std::uint64_t generation, std::uint64_t token, Json const& payload);

    static void CheckInfo(Json const& info, Bounds const& bounds);

private:
    struct InFlightOp
    {
        std::string kind;
        Request request;
        std::uint64_t token = 0;
        std::size_t copies = 1;
        Json actions = Json::array();
    };

    struct Cached
    {
        std::string id;
        std::string fingerprint;
        Json response;
    };

    Json StatusResult(Request const& req) const;
    Output HandleHello(Request const& req);
    Output HandleClose(Request const& req);
    Output HandleReset(Request const& req);
    Output HandleStep(Request const& req);
    InFlightOp* Current(std::string_view kind, std::uint64_t generation, std::uint64_t token);
    void CutEpisode(std::string_view reason, Output& out);
    void EndSession();
    void Fail(Output& out, std::string_view code, std::string_view reason, std::string const& message);
    bool Fits(Output& out, Json const& response);
    void Deliver(Output& out, Json const& response);
    void Remember(Request const& req, Json const& response);
    Cached const* FindCached(std::string const& id) const;
    std::optional<std::vector<std::vector<int>>> CheckObservations(Json const& observations) const;
    std::optional<std::vector<std::vector<int>>> CheckStepPayload(Json const& payload) const;

    Json _info;
    Bounds _bounds;
    std::size_t _actorCount = 0;
    std::size_t _catalogSize = 0;
    State _state = State::Ready;
    std::optional<std::string> _session;
    bool _helloed = false;
    std::optional<std::string> _episode;
    std::optional<std::int64_t> _decision;
    std::uint64_t _generation = 0;
    std::uint64_t _tokens = 0;
    std::optional<std::pair<std::uint64_t, std::uint64_t>> _draining;   // generation and token of abandoned work
    std::optional<std::string> _abortReason;
    std::optional<InFlightOp> _inflight;
    std::optional<std::vector<std::vector<int>>> _masks;
    std::deque<Cached> _cache;
    std::deque<std::string> _recent;
};
}

#endif
