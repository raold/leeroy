// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#include "DojoEndpoint.h"

#ifndef DOJO_TRAINING_BUILD
#error "DOJO_TRAINING_BUILD is not set: mod-dojo.cmake was skipped (the module folder must be named exactly mod-dojo)"
#endif

#if DOJO_TRAINING_BUILD

#include "DojoExecutor.h"
#include "DojoFixtures.h"
#include "DojoLifecycle.h"
#include "DojoObservation.h"
#include "DojoOutcome.h"
#include "DojoReset.h"
#include "DojoTransport.h"

#include "Config.h"
#include "GameTime.h"
#include "GitRevision.h"
#include "Log.h"
#include "Util.h"

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <random>

using nlohmann::json;

namespace Dojo
{
namespace
{
constexpr char const* kHost = "127.0.0.1";
constexpr std::uint32_t kDecisionMs = 200;      // D07, D30
constexpr std::size_t kPollPerTick = 16;
constexpr std::size_t kBoundaryLimit = 2048;    // more than an episode's decisions at the 180 s limit
constexpr std::size_t kDetailBytes = 200;

std::uint64_t Now()
{
    return std::uint64_t(GameTime::GetGameTimeMS().count());
}

std::string WireName(std::string name)
{
    // Catalog names join ability, movement and facing with '/'; protocol ids allow A-Za-z0-9._:- only.
    std::replace(name.begin(), name.end(), '/', '.');
    return name;
}

std::string Nonce()
{
    std::random_device device;
    char const* digits = "0123456789abcdef";
    std::string out;
    for (int i = 0; i < 6; ++i)
        out += digits[device() % 16];
    return out;
}
}

Endpoint& Endpoint::Instance()
{
    static Endpoint endpoint;
    return endpoint;
}

Endpoint::~Endpoint() = default;

void Endpoint::Configure(EndpointConfig config)
{
    _config = std::move(config);
    _configured = true;
}

void Endpoint::Start()
{
    Control const& control = Control::Instance();
    Observer const& observer = Observer::Instance();
    json actions = json::array();
    for (CatalogEntry const& e : control.Catalog().entries)
        actions.push_back(WireName(e.name));
    json actors = json::array();
    for (std::size_t i = 0; i < _config.teams.size(); ++i)
        actors.push_back({{"slot", i}, {"team", _config.teams[i]}});
    MaskRules const rules = observer.Rules();
    std::int32_t const tick = sConfigMgr->GetOption<std::int32_t>("MinWorldUpdateTime", 1);
    json info = {
        {"worker", {{"kind", "azerothcore"}, {"module", "mod-dojo"}, {"build", "training"}, {"core", GitRevision::GetHash()},
            {"instance", _config.instance}, {"scenario_sha256", _config.scenarioSha256}, {"pid", GetPID()},
            {"reward", {{"terminal", "+1 to the winning team, -1 to the losing team, 0 on a draw"},
                {"shaping", "scale * (Phi(next) - Phi(current)), Phi = team 0's mean health fraction minus team 1's, "
                    "0 at every terminal state"}, {"shaping_scale", kShapingScale}}},
            {"action_names", "catalog names with '/' written as '.'"}}},
        {"scenario", _config.scenario},
        {"schemas", {{"observation", std::string(kObservationSchema) + ":" + observer.Schema().sha256},
            {"action", std::string(kActionSchema) + ":" + control.Catalog().sha256}}},
        {"actors", actors},
        {"actions", actions},
        {"clock", {{"mode", "realtime"}, {"paced", true}, {"pauseable", false}, {"decision_ms", kDecisionMs},
            {"tick_ms", tick}, {"command_delay_ms", rules.commandDelayMs}, {"perceptual_delay_ms", rules.perceptualDelayMs},
            {"spell_queue_ms", rules.queueWindowMs}, {"time_limit_ms", _config.matchMs},
            {"elapsed", "game time between consecutive boundaries; the world runs on while the controller decides"}}},
        {"rng", {{"scenario_seed", false}, {"combat_replay", false}}},
    };
    try
    {
        _lifecycle = std::make_unique<Lifecycle>(std::move(info));
    }
    catch (std::exception const& e)
    {
        _failed = true;
        LOG_ERROR("module.dojo", "mod-dojo: endpoint refused: {}", e.what());
        return;
    }
    auto log = [](Transport::LogLevel level, std::string const& text)
    {
        if (level == Transport::LogLevel::Error)
            LOG_ERROR("module.dojo", "mod-dojo endpoint: {}", text);
        else if (level == Transport::LogLevel::Warn)
            LOG_WARN("module.dojo", "mod-dojo endpoint: {}", text);
        else
            LOG_INFO("module.dojo", "mod-dojo endpoint: {}", text);
    };
    Transport::Options options;
    options.bounds = _lifecycle->GetBounds();
    options.inboxCapacity = options.bounds.maxCopies;
    _transport = std::make_unique<Transport>(log, options);
    if (!_transport->Start(kHost, 0))
    {
        _failed = true;
        _transport.reset();
        LOG_ERROR("module.dojo", "mod-dojo: endpoint could not bind {}", kHost);
        return;
    }
    _port = _transport->BoundPort();
    _nonce = Nonce();
    WriteReceipt();
    LOG_INFO("module.dojo", "mod-dojo: endpoint listening on {}:{} (protocol {}, scenario {}, {} actions), receipt {}",
        kHost, _port, kProtocolVersion, _config.scenario, control.Catalog().entries.size(),
        _receipt.empty() ? std::string("none (no PidFile)") : _receipt);
}

void Endpoint::WriteReceipt()
{
    std::string const pidFile = sConfigMgr->GetOption<std::string>("PidFile", "");
    if (pidFile.empty())
        return;
    std::filesystem::path const path = std::filesystem::path(pidFile).parent_path() / "dojo-endpoint.json";
    json const receipt = {{"kind", "leeroy-dojo-receipt"}, {"worker", "azerothcore"}, {"instance", _config.instance},
        {"pid", GetPID()}, {"host", kHost}, {"port", _port}, {"protocol", kProtocolVersion}, {"scenario", _config.scenario},
        {"scenario_sha256", _config.scenarioSha256}, {"started_ms", Now()}};
    std::error_code error;
    std::filesystem::path const tmp = path.string() + ".tmp";
    {
        std::ofstream out(tmp, std::ios::binary | std::ios::trunc);
        out << receipt.dump(2) << "\n";
        if (!out)
        {
            LOG_ERROR("module.dojo", "mod-dojo: cannot write the endpoint receipt {}", tmp.string());
            return;
        }
    }
    std::filesystem::rename(tmp, path, error);
    if (error)
    {
        LOG_ERROR("module.dojo", "mod-dojo: cannot place the endpoint receipt {}: {}", path.string(), error.message());
        return;
    }
    _receipt = path.generic_string();
}

void Endpoint::Shutdown()
{
    if (_transport)
        _transport->Stop();
    if (!_receipt.empty())
    {
        std::error_code error;
        std::filesystem::remove(_receipt, error);
    }
}

void Endpoint::Update()
{
    if (!_configured || _failed)
        return;
    if (!_transport)
    {
        Start();
        if (!_transport)
            return;
    }
    ++_ticks;
    std::vector<TransportEvent> events;
    _transport->Poll(events, kPollPerTick);
    for (TransportEvent const& e : events)
        Handle(e);
    for (json& e : Episode::Instance().TakeEvents())
        if (_live)
            _pending.push_back(std::move(e));
    Progress(Now());
}

void Endpoint::Handle(TransportEvent const& event)
{
    switch (event.kind)
    {
        case TransportEvent::Kind::Connected:
        {
            if (_connection)
                Apply(_lifecycle->Disconnect());
            _connection = event.connection;
            ++_counts.connections;
            std::string const session = "s" + std::to_string(event.connection) + "-" + _nonce;
            try
            {
                Apply(_lifecycle->Connect(session));
            }
            catch (std::exception const& e)
            {
                LOG_ERROR("module.dojo", "mod-dojo: endpoint session refused: {}", e.what());
                _transport->Close(event.connection);
                _connection = 0;
                return;
            }
            LOG_INFO("module.dojo", "mod-dojo: endpoint connection {} (session {})", event.connection, session);
            break;
        }
        case TransportEvent::Kind::Line:
            if (event.connection == _connection)
            {
                ++_counts.requests;
                Apply(_lifecycle->Handle(event.decoded));
            }
            break;
        case TransportEvent::Kind::Disconnected:
            if (event.connection == _connection)
            {
                Apply(_lifecycle->Disconnect());
                _connection = 0;
                LOG_INFO("module.dojo", "mod-dojo: endpoint connection {} closed", event.connection);
            }
            break;
    }
}

void Endpoint::Apply(Output const& out)
{
    for (json const& line : out.send)
    {
        std::string text;
        try
        {
            text = Encode(line);
        }
        catch (std::exception const& e)
        {
            LOG_ERROR("module.dojo", "mod-dojo: endpoint response not encodable: {}", e.what());
            continue;
        }
        if (_connection)
            _transport->Send(_connection, std::move(text));
    }
    for (json const& effect : out.effects)
        Effect(effect);
}

void Endpoint::Effect(json const& effect)
{
    std::string const kind = effect["effect"].get<std::string>();
    if (kind == "begin_reset")
        BeginReset(effect);
    else if (kind == "begin_step")
        BeginStep(effect);
    else if (kind == "abort")
    {
        std::uint64_t const generation = effect["generation"].get<std::uint64_t>();
        std::string const reason = effect["reason"].get<std::string>();
        if (_op && _op->generation == generation)
        {
            _abandoned.push_back(std::move(*_op));
            _op.reset();
            ++_counts.abandoned;
        }
        Episode& episode = Episode::Instance();
        if (generation == _lifecycleGeneration && episode.Generation() == _episodeGeneration
            && (episode.Busy() || episode.Running()))
            episode.AbortFor("protocol: " + reason);
        _live = false;
        LOG_INFO("module.dojo", "mod-dojo: endpoint aborted protocol episode {} ({})", _episode, reason);
    }
    else if (kind == "release")
        LOG_INFO("module.dojo", "mod-dojo: endpoint session ended ({})", effect["reason"].get<std::string>());
    else if (kind == "close_connection" && _connection)
        _transport->Close(_connection);
}

void Endpoint::BeginReset(json const& effect)
{
    Op op;
    op.generation = effect["generation"].get<std::uint64_t>();
    op.token = effect["token"].get<std::uint64_t>();
    std::string const episodeId = effect["episode"].get<std::string>();
    Episode& episode = Episode::Instance();
    std::string error;
    if (!episode.BeginProtocolReset("protocol episode " + episodeId, error))
        op.error = error.empty() ? "the episode refused the reset" : error;
    else
    {
        op.episodeGeneration = episode.Generation();
        _episode = episodeId;
        _lifecycleGeneration = op.generation;
        _episodeGeneration = op.episodeGeneration;
        _live = true;
        _pending.clear();
        _actionIds.clear();
        _boundaries.clear();
    }
    _op = std::move(op);
}

void Endpoint::BeginStep(json const& effect)
{
    Op op;
    op.step = true;
    op.generation = effect["generation"].get<std::uint64_t>();
    op.token = effect["token"].get<std::uint64_t>();
    op.decision = effect["decision"].get<std::int64_t>();
    op.acceptMs = Now();
    Episode& episode = Episode::Instance();
    bool const running = _live && episode.Running() && episode.Generation() == _episodeGeneration;
    std::uint64_t const t = op.acceptMs >= episode.StartMs() ? op.acceptMs - episode.StartMs() : 0;
    for (json const& a : effect["actions"])
    {
        std::size_t const actor = a["actor"].get<std::size_t>();
        std::size_t const action = a["action"].get<std::size_t>();
        std::int64_t const actionId = a["action_id"].get<std::int64_t>();
        std::string error = running ? "" : "the episode had ended";
        std::uint64_t executorId = 0;
        if (running)
            executorId = Control::Instance().Enqueue(actor, action, "protocol",
                "step:" + std::to_string(_episodeGeneration) + ":" + std::to_string(op.decision), error);
        if (executorId)
            _actionIds[executorId] = actionId;
        else
        {
            ++_counts.refusedCommands;
            op.notes.push_back({{"kind", "command"}, {"producer", "protocol"}, {"outcome", "refused"}, {"actor", actor},
                {"action_id", actionId}, {"t_ms", t}, {"detail", Clip(error, kDetailBytes)}});
        }
    }
    _op = std::move(op);
}

void Endpoint::Progress(std::uint64_t now)
{
    // Operations the lifecycle abandoned end its drain once nothing of them can still happen.
    std::vector<Op> abandoned;
    abandoned.swap(_abandoned);
    for (Op& op : abandoned)
    {
        if (op.step)
            Apply(_lifecycle->CompleteStep(op.generation, op.token, json{{"fault", "abandoned"}}));
        else if (!Director::Instance().JobRunning() && !Episode::Instance().Busy())
            Apply(_lifecycle->CompleteReset(op.generation, op.token, json{{"ok", false}, {"reason", "abandoned"}}));
        else
            _abandoned.push_back(std::move(op));
    }

    Episode& episode = Episode::Instance();
    if (_op && !_op->step)
    {
        CompleteReset(now);
        return;
    }
    // The episode stopped under a live protocol episode: a worker fault, never a lost game.
    std::string const state(_lifecycle->StateString());
    bool const live = state == "awaiting_action" || state == "stepping";
    bool const ours = episode.Generation() == _episodeGeneration && (episode.Running() || episode.Ended());
    if (_live && live && !ours)
    {
        ++_counts.faults;
        LOG_WARN("module.dojo", "mod-dojo: endpoint episode {} faulted: {}", _episode, episode.Result());
        _live = false;
        Apply(_lifecycle->Fault());
        return;
    }
    if (_op && _op->step && (episode.Ended() || now - _op->acceptMs >= kDecisionMs))
        CompleteStep(now);
}

void Endpoint::CompleteReset(std::uint64_t now)
{
    Episode& episode = Episode::Instance();
    std::string failure = _op->error;
    if (failure.empty() && episode.Generation() != _op->episodeGeneration)
        failure = "another reset replaced the episode";
    else if (failure.empty() && (episode.Stopped() || episode.Ended()))
        failure = episode.Result();
    else if (failure.empty() && !episode.Running())
        return;   // still resetting
    json observations = json::array();
    std::string why;
    if (failure.empty() && !Observations(now, observations, why))
        failure = why;
    Op const op = std::move(*_op);
    _op.reset();
    if (!failure.empty())
    {
        ++_counts.resetFailures;
        _live = false;
        LOG_WARN("module.dojo", "mod-dojo: endpoint reset of {} failed: {}", _episode, failure);
        Apply(_lifecycle->CompleteReset(op.generation, op.token, json{{"ok", false}, {"reason", Clip(failure, kDetailBytes)}}));
        return;
    }
    ++_counts.resets;
    _boundaryMs = now;
    _boundaryTicks = _ticks;
    _potential = Potential(episode.Standings());
    _traceSeq = Control::Instance().TraceSeq();
    _pending.clear();
    Boundary(now, 0);
    Apply(_lifecycle->CompleteReset(op.generation, op.token, json{{"ok", true}, {"sim_ms", now}, {"observations", observations}}));
}

void Endpoint::CompleteStep(std::uint64_t now)
{
    Episode& episode = Episode::Instance();
    Op op = std::move(*_op);
    _op.reset();
    json observations = json::array();
    std::string why;
    bool lost = false;
    std::vector<TraceRecord> const records = Control::Instance().RecordsSince(_traceSeq, lost);
    if (!Observations(now, observations, why) || lost)
    {
        ++_counts.faults;
        _live = false;
        Apply(_lifecycle->CompleteStep(op.generation, op.token,
            json{{"fault", lost ? std::string("trace records were lost from the buffer") : why}}));
        return;
    }
    std::uint64_t const start = episode.StartMs();
    json events = json::array();
    std::vector<json> all;
    for (TraceRecord const& r : records)
        if (r.producer != "episode")
            all.push_back(EventOf(r, start));
    for (json& n : op.notes)
        all.push_back(std::move(n));
    for (json& e : _pending)
        all.push_back(std::move(e));
    _pending.clear();
    std::stable_sort(all.begin(), all.end(),
        [](json const& a, json const& b) { return a.value("t_ms", std::uint64_t(0)) < b.value("t_ms", std::uint64_t(0)); });
    for (json& e : all)
        events.push_back(std::move(e));

    Outcome const outcome = episode.Ended() ? episode.GetOutcome() : Outcome{};
    std::vector<Standing> const standings = episode.Standings();
    std::vector<Reward> const rewards = Rewards(standings, _potential, outcome);
    json rewardJson = json::array();
    for (std::size_t i = 0; i < rewards.size(); ++i)
        rewardJson.push_back({{"actor", i}, {"terminal", rewards[i].terminal}, {"shaping", rewards[i].shaping}});
    json const payload = {{"elapsed_ms", now - _boundaryMs}, {"substeps", _ticks - _boundaryTicks}, {"sim_ms", now},
        {"observations", observations}, {"events", events}, {"rewards", rewardJson}, {"terminated", outcome.Over()},
        {"truncated", false}, {"outcome", OutcomeToJson(outcome)}};
    _boundaryMs = now;
    _boundaryTicks = _ticks;
    _potential = outcome.Over() ? 0.0 : Potential(standings);
    _traceSeq = Control::Instance().TraceSeq();
    ++_counts.steps;
    if (outcome.Over())
    {
        ++_counts.terminal;
        _live = false;
    }
    Boundary(now, op.decision + 1);
    Apply(_lifecycle->CompleteStep(op.generation, op.token, payload));
}

bool Endpoint::Observations(std::uint64_t now, json& out, std::string& why) const
{
    out = json::array();
    Control const& control = Control::Instance();
    for (std::size_t i = 0; i < _config.teams.size(); ++i)
    {
        std::string reason;
        std::optional<ActorObservation> const o = Observer::Instance().Observe(i, now, reason);
        if (!o)
        {
            why = "actor " + std::to_string(i) + " has no observation (" + reason + ")";
            return false;
        }
        // Collection-time masks answer for what the actor knew: its delayed observation, never current state.
        MaskResult const mask = ComputeMask(control.Catalog(), o->knowledge, Observer::Instance().Rules());
        json bits = json::array();
        for (std::uint8_t b : mask.mask)
            bits.push_back(int(b));
        out.push_back({{"actor", i}, {"obs", o->values}, {"mask", bits}});
    }
    return true;
}

json Endpoint::EventOf(TraceRecord const& r, std::uint64_t startMs) const
{
    auto rel = [startMs](std::uint64_t ms) { return ms >= startMs ? ms - startMs : 0; };
    json e = {{"kind", r.kind}, {"producer", r.producer}, {"outcome", r.outcome}, {"actor", r.actor}, {"t_ms", rel(r.atMs)}};
    if (r.actionId)
    {
        auto const found = _actionIds.find(r.actionId);
        e["action_id"] = found != _actionIds.end() ? json(found->second) : json(nullptr);
        e["executor_id"] = r.actionId;
    }
    if (!r.action.empty())
        e["action"] = WireName(r.action);
    if (r.inputMs)
        e["input_t_ms"] = rel(r.inputMs);
    if (r.dueMs)
        e["due_t_ms"] = rel(r.dueMs);
    if (r.execMs)
        e["exec_t_ms"] = rel(r.execMs);
    if (!r.detail.empty())
        e["detail"] = Clip(r.detail, kDetailBytes);
    return e;
}

void Endpoint::Boundary(std::uint64_t now, std::int64_t decision)
{
    json const debug = Observer::Instance().DebugJson(1);
    json standings = json::array();
    for (Standing const& s : Episode::Instance().Standings())
        standings.push_back({{"team", s.team}, {"alive", s.alive}, {"health", s.health}});
    _boundaries.push_back({{"episode", _episode}, {"generation", _lifecycleGeneration},
        {"episode_generation", _episodeGeneration}, {"decision", decision}, {"at_ms", now},
        {"standings", standings}, {"debug", debug.empty() ? json(nullptr) : debug.back()}});
    while (_boundaries.size() > kBoundaryLimit)
        _boundaries.pop_front();
}

std::string Endpoint::Status() const
{
    json doc = {{"configured", _configured}, {"failed", _failed}, {"listening", _transport && _transport->Running()},
        {"host", kHost}, {"port", _port}, {"receipt", _receipt}, {"connection", _connection},
        {"state", _lifecycle ? std::string(_lifecycle->StateString()) : std::string("none")},
        {"session", _lifecycle && _lifecycle->Session() ? json(*_lifecycle->Session()) : json(nullptr)},
        {"generation", _lifecycle ? _lifecycle->Generation() : 0},
        {"in_flight", _lifecycle && _lifecycle->InFlight() ? json(*_lifecycle->InFlight()) : json(nullptr)},
        {"episode", _episode}, {"episode_generation", _episodeGeneration}, {"live", _live},
        {"decision", _lifecycle && _lifecycle->Decision() ? json(*_lifecycle->Decision()) : json(nullptr)},
        {"ticks", _ticks}, {"boundaries", _boundaries.size()},
        {"counts", {{"connections", _counts.connections}, {"requests", _counts.requests}, {"resets", _counts.resets},
            {"reset_failures", _counts.resetFailures}, {"steps", _counts.steps}, {"terminal", _counts.terminal},
            {"faults", _counts.faults}, {"abandoned", _counts.abandoned}, {"refused_commands", _counts.refusedCommands}}}};
    if (_transport)
    {
        TransportStats const s = _transport->Stats();
        doc["transport"] = {{"lines_in", s.linesIn}, {"lines_out", s.linesOut}, {"refused", s.refused},
            {"dropped_sends", s.droppedSends}, {"read_pauses", s.readPauses}};
    }
    return "dojo-endpoint " + CanonicalJson(doc);
}

std::string Endpoint::Boundaries(std::uint64_t from, std::size_t max) const
{
    std::string out;
    std::size_t shown = 0;
    for (json const& b : _boundaries)
    {
        if (b["decision"].get<std::int64_t>() < std::int64_t(from))
            continue;
        if (shown == max)
            break;
        out += "dojo-boundary " + CanonicalJson(b) + "\n";
        ++shown;
    }
    return out + "dojo endpoint boundaries: " + std::to_string(shown) + " from decision " + std::to_string(from)
        + " of episode " + _episode + " (" + std::to_string(_boundaries.size()) + " kept); authoritative state, never "
        "part of an actor's observation";
}
}

#endif
