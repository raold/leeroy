// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#include "DojoReset.h"

#include "DojoExecutor.h"
#include "DojoFixtures.h"
#include "DojoObservation.h"
#include "DojoRuntime.h"
#include "DojoSha256.h"

#ifndef DOJO_TRAINING_BUILD
#error "DOJO_TRAINING_BUILD is not set: mod-dojo.cmake was skipped (the module folder must be named exactly mod-dojo)"
#endif

#if DOJO_TRAINING_BUILD

#include "Arena.h"
#include "Battleground.h"
#include "GameTime.h"
#include "Log.h"
#include "MotionMaster.h"
#include "MoveSpline.h"
#include "Pet.h"
#include "Player.h"
#include "Spell.h"
#include "SpellAuras.h"
#include "SpellInfo.h"
#include "SpellMgr.h"

#include <algorithm>
#include <cmath>
#include <set>

using nlohmann::json;

namespace Dojo
{
namespace
{
constexpr std::uint32_t kGateSettleMs = 200;       // after the gates open, for the arena's own start work to have run
constexpr std::uint32_t kGateSlackMs = 30000;      // past the preparation time
constexpr std::uint32_t kSettleLimitMs = 3000;     // for the first matured observations
constexpr float kStartTolerance = 1.0f;            // yards from the frozen start position
constexpr float kFacingTolerance = 0.05f;          // radians
constexpr std::uint32_t kTeamMarkers[] = {SPELL_ALLIANCE_GOLD_FLAG, SPELL_ALLIANCE_GREEN_FLAG, SPELL_HORDE_GOLD_FLAG,
    SPELL_HORDE_GREEN_FLAG};

std::uint64_t Now()
{
    return std::uint64_t(GameTime::GetGameTimeMS().count());
}

bool IsTeamMarker(std::uint32_t spellId)
{
    return std::find(std::begin(kTeamMarkers), std::end(kTeamMarkers), spellId) != std::end(kTeamMarkers);
}

std::string Join(std::vector<std::string> const& parts)
{
    std::string out;
    for (std::string const& p : parts)
        out += (out.empty() ? "" : "; ") + p;
    return out;
}

std::string Bits(std::vector<std::uint8_t> const& mask)
{
    std::string bits;
    for (std::uint8_t b : mask)
        bits += b ? '1' : '0';
    return bits;
}

float AngleBetween(float a, float b)
{
    constexpr float kPi = 3.14159265358979323846f;
    float const d = std::fmod(std::fabs(a - b), 2.0f * kPi);
    return d > kPi ? 2.0f * kPi - d : d;
}
}

Episode& Episode::Instance()
{
    static Episode episode;
    return episode;
}

char const* Episode::PhaseName(Phase phase)
{
    switch (phase)
    {
        case Phase::Idle: return "idle";
        case Phase::Teardown: return "resetting";
        case Phase::Gates: return "resetting: waiting for the gates";
        case Phase::Settle: return "resetting: first observations";
        case Phase::Running: return "running";
        case Phase::Ended: return "ended";
        case Phase::Aborted: return "aborted";
        case Phase::Failed: return "failed";
    }
    return "?";
}

bool Episode::Busy() const
{
    return _phase == Phase::Teardown || _phase == Phase::Gates || _phase == Phase::Settle;
}

std::string Episode::Reset(std::string const& reason)
{
    if (!State().scenarioReady || !Observer::Instance().Ready() || !Control::Instance().Ready())
        return "dojo episode reset: refused, the scenario is not ready (see dojo scenario)";
    if (Busy())
        return "dojo episode reset: refused, generation " + std::to_string(_generation) + " is still " + PhaseName(_phase);
    if (Director::Instance().JobRunning())
        return "dojo episode reset: refused, a director job is still running (dojo status)";
    std::uint64_t const now = Now();
    // The fence comes first: from here on nothing of the old episode reaches an actor's perception or executes.
    ++_generation;
    _token = ++_tokens;
    _reason = reason.empty() ? "requested" : reason;
    Observer::Instance().Hold("reset to generation " + std::to_string(_generation));
    Control::Instance().BeginGeneration(_generation, "reset to generation " + std::to_string(_generation));
    _resetBeganMs = now;
    _phaseBeganMs = now;
    _gatesOpenMs = 0;
    _startMs = 0;
    _readyMs = 0;
    _endedMs = 0;
    _decision = 0;
    _clean = json::object();
    _initial = json::array();
    _timings = json::object();
    ++_resets;
    std::string const refused = Director::Instance().StartReset(_token);
    if (!refused.empty())
    {
        Fail(refused);
        return refused;
    }
    _phase = Phase::Teardown;
    _result = "resetting";
    Control::Instance().Note(0, "episode", "reset", "began", "generation " + std::to_string(_generation) + ", token "
        + std::to_string(_token) + ": " + _reason);
    LOG_INFO("module.dojo", "mod-dojo: episode reset to generation {} began (token {}): {}", _generation, _token, _reason);
    return "dojo-episode-reset " + CanonicalJson(json{{"generation", _generation}, {"token", _token},
        {"phase", PhaseName(_phase)}, {"began_ms", now}});
}

std::string Episode::Abort(std::string const& reason)
{
    if (_phase == Phase::Idle || _phase == Phase::Ended || _phase == Phase::Aborted || _phase == Phase::Failed)
        return "dojo episode abort: nothing is running (" + std::string(PhaseName(_phase)) + ")";
    if (_phase == Phase::Teardown)
        _abandonedToken = _token;   // the director's job finishes on its own; its completion is then stale
    Stop(Phase::Aborted, "aborted: " + (reason.empty() ? std::string("requested") : reason));
    return "dojo episode abort: generation " + std::to_string(_generation) + " " + _result;
}

void Episode::Fail(std::string const& why)
{
    Stop(Phase::Failed, "reset FAILED: " + why);
}

void Episode::Stop(Phase phase, std::string const& why)
{
    _phase = phase;
    _result = why;
    _endedMs = Now();
    if (phase == Phase::Failed)
        ++_failures;
    else if (phase == Phase::Aborted)
        ++_aborts;
    Observer::Instance().Hold(why);
    // Same generation, but nothing pending survives: a command queued before this point can no longer execute.
    Control::Instance().BeginGeneration(_generation, "episode " + std::to_string(_generation) + " " + PhaseName(phase));
    Control::Instance().Note(0, "episode", "episode", PhaseName(phase), "generation " + std::to_string(_generation) + ": " + why);
    if (phase == Phase::Failed)
        LOG_ERROR("module.dojo", "mod-dojo: episode {} {}", _generation, why);
    else
        LOG_INFO("module.dojo", "mod-dojo: episode {} {}: {}", _generation, PhaseName(phase), why);
}

void Episode::Update(std::uint32_t /*diff*/)
{
    if (!State().scenarioReady || !Observer::Instance().Ready())
        return;
    std::uint64_t const now = Now();
    Observer::Instance().Capture(now);
    Director& director = Director::Instance();
    Control& control = Control::Instance();
    Manifest const& m = *State().manifest;

    if (_abandonedToken && !director.JobRunning() && director.ResetToken() == _abandonedToken)
    {
        ++_staleCompletions;
        control.Note(0, "episode", "reset", "ignored", "token " + std::to_string(_abandonedToken)
            + " completed after its episode was abandoned: " + director.LastResult());
        _abandonedToken = 0;
    }

    switch (_phase)
    {
        case Phase::Teardown:
        {
            if (director.JobRunning())
                break;
            if (director.ResetToken() != _token || !director.LastJobOk() || !director.ArenaInstance())
            {
                Fail("the director's reset: " + director.LastResult());
                break;
            }
            _timings["reconstructed_ms"] = now - _resetBeganMs;
            _phase = Phase::Gates;
            _phaseBeganMs = now;
            break;
        }
        case Phase::Gates:
        {
            Battleground* bg = director.ArenaBattleground();
            if (!bg)
            {
                Fail("the arena closed before its gates opened");
                break;
            }
            for (std::size_t i = 0; i < kScenarioActors; ++i)
                if (!control.Actor(i) || !control.Owns(i))
                {
                    Fail("actor " + std::to_string(i) + " went offline or lost ownership before the gates opened");
                    return;
                }
            if (bg->GetStatus() == STATUS_IN_PROGRESS)
            {
                if (!_gatesOpenMs)
                    _gatesOpenMs = now;
                if (now - _gatesOpenMs < kGateSettleMs)
                    break;
                std::vector<std::string> problems;
                if (!VerifyClean(problems))
                {
                    Fail("not clean: " + Join(problems));
                    break;
                }
                _timings["gates_open_ms"] = _gatesOpenMs - _resetBeganMs;
                _startMs = now;
                Observer::Instance().Begin(_generation, _startMs);
                _phase = Phase::Settle;
                _phaseBeganMs = now;
            }
            else if (now - _phaseBeganMs > std::uint64_t(m.rules.prepSeconds) * 1000 + kGateSlackMs)
                Fail("the gates did not open within the preparation time");
            break;
        }
        case Phase::Settle:
        {
            std::array<std::optional<ActorObservation>, kScenarioActors> seen;
            std::string why;
            for (std::size_t i = 0; i < kScenarioActors && Observer::Instance().Active(); ++i)
                seen[i] = Observer::Instance().Observe(i, now, why);
            if (!seen[0] || !seen[1])
            {
                if (!Observer::Instance().Fault().empty())
                    Fail("perception: " + Observer::Instance().Fault());
                else if (now - _phaseBeganMs > kSettleLimitMs)
                    Fail("no first observation within " + std::to_string(kSettleLimitMs) + " ms (" + why + ")");
                break;
            }
            // Nothing may have moved while the first percepts matured.
            std::vector<std::string> problems;
            for (std::size_t i = 0; i < kScenarioActors; ++i)
            {
                Player* p = control.Actor(i);
                Point const& start = m.actors[i].start;
                if (!p || !p->IsAlive() || p->GetExactDist(float(start.x), float(start.y), float(start.z)) > kStartTolerance)
                    problems.push_back(m.actors[i].name + " is not alive at its start");
            }
            if (!problems.empty())
            {
                Fail(Join(problems));
                break;
            }
            _initial = json::array();
            for (std::size_t i = 0; i < kScenarioActors; ++i)
            {
                _initial.push_back(ObservationJson(i, *seen[i], true));
                _epochs[i] = control.Epoch(i);
            }
            _readyMs = now;
            _timings["ready_ms"] = now - _resetBeganMs;
            _phase = Phase::Running;
            ++_ready;
            _result = "ready";
            control.Note(0, "episode", "reset", "ready", "generation " + std::to_string(_generation) + " ready in "
                + std::to_string(now - _resetBeganMs) + " ms");
            LOG_INFO("module.dojo", "mod-dojo: episode {} ready (token {}) in {} ms, gates open after {} ms", _generation,
                _token, now - _resetBeganMs, _gatesOpenMs - _resetBeganMs);
            break;
        }
        case Phase::Running:
        {
            for (std::size_t i = 0; i < kScenarioActors; ++i)
                if (!control.Actor(i) || !control.Owns(i) || control.Epoch(i) != _epochs[i])
                {
                    Stop(Phase::Aborted, "aborted: actor " + std::to_string(i) + " disconnected or lost ownership");
                    return;
                }
            Battleground* bg = director.ArenaBattleground();
            if (!bg || bg->GetStatus() != STATUS_IN_PROGRESS)
            {
                Stop(Phase::Aborted, "aborted: the core ended the arena");
                break;
            }
            if (!Observer::Instance().Active())
            {
                Stop(Phase::Aborted, "aborted: perception stopped (" + Observer::Instance().Fault() + ")");
                break;
            }
            if (now - _startMs >= std::uint64_t(m.rules.matchSeconds) * 1000)
                Stop(Phase::Ended, "time limit");
            break;
        }
        default:
            break;
    }
}

json Episode::CleanState(std::size_t slot, Player* p, std::vector<std::string>& problems) const
{
    Manifest const& m = *State().manifest;
    ActorSpec const& spec = m.actors[slot];
    Control& control = Control::Instance();
    auto need = [&](bool ok, std::string const& what) {
        if (!ok)
            problems.push_back(spec.name + ": " + what);
    };
    std::optional<Observation> const o = Director::Instance().Inspect(slot);
    need(o.has_value(), "cannot be inspected");
    if (o)
        for (std::string const& d : CheckLoadout(m.loadout, *o))
            problems.push_back(spec.name + ": " + d);

    std::vector<std::uint32_t> auras, transient;
    for (auto const& [spellId, application] : p->GetAppliedAuras())
    {
        if (IsTeamMarker(spellId))
            continue;
        auras.push_back(spellId);
        // What a fight leaves behind has a duration; the arena's own zone aura (Arena - Dampening, spell_area) and
        // the passives are permanent. The whole list is compared with the reference reset as well.
        if (!application->GetBase()->GetSpellInfo()->IsPassive() && !application->GetBase()->IsPermanent())
            transient.push_back(spellId);
    }
    std::sort(auras.begin(), auras.end());
    auras.erase(std::unique(auras.begin(), auras.end()), auras.end());
    need(transient.empty(), std::to_string(transient.size()) + " transient auras");
    json cooldowns = json::array();
    for (auto const& [spellId, cooldown] : p->GetSpellCooldownMap())
        cooldowns.push_back(spellId);
    need(cooldowns.empty(), "spells on cooldown");
    bool gcd = false;
    for (KitAbility const& k : control.Catalog().kit)
        if (SpellInfo const* info = sSpellMgr->GetSpellInfo(k.spellId))
            gcd = gcd || p->GetGlobalCooldownMgr().HasGlobalCooldown(info);
    need(!gcd, "on the global cooldown");
    json dr = json::array();
    for (int g = DIMINISHING_BANISH; g <= DIMINISHING_DRAGONS_BREATH; ++g)
        if (p->GetDiminishing(DiminishingGroup(g)) != DIMINISHING_LEVEL_1)
            dr.push_back(g);
    need(dr.empty(), "diminishing returns carried over");
    bool const combat = p->IsInCombat() || p->GetCombatManager().HasCombat();
    need(!combat, "in combat");
    need(!p->GetVictim(), "auto-attacking");
    need(p->GetTarget().IsEmpty(), "has a selection");
    bool const moving = !p->movespline->Finalized() || p->GetMotionMaster()->GetCurrentMovementGeneratorType() != IDLE_MOTION_TYPE;
    need(!moving, "moving");
    json spells = json::array();
    for (std::uint32_t type = 0; type < CURRENT_MAX_SPELL; ++type)
        if (Spell const* s = p->GetCurrentSpell(CurrentSpellTypes(type)))
            spells.push_back(s->GetSpellInfo()->Id);
    need(spells.empty(), "a spell in progress");
    need(p->SpellQueue.empty(), "a request in the spell queue");
    bool const pet = p->GetPet() || p->GetGuardianPet();
    need(!pet, "has a pet or guardian");
    need(!control.PendingFor(slot), "a pending command");
    need(control.Owns(slot) && control.Epoch(slot) > _epochs[slot], "not freshly owned (epoch "
        + std::to_string(control.Epoch(slot)) + " after " + std::to_string(_epochs[slot]) + ")");
    float const away = p->GetExactDist(float(spec.start.x), float(spec.start.y), float(spec.start.z));
    need(away <= kStartTolerance, "not at its start");
    need(AngleBetween(p->GetOrientation(), float(spec.start.o)) <= kFacingTolerance, "not facing its start orientation");
    Battleground* bg = Director::Instance().ArenaBattleground();
    TeamId const side = spec.team == "alliance" ? TEAM_ALLIANCE : TEAM_HORDE;
    need(bg && p->GetBattleground() == bg && p->GetBgTeamId() == side, "not in the training arena on its side");
    return {{"loadout_fingerprint", o ? Fingerprint(*o) : ""}, {"auras", auras}, {"transient_auras", transient},
        {"cooldowns", cooldowns}, {"gcd", gcd}, {"diminishing", dr}, {"combat", combat}, {"victim", p->GetVictim() != nullptr},
        {"selection", !p->GetTarget().IsEmpty()}, {"moving", moving}, {"spells", spells}, {"spell_queue", p->SpellQueue.size()},
        {"events", p->m_Events.HasEvents()}, {"pet", pet}, {"pending", control.PendingFor(slot).has_value()},
        {"alive", p->IsAlive()}, {"health", p->GetHealth()}, {"max_health", p->GetMaxHealth()},
        {"mana", p->GetPower(POWER_MANA)}, {"max_mana", p->GetMaxPower(POWER_MANA)},
        {"position_cm", {std::llround(double(p->GetPositionX()) * 100.0), std::llround(double(p->GetPositionY()) * 100.0),
            std::llround(double(p->GetPositionZ()) * 100.0)}},
        {"orientation_mrad", std::llround(double(p->GetOrientation()) * 1000.0)},
        {"start_distance_cm", std::llround(double(away) * 100.0)}, {"team", spec.team}};
}

bool Episode::VerifyClean(std::vector<std::string>& problems)
{
    Battleground* bg = Director::Instance().ArenaBattleground();
    if (!bg || bg->GetStatus() != STATUS_IN_PROGRESS || bg->isRated() || bg->GetPlayersSize() != kScenarioActors)
        problems.push_back("the arena is not an unrated one in progress with both actors in it");
    Control& control = Control::Instance();
    json actors = json::array();
    std::array<Player*, kScenarioActors> players{};
    for (std::size_t i = 0; i < kScenarioActors; ++i)
    {
        players[i] = control.Actor(i);
        if (!players[i] || !players[i]->IsInWorld())
        {
            problems.push_back("actor " + std::to_string(i) + " is not in the world");
            actors.push_back(nullptr);
            continue;
        }
        actors.push_back(CleanState(i, players[i], problems));
    }
    json sees = json::array();
    for (std::size_t i = 0; i < kScenarioActors; ++i)
        sees.push_back(players[0] && players[1] && Observer::Instance().Sees(players[i], players[1 - i]));
    _clean = {{"actors", actors}, {"sees", sees},
        {"arena", bg ? json{{"map", bg->GetMapId()}, {"status", int(bg->GetStatus())}, {"rated", bg->isRated()},
            {"players", bg->GetPlayersSize()}, {"arena_type", bg->GetArenaType()}} : json(nullptr)},
        {"problems", problems}};
    return problems.empty();
}

json Episode::ObservationJson(std::size_t slot, ActorObservation const& o, bool full) const
{
    MaskResult const mask = ComputeMask(Control::Instance().Catalog(), o.knowledge, Observer::Instance().Rules());
    json values = o.values;
    std::string const canonical = CanonicalJson(values);
    json doc = {{"actor", slot}, {"generation", o.generation}, {"decision_ms", o.decisionMs}, {"percept_ms", o.perceptMs},
        {"age_ms", o.decisionMs - o.perceptMs}, {"start_offset_ms", o.perceptMs >= _startMs ? o.perceptMs - _startMs : 0},
        {"enemy_present", o.enemyPresent}, {"enemy_visible", o.enemyVisible},
        {"open", std::count(mask.mask.begin(), mask.mask.end(), std::uint8_t(1))}, {"values_sha256", Sha256Hex(canonical)}};
    if (full)
    {
        doc["values"] = values;
        doc["mask"] = Bits(mask.mask);
    }
    return doc;
}

std::string Episode::Observe(bool full)
{
    std::uint64_t const now = Now();
    std::string out;
    for (std::size_t i = 0; i < kScenarioActors; ++i)
    {
        std::string why;
        std::optional<ActorObservation> const o = Observer::Instance().Observe(i, now, why);
        if (!o)
            out += "dojo episode observe: actor " + std::to_string(i) + " has no observation (" + why + ")\n";
        else
            out += "dojo-observation " + CanonicalJson(ObservationJson(i, *o, full)) + "\n";
    }
    return out + "dojo episode observe: generation " + std::to_string(_generation) + " " + PhaseName(_phase) + " at "
        + std::to_string(now);
}

std::string Episode::Decide(std::vector<std::string> const& actions, bool force)
{
    if (_phase != Phase::Running)
        return "dojo episode decide: refused, the episode is " + std::string(PhaseName(_phase));
    Control& control = Control::Instance();
    ActionCatalog const& catalog = control.Catalog();
    if (actions.size() != kScenarioActors)
        return "dojo episode decide: one action per actor, actor 0 first";
    std::uint64_t const now = Now();
    std::array<std::optional<ActorObservation>, kScenarioActors> seen;
    std::array<std::size_t, kScenarioActors> chosen{};
    json observations = json::array();
    for (std::size_t i = 0; i < kScenarioActors; ++i)
    {
        std::string why;
        seen[i] = Observer::Instance().Observe(i, now, why);
        if (!seen[i])
            return "dojo episode decide: refused, actor " + std::to_string(i) + " has no observation (" + why + ")";
        std::optional<std::size_t> index = FindAction(catalog, actions[i]);
        if (!index && !actions[i].empty() && actions[i].size() < 6
            && std::all_of(actions[i].begin(), actions[i].end(), [](char c) { return c >= '0' && c <= '9'; })
            && std::stoul(actions[i]) < catalog.entries.size())
            index = std::stoul(actions[i]);
        if (!index)
            return "dojo episode decide: refused, unknown action " + actions[i];
        MaskResult const mask = ComputeMask(catalog, seen[i]->knowledge, Observer::Instance().Rules());
        if (!mask.mask[*index] && !force)
            return "dojo episode decide: refused, " + actions[i] + " is masked for actor " + std::to_string(i) + " ("
                + mask.abilityReasons[catalog.entries[*index].ability] + ")";
        chosen[i] = *index;
        observations.push_back(ObservationJson(i, *seen[i], true));
    }
    // Both commands answer the same boundary's observations (D14): neither is a reply to the other.
    std::uint64_t const decision = _decision++;
    json requested = json::array();
    for (std::size_t i = 0; i < kScenarioActors; ++i)
    {
        std::string error;
        std::uint64_t const actionId = control.Enqueue(i, chosen[i], force ? "episode (forced)" : "episode",
            "decision:" + std::to_string(_generation) + ":" + std::to_string(decision), error);
        requested.push_back({{"actor", i}, {"action", chosen[i]}, {"name", catalog.entries[chosen[i]].name},
            {"action_id", actionId}, {"error", error}});
    }
    return "dojo-decision " + CanonicalJson(json{{"generation", _generation}, {"decision", decision},
        {"decision_ms", now}, {"elapsed_ms", now - _startMs}, {"requested", requested}, {"observations", observations}});
}

std::string Episode::Schema() const
{
    Observer const& o = Observer::Instance();
    if (!o.Ready())
        return "dojo episode schema: not ready";
    return "dojo-schema " + o.Schema().canonical + "\ndojo-context " + CanonicalJson(o.ContextJson());
}

std::string Episode::Debug(std::size_t last) const
{
    std::string out;
    for (json const& record : Observer::Instance().DebugJson(last))
        out += "dojo-debug " + CanonicalJson(record) + "\n";
    return out + "dojo episode debug: authoritative state, never part of an actor's observation";
}

std::string Episode::Fault(std::string const& point)
{
    if (Busy())
        return "dojo episode fault: refused while a reset is in flight";
    return Director::Instance().ArmFault(point);
}

std::string Episode::Status() const
{
    std::uint64_t const now = Now();
    Manifest const* m = State().manifest ? &*State().manifest : nullptr;
    std::uint64_t const limit = m ? std::uint64_t(m->rules.matchSeconds) * 1000 : 0;
    std::uint64_t const elapsed = _phase == Phase::Running && _startMs ? now - _startMs : 0;
    json doc = {{"phase", PhaseName(_phase)}, {"generation", _generation}, {"token", _token}, {"reason", _reason},
        {"result", _result}, {"now_ms", now}, {"reset_began_ms", _resetBeganMs}, {"start_ms", _startMs},
        {"ready_ms", _readyMs}, {"ended_ms", _endedMs}, {"elapsed_ms", elapsed},
        {"remaining_ms", limit > elapsed ? limit - elapsed : 0}, {"decision", _decision}, {"timings", _timings},
        {"counts", {{"resets", _resets}, {"ready", _ready}, {"failures", _failures}, {"aborts", _aborts},
            {"stale_completions", _staleCompletions}}},
        {"director", {{"running", Director::Instance().JobRunning()}, {"token", Director::Instance().ResetToken()},
            {"result", Director::Instance().LastResult()}, {"arena_instance", Director::Instance().ArenaInstance()}}},
        {"observer", Observer::Instance().StatsJson()}, {"trace_seq", Control::Instance().TraceSeq()},
        {"first_action_id", Control::Instance().FirstActionId()}};
    return "dojo-episode " + CanonicalJson(doc);
}

std::string Episode::Evidence() const
{
    return "dojo-reset-evidence " + CanonicalJson(json{{"generation", _generation}, {"token", _token},
        {"phase", PhaseName(_phase)}, {"result", _result}, {"timings", _timings}, {"start_ms", _startMs},
        {"ready_ms", _readyMs}, {"clean", _clean}, {"initial", _initial}});
}
}

#endif
