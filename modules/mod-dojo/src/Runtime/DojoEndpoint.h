// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_DOJO_DOJO_ENDPOINT_H
#define MOD_DOJO_DOJO_ENDPOINT_H

#include <cstdint>
#include <deque>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

// The training worker's protocol endpoint (plan Task 8, D14-D17): the loopback NDJSON transport and the lifecycle state
// machine of src/Lib, driving the episode (DojoReset.h), the controller's queue (DojoExecutor.h) and the actors'
// observations (DojoObservation.h). Training build only, world thread only: the transport's io thread frames and decodes
// lines, and everything else, polling included, happens here once per world tick after the episode's own update.
//
// This is the realtime diagnostic controller's endpoint of milestone M1, not a pauseable environment: the world keeps
// running between steps. A step's commands are queued when the world thread takes the step, under the ordinary
// command delay, and the step completes on the first tick at least decision_ms of game time later, or at once when
// the episode ends. Its result reports what actually advanced since the observations the commands answered:
// elapsed_ms of game time and the world ticks (substeps) in it. hello says so: clock mode realtime, paced, not
// pauseable; the RNG promises neither scenario seeds nor combat replay.
//
// A reset is the episode's own (fresh instance, fresh logins, clean-state verification) and is answered only once the
// episode is ready, with both actors' delayed observations and masks. Each step's events are the controller's trace
// records since the last boundary (commands, casts, the core's cancellations, anything stock), with the protocol's
// action ids, plus the episode's damage and death events; its rewards and outcome come from DojoOutcome.h. An episode
// the worker aborts (an actor offline, perception stopped, the core ended the arena, a console abort) is a worker fault
// to the lifecycle, never a lost game. The authoritative state at every decision boundary is kept apart, for the
// tooling's separate debug stream (dojo endpoint boundaries).
//
// The endpoint binds 127.0.0.1 on an ephemeral port and writes a process receipt next to the worldserver's PidFile
// (dojo-endpoint.json: pid, host, port, scenario and schemas); one controller at a time.
namespace Dojo
{
class Lifecycle;
class Transport;
struct TraceRecord;
struct TransportEvent;
struct Output;

struct EndpointConfig
{
    std::string instance;
    std::string scenario;              // the manifest's id
    std::string scenarioSha256;
    std::vector<std::uint32_t> teams;  // per actor slot: 0 alliance (gold), 1 horde (green)
    std::uint32_t matchMs = 0;
};

class Endpoint
{
public:
    static Endpoint& Instance();
    ~Endpoint();

    // Once the scenario is ready; the endpoint starts on the next world tick.
    void Configure(EndpointConfig config);
    void Update();
    void Shutdown();

    // Console.
    std::string Status() const;
    std::string Boundaries(std::uint64_t from, std::size_t max) const;

private:
    struct Op
    {
        bool step = false;
        std::uint64_t generation = 0;
        std::uint64_t token = 0;
        std::uint64_t episodeGeneration = 0;   // reset: the episode's generation it started
        std::int64_t decision = 0;
        std::uint64_t acceptMs = 0;
        std::string error;                     // reset: refused at once
        std::vector<nlohmann::json> notes;     // step: commands that were not queued
    };

    void Start();
    void Handle(TransportEvent const& event);
    void Apply(Output const& out);
    void Effect(nlohmann::json const& effect);
    void BeginReset(nlohmann::json const& effect);
    void BeginStep(nlohmann::json const& effect);
    void Progress(std::uint64_t now);
    void CompleteReset(std::uint64_t now);
    void CompleteStep(std::uint64_t now);
    bool Observations(std::uint64_t now, nlohmann::json& out, std::string& why) const;
    nlohmann::json EventOf(TraceRecord const& record, std::uint64_t startMs) const;
    void Boundary(std::uint64_t now, std::int64_t decision);
    void WriteReceipt();

    EndpointConfig _config;
    bool _configured = false;
    bool _failed = false;
    std::unique_ptr<Lifecycle> _lifecycle;
    std::unique_ptr<Transport> _transport;
    std::string _nonce;
    std::string _receipt;
    std::uint16_t _port = 0;
    std::uint64_t _connection = 0;
    std::uint64_t _ticks = 0;

    std::optional<Op> _op;
    std::vector<Op> _abandoned;

    // The protocol episode.
    bool _live = false;
    std::string _episode;
    std::uint64_t _lifecycleGeneration = 0;
    std::uint64_t _episodeGeneration = 0;
    std::uint64_t _boundaryMs = 0;
    std::uint64_t _boundaryTicks = 0;
    double _potential = 0;
    std::uint64_t _traceSeq = 0;
    std::map<std::uint64_t, std::int64_t> _actionIds;   // executor action id -> protocol action id
    std::vector<nlohmann::json> _pending;              // the episode's damage and death events since the boundary
    std::deque<nlohmann::json> _boundaries;            // authoritative state at each boundary, never an observation

    struct Counts
    {
        std::uint64_t connections = 0, requests = 0, resets = 0, resetFailures = 0, steps = 0, terminal = 0;
        std::uint64_t faults = 0, abandoned = 0, refusedCommands = 0;
    } _counts;
};
}

#endif
