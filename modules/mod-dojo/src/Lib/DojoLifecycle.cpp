// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#include "DojoLifecycle.h"

#include <algorithm>
#include <initializer_list>
#include <iterator>
#include <limits>
#include <stdexcept>
#include <utility>

namespace Dojo
{
namespace
{
constexpr std::int64_t kMaxInt64 = std::numeric_limits<std::int64_t>::max();

template <typename T>
Json OrNull(std::optional<T> const& value)
{
    return value ? Json(*value) : Json(nullptr);
}

bool IsLive(State s)
{
    return s == State::Resetting || s == State::AwaitingAction || s == State::Stepping;
}

bool IsResettable(State s)
{
    return s == State::Ready || s == State::AwaitingAction || s == State::Terminal || s == State::Aborted;
}

bool HasExactly(Json const& j, std::initializer_list<std::string_view> keys)
{
    if (!j.is_object() || j.size() != keys.size())
        return false;
    for (std::string_view key : keys)
        if (!j.contains(key))
            return false;
    return true;
}

bool IsTrue(Json const& j)
{
    return j.is_boolean() && j.get<bool>();
}

Json Effect(std::string_view name)
{
    return Json{{"effect", std::string(name)}};
}

Json AbortEffect(std::uint64_t generation, std::string_view reason)
{
    Json e = Effect("abort");
    e["generation"] = generation;
    e["reason"] = std::string(reason);
    return e;
}

Json ReleaseEffect(std::string_view reason)
{
    Json e = Effect("release");
    e["reason"] = std::string(reason);
    return e;
}

std::string Describe(Json const& j)
{
    return j.is_string() ? j.get<std::string>() : j.dump();
}
}

std::string_view StateName(State state)
{
    switch (state)
    {
        case State::Booting: return "booting";
        case State::Ready: return "ready";
        case State::Resetting: return "resetting";
        case State::AwaitingAction: return "awaiting_action";
        case State::Stepping: return "stepping";
        case State::Terminal: return "terminal";
        case State::Aborted: return "aborted";
        case State::Closing: return "closing";
    }
    return "unknown";
}

void Lifecycle::CheckInfo(Json const& info, Bounds const& bounds)
{
    auto fail = [](std::string const& what) { throw std::invalid_argument("worker info: " + what); };
    static constexpr std::string_view keys[] = {"worker", "scenario", "schemas", "actors", "actions", "clock", "rng"};
    if (!info.is_object() || info.size() != std::size(keys))
        fail("needs exactly worker, scenario, schemas, actors, actions, clock and rng");
    for (std::string_view key : keys)
        if (!info.contains(key))
            fail("missing " + std::string(key));
    if (!IsId(info["scenario"]))
        fail("bad scenario");
    Json const& schemas = info["schemas"];
    if (!HasExactly(schemas, {"observation", "action"}) || !IsText(schemas["observation"])
        || !IsText(schemas["action"]))
        fail("bad schemas");
    Json const& actors = info["actors"];
    if (!actors.is_array() || actors.empty() || actors.size() > bounds.maxActors)
        fail("bad actors");
    for (std::size_t slot = 0; slot < actors.size(); ++slot)
    {
        Json const& a = actors[slot];
        auto const s = static_cast<std::int64_t>(slot);
        if (!a.is_object() || !a.contains("slot") || !IsInt(a["slot"], s, s) || !a.contains("team")
            || !IsInt(a["team"], 0, 255))
            fail("bad actors");
    }
    Json const& actions = info["actions"];
    if (!actions.is_array() || actions.empty() || actions[0] != "noop")
        fail("bad actions: a non-empty list of names whose first is noop");
    for (Json const& a : actions)
        if (!IsId(a))
            fail("bad actions");
    for (std::string_view key : {"worker", "clock", "rng"})
        if (!info[std::string(key)].is_object())
            fail("bad " + std::string(key));
}

Lifecycle::Lifecycle(Json info, Bounds bounds, bool booted) : _bounds(bounds)
{
    CheckInfo(info, _bounds);
    _info = std::move(info);
    _actorCount = _info["actors"].size();
    _catalogSize = _info["actions"].size();
    _state = booted ? State::Ready : State::Booting;
}

std::optional<std::string> Lifecycle::InFlight() const
{
    if (!_inflight)
        return std::nullopt;
    return _inflight->request.id;
}

bool Lifecycle::AcceptsCallback(std::uint64_t generation) const
{
    return generation == _generation && IsLive(_state);
}

Output Lifecycle::BootComplete()
{
    if (_state == State::Booting)
        _state = State::Ready;
    return {};
}

Output Lifecycle::Connect(std::string session)
{
    if (_session)
        throw std::logic_error("a session is already connected");
    if (!IsIdText(session))
        throw std::invalid_argument("bad session id");
    _session = std::move(session);
    _helloed = false;
    _cache.clear();
    _recent.clear();
    return {};
}

Output Lifecycle::Disconnect()
{
    Output out;
    if (!_session)
        return out;
    CutEpisode(AbortReason::Disconnect, out);
    out.effects.push_back(ReleaseEffect(AbortReason::Disconnect));
    EndSession();
    return out;
}

Output Lifecycle::Fault()
{
    Output out;
    if (_state == State::Booting || _state == State::Closing)
    {
        _abortReason = std::string(AbortReason::Fault);
        return out;
    }
    if (_inflight)
    {
        Json const failed = MakeError(Err::Aborted, _inflight->request.id, "worker fault", AbortReason::Fault);
        Remember(_inflight->request, failed);
        out.send.insert(out.send.end(), _inflight->copies, failed);
    }
    CutEpisode(AbortReason::Fault, out);
    _state = State::Aborted;
    _abortReason = std::string(AbortReason::Fault);
    _decision.reset();
    _masks.reset();
    return out;
}

Output Lifecycle::Drained()
{
    _draining.reset();
    if (_state == State::Closing)
        _state = State::Ready;
    return {};
}

Output Lifecycle::Handle(Decoded const& decoded)
{
    Output out;
    if (!decoded.request)
    {
        out.send.push_back(MakeError(decoded.code.empty() ? Err::Malformed : std::string_view(decoded.code),
            decoded.id, decoded.message));
        return out;
    }
    Request const& req = *decoded.request;
    if (_inflight && _inflight->request.id == req.id)
    {
        if (_inflight->request.fingerprint == req.fingerprint)
            ++_inflight->copies;
        else
            out.send.push_back(MakeError(Err::ConflictingDuplicate, req.id, "id is in flight with another payload"));
        return out;
    }
    if (Cached const* cached = FindCached(req.id))
    {
        out.send.push_back(cached->fingerprint == req.fingerprint
                ? cached->response
                : MakeError(Err::ConflictingDuplicate, req.id, "id was used for another payload"));
        return out;
    }
    if (req.type == "status")
    {
        out.send.push_back(StatusResult(req));
        return out;
    }
    if (req.type == "hello")
        return HandleHello(req);
    if (!_helloed)
    {
        out.send.push_back(MakeError(Err::NotReady, req.id, "say hello first"));
        return out;
    }
    if (req.body["session"].get<std::string>() != *_session)
    {
        out.send.push_back(MakeError(Err::BadSession, req.id, "not this connection's session"));
        return out;
    }
    if (req.type == "close")
        return HandleClose(req);
    if (_inflight)
    {
        out.send.push_back(MakeError(Err::Busy, req.id, _inflight->request.id + " is in flight"));
        return out;
    }
    return req.type == "reset" ? HandleReset(req) : HandleStep(req);
}

Json Lifecycle::StatusResult(Request const& req) const
{
    return Json{{"v", kProtocolVersion}, {"type", "status_result"}, {"id", req.id}, {"ok", true},
        {"state", std::string(StateName(_state))}, {"session", _helloed ? OrNull(_session) : Json(nullptr)},
        {"episode", OrNull(_episode)}, {"decision", OrNull(_decision)}, {"generation", _generation},
        {"in_flight", OrNull(InFlight())}, {"draining", _draining.has_value()}, {"abort_reason", OrNull(_abortReason)}};
}

Output Lifecycle::HandleHello(Request const& req)
{
    Output out;
    if (!_session || _helloed)
    {
        out.send.push_back(MakeError(Err::NotReady, req.id, "this connection already has a session"));
        return out;
    }
    Json const& protocol = req.body["protocol"];
    if (protocol["min"].get<std::int64_t>() > kProtocolVersion || protocol["max"].get<std::int64_t>() < kProtocolVersion)
    {
        out.send.push_back(MakeError(Err::UnsupportedVersion, req.id, "this worker speaks v1"));
        return out;
    }
    if (req.body.contains("expect"))
    {
        for (auto const& [key, expected] : req.body["expect"].items())
        {
            Json const& mine = key == "observation_schema" ? _info["schemas"]["observation"]
                : key == "action_schema"                    ? _info["schemas"]["action"]
                                                            : _info["scenario"];
            if (expected != mine)
            {
                out.send.push_back(MakeError(Err::UnsupportedVersion, req.id, key + " is " + Describe(mine)));
                return out;
            }
        }
    }
    _helloed = true;
    Json response{{"v", kProtocolVersion}, {"type", "hello_result"}, {"id", req.id}, {"ok", true},
        {"session", *_session}, {"protocol", kProtocolVersion}, {"state", std::string(StateName(_state))},
        {"bounds", _bounds.ToWire()}};
    for (auto const& [key, value] : _info.items())
        response[key] = value;
    Remember(req, response);
    out.send.push_back(std::move(response));
    return out;
}

Output Lifecycle::HandleClose(Request const& req)
{
    Output out;
    if (_inflight)
    {
        Json const failed = MakeError(Err::Aborted, _inflight->request.id, "closed by the controller",
            AbortReason::Closed);
        out.send.insert(out.send.end(), _inflight->copies, failed);
    }
    CutEpisode(AbortReason::Closed, out);
    out.send.push_back(Json{{"v", kProtocolVersion}, {"type", "close_result"}, {"id", req.id}, {"ok", true},
        {"session", *_session}});
    out.effects.push_back(ReleaseEffect(AbortReason::Closed));
    out.effects.push_back(Effect("close_connection"));
    EndSession();
    return out;
}

Output Lifecycle::HandleReset(Request const& req)
{
    Output out;
    Json const& body = req.body;
    if (!IsResettable(_state) || _draining)
    {
        out.send.push_back(MakeError(Err::NotReady, req.id,
            "cannot reset while " + std::string(StateName(_state)) + (_draining ? " (draining)" : "")));
        return out;
    }
    if (body["decision"].get<std::int64_t>() != 0)
    {
        out.send.push_back(MakeError(Err::WrongStep, req.id, "a reset starts at decision 0"));
        return out;
    }
    if (body.contains("scenario") && body["scenario"] != _info["scenario"])
    {
        out.send.push_back(MakeError(Err::UnsupportedVersion, req.id, "scenario is " + Describe(_info["scenario"])));
        return out;
    }
    std::string const episode = body["episode"].get<std::string>();
    if (_episode == episode || std::find(_recent.begin(), _recent.end(), episode) != _recent.end())
    {
        out.send.push_back(MakeError(Err::StaleEpisode, req.id, "episode id was already used"));
        return out;
    }
    ++_generation;
    _episode = episode;
    _recent.push_back(episode);
    while (_recent.size() > kRecentEpisodes)
        _recent.pop_front();
    _decision.reset();
    _masks.reset();
    _abortReason.reset();
    _inflight = InFlightOp{"reset", req};
    _state = State::Resetting;
    Json effect = Effect("begin_reset");
    effect["generation"] = _generation;
    effect["episode"] = episode;
    effect["scenario"] = _info["scenario"];
    effect["scenario_seed"] = body["scenario_seed"];
    out.effects.push_back(std::move(effect));
    return out;
}

Output Lifecycle::HandleStep(Request const& req)
{
    Output out;
    Json const& body = req.body;
    if (_state == State::Aborted)
    {
        std::optional<std::string_view> reason;
        if (_abortReason)
            reason = *_abortReason;
        out.send.push_back(MakeError(Err::Aborted, req.id, "the episode was aborted", reason));
        return out;
    }
    if (_state != State::AwaitingAction)
    {
        out.send.push_back(MakeError(Err::NotReady, req.id, "cannot step while " + std::string(StateName(_state))));
        return out;
    }
    if (body["episode"].get<std::string>() != *_episode)
    {
        out.send.push_back(MakeError(Err::StaleEpisode, req.id, "the episode is " + *_episode));
        return out;
    }
    if (body["decision"].get<std::int64_t>() != *_decision)
    {
        out.send.push_back(MakeError(Err::WrongStep, req.id, "the decision is " + std::to_string(*_decision)));
        return out;
    }
    std::vector<std::int64_t> chosen(_actorCount, -1);
    std::size_t count = 0;
    for (Json const& a : body["actions"])
    {
        auto const actor = a["actor"].get<std::uint64_t>();
        auto const action = a["action"].get<std::uint64_t>();
        if (actor >= _actorCount || chosen[actor] >= 0)
        {
            out.send.push_back(MakeError(Err::InvalidAction, req.id,
                "actor " + std::to_string(actor) + " is unknown or repeated"));
            return out;
        }
        if (action >= _catalogSize || (*_masks)[actor][action] == 0)
        {
            out.send.push_back(MakeError(Err::InvalidAction, req.id,
                "action " + std::to_string(action) + " is not open to " + std::to_string(actor)));
            return out;
        }
        chosen[actor] = static_cast<std::int64_t>(action);
        ++count;
    }
    if (count != _actorCount)
    {
        out.send.push_back(MakeError(Err::InvalidAction, req.id, "every controlled actor needs a command"));
        return out;
    }
    Json actions = Json::array();
    auto const decision = static_cast<std::uint64_t>(*_decision);
    for (std::size_t actor = 0; actor < _actorCount; ++actor)
        actions.push_back(Json{{"actor", actor}, {"action", chosen[actor]},
            {"action_id", decision * _actorCount + actor}});
    _inflight = InFlightOp{"step", req, 1, actions};
    _state = State::Stepping;
    Json effect = Effect("begin_step");
    effect["generation"] = _generation;
    effect["episode"] = *_episode;
    effect["decision"] = *_decision;
    effect["actions"] = std::move(actions);
    out.effects.push_back(std::move(effect));
    return out;
}

Output Lifecycle::CompleteReset(std::uint64_t generation, Json const& payload)
{
    InFlightOp* op = Current("reset", generation);
    if (!op)
    {
        Output stale;
        stale.stale = true;
        return stale;
    }
    Output out;
    if (!payload.is_object() || !payload.contains("ok") || !IsTrue(payload["ok"]))
    {
        std::string const why = payload.is_object() && payload.contains("reason") ? Describe(payload["reason"])
                                                                                 : "reset failed";
        Fail(out, Err::Aborted, AbortReason::ResetFailed, why);
        return out;
    }
    Json const sim = payload.contains("sim_ms") ? payload["sim_ms"] : Json(0);
    auto masks = CheckObservations(payload.contains("observations") ? payload["observations"] : Json(nullptr));
    bool extra = false;
    for (auto const& [key, value] : payload.items())
    {
        (void)value;
        extra = extra || (key != "ok" && key != "observations" && key != "sim_ms");
    }
    if (!masks || !IsInt(sim, 0, kMaxInt64) || extra)
    {
        Fail(out, Err::Aborted, AbortReason::InvalidResult, "reset result does not fit the schema");
        return out;
    }
    Request const& req = op->request;
    Json const response{{"v", kProtocolVersion}, {"type", "reset_result"}, {"id", req.id}, {"ok", true},
        {"session", *_session}, {"episode", *_episode}, {"decision", 0}, {"generation", _generation},
        {"scenario", _info["scenario"]}, {"scenario_seed", req.body["scenario_seed"]}, {"sim_ms", sim},
        {"observations", payload["observations"]}};
    if (!Fits(out, response))
        return out;
    Deliver(out, response);
    _state = State::AwaitingAction;
    _decision = 0;
    _masks = std::move(masks);
    return out;
}

Output Lifecycle::CompleteStep(std::uint64_t generation, Json const& payload)
{
    InFlightOp* op = Current("step", generation);
    if (!op)
    {
        Output stale;
        stale.stale = true;
        return stale;
    }
    Output out;
    if (!payload.is_object())
    {
        Fail(out, Err::Aborted, AbortReason::InvalidResult, "step result is not an object");
        return out;
    }
    if (payload.contains("fault"))
    {
        Fail(out, Err::Aborted, AbortReason::Fault, Describe(payload["fault"]));
        return out;
    }
    if (payload.contains("events") && payload["events"].is_array() && payload["events"].size() > _bounds.maxEvents)
    {
        Fail(out, Err::ResultOverflow, AbortReason::ResultOverflow,
            std::to_string(payload["events"].size()) + " events, the bound is " + std::to_string(_bounds.maxEvents));
        return out;
    }
    auto masks = CheckStepPayload(payload);
    if (!masks)
    {
        Fail(out, Err::Aborted, AbortReason::InvalidResult, "step result does not fit the schema");
        return out;
    }
    Request const& req = op->request;
    Json const response{{"v", kProtocolVersion}, {"type", "step_result"}, {"id", req.id}, {"ok", true},
        {"session", *_session}, {"episode", *_episode}, {"decision", *_decision}, {"next_decision", *_decision + 1},
        {"generation", _generation}, {"requested", op->actions}, {"elapsed_ms", payload["elapsed_ms"]},
        {"substeps", payload["substeps"]}, {"sim_ms", payload["sim_ms"]}, {"observations", payload["observations"]},
        {"events", payload["events"]}, {"rewards", payload["rewards"]}, {"terminated", payload["terminated"]},
        {"truncated", payload["truncated"]}, {"outcome", payload["outcome"]}};
    if (!Fits(out, response))
        return out;
    bool const over = IsTrue(payload["terminated"]) || IsTrue(payload["truncated"]);
    Deliver(out, response);
    _decision = *_decision + 1;
    _masks = std::move(masks);
    _state = over ? State::Terminal : State::AwaitingAction;
    return out;
}

Lifecycle::InFlightOp* Lifecycle::Current(std::string_view kind, std::uint64_t generation)
{
    if (_inflight && _inflight->kind == kind && generation == _generation)
        return &*_inflight;
    if (_draining && generation == *_draining)
        Drained();
    return nullptr;
}

void Lifecycle::CutEpisode(std::string_view reason, Output& out)
{
    if (IsLive(_state))
    {
        out.effects.push_back(AbortEffect(_generation, reason));
        if (_inflight)
            _draining = _generation;
        ++_generation;
    }
    _inflight.reset();
}

void Lifecycle::EndSession()
{
    _session.reset();
    _helloed = false;
    _episode.reset();
    _decision.reset();
    _abortReason.reset();
    _masks.reset();
    _cache.clear();
    _recent.clear();
    if (_state != State::Booting)
        _state = _draining ? State::Closing : State::Ready;
}

void Lifecycle::Fail(Output& out, std::string_view code, std::string_view reason, std::string const& message)
{
    Json const failed = MakeError(code, _inflight->request.id, message, reason);
    Remember(_inflight->request, failed);
    out.send.insert(out.send.end(), _inflight->copies, failed);
    out.effects.push_back(AbortEffect(_generation, reason));
    ++_generation;
    _inflight.reset();
    _decision.reset();
    _masks.reset();
    _state = State::Aborted;
    _abortReason = std::string(reason);
}

bool Lifecycle::Fits(Output& out, Json const& response)
{
    std::size_t size = 0;
    bool encodable = !HasNonFinite(response);
    if (encodable)
    {
        try
        {
            size = Encode(response).size();
        }
        catch (Json::type_error const&)
        {
            encodable = false;
        }
    }
    if (!encodable)
    {
        Fail(out, Err::Aborted, AbortReason::InvalidResult, "result is not encodable");
        return false;
    }
    if (size > _bounds.maxLineBytes)
    {
        Fail(out, Err::ResultOverflow, AbortReason::ResultOverflow,
            "result is " + std::to_string(size) + " bytes, the bound is " + std::to_string(_bounds.maxLineBytes));
        return false;
    }
    return true;
}

void Lifecycle::Deliver(Output& out, Json const& response)
{
    Remember(_inflight->request, response);
    out.send.insert(out.send.end(), _inflight->copies, response);
    _inflight.reset();
}

void Lifecycle::Remember(Request const& req, Json const& response)
{
    auto const same = [&req](Cached const& c) { return c.id == req.id; };
    _cache.erase(std::remove_if(_cache.begin(), _cache.end(), same), _cache.end());
    _cache.push_back(Cached{req.id, req.fingerprint, response});
    while (_cache.size() > std::max<std::size_t>(_bounds.resultCache, 1))
        _cache.pop_front();
}

Lifecycle::Cached const* Lifecycle::FindCached(std::string const& id) const
{
    for (Cached const& c : _cache)
        if (c.id == id)
            return &c;
    return nullptr;
}

std::optional<std::vector<std::vector<int>>> Lifecycle::CheckObservations(Json const& observations) const
{
    if (!observations.is_array() || observations.size() != _actorCount)
        return std::nullopt;
    std::vector<std::vector<int>> masks;
    for (std::size_t slot = 0; slot < _actorCount; ++slot)
    {
        Json const& o = observations[slot];
        auto const s = static_cast<std::int64_t>(slot);
        if (!HasExactly(o, {"actor", "obs", "mask"}) || !IsInt(o["actor"], s, s))
            return std::nullopt;
        Json const& values = o["obs"];
        Json const& mask = o["mask"];
        if (!values.is_array() || values.size() > kMaxObservationValues)
            return std::nullopt;
        for (Json const& v : values)
            if (!IsNumber(v))
                return std::nullopt;
        if (!mask.is_array() || mask.size() != _catalogSize)
            return std::nullopt;
        std::vector<int> bits;
        for (Json const& m : mask)
        {
            if (!IsInt(m, 0, 1))
                return std::nullopt;
            bits.push_back(m.get<int>());
        }
        if (bits[0] != 1)
            return std::nullopt;
        masks.push_back(std::move(bits));
    }
    return masks;
}

std::optional<std::vector<std::vector<int>>> Lifecycle::CheckStepPayload(Json const& payload) const
{
    if (!HasExactly(payload, {"elapsed_ms", "substeps", "sim_ms", "observations", "events", "rewards", "terminated",
            "truncated", "outcome"}))
        return std::nullopt;
    for (char const* key : {"elapsed_ms", "substeps", "sim_ms"})
        if (!IsInt(payload[key], 0, kMaxInt64))
            return std::nullopt;
    Json const& events = payload["events"];
    if (!events.is_array())
        return std::nullopt;
    for (Json const& e : events)
        if (!e.is_object() || !e.contains("kind") || !e["kind"].is_string())
            return std::nullopt;
    Json const& rewards = payload["rewards"];
    if (!rewards.is_array() || rewards.size() != _actorCount)
        return std::nullopt;
    for (std::size_t slot = 0; slot < _actorCount; ++slot)
    {
        Json const& r = rewards[slot];
        auto const s = static_cast<std::int64_t>(slot);
        if (!HasExactly(r, {"actor", "terminal", "shaping"}) || !IsInt(r["actor"], s, s) || !IsNumber(r["terminal"])
            || !IsNumber(r["shaping"]))
            return std::nullopt;
    }
    Json const& terminated = payload["terminated"];
    Json const& truncated = payload["truncated"];
    Json const& outcome = payload["outcome"];
    if (!terminated.is_boolean() || !truncated.is_boolean() || (IsTrue(terminated) && IsTrue(truncated)))
        return std::nullopt;
    if (!IsTrue(terminated) && !IsTrue(truncated))
    {
        if (!outcome.is_null())
            return std::nullopt;
    }
    else
    {
        if (!HasExactly(outcome, {"reason", "winner"}) || !outcome["reason"].is_string())
            return std::nullopt;
        std::string const reason = outcome["reason"].get<std::string>();
        if (reason != "death" && reason != "simultaneous_death" && reason != "time_limit" && reason != "external")
            return std::nullopt;
        if (IsTrue(truncated) != (reason == "external"))
            return std::nullopt;
        if (reason == "death" ? !IsInt(outcome["winner"], 0, 255) : !outcome["winner"].is_null())
            return std::nullopt;
    }
    return CheckObservations(payload["observations"]);
}
}
