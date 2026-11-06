// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_DOJO_DOJO_RESET_H
#define MOD_DOJO_DOJO_RESET_H

#include "DojoManifest.h"
#include "DojoOutcome.h"
#include "DojoPerception.h"

#include <array>
#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

class Player;
class SpellInfo;
class Unit;

// Episodes and their reset (plan Task 7, D04-D05). Training build only, world thread only.
//
// A reset starts a new generation and returns at once; it completes over later ticks. At its start every pending
// command and in-flight cast record of the old generation is dropped and the actors' perception is held, so nothing
// of the old episode reaches the new one. Then the director tears down whatever is there (out of the arena, the dead
// revived, transient state restored, logged out, the old instance closed) and reconstructs it: fresh logins (new
// Player and PlayerbotAI objects, so no aura, diminishing return, threat, combat, spell event, motion or AI cache
// survives), the staging baseline restored and verified against the manifest, a new arena instance with one actor per
// side. The episode waits for the gates, verifies the clean state of both actors, starts their perception at the
// episode's start, and becomes ready only once each actor has an observation made under the perceptual delay and
// both still stand at their start. Until then nothing reports the reset complete; a failure at any step leaves the
// episode failed (not ready), and the next reset starts from whatever is there.
//
// While an episode runs, an actor that goes offline or loses ownership, a perception fault or the core ending the
// arena aborts it: an aborted episode is not a game result. Otherwise the dojo decides its outcome each tick from both
// actors (DojoOutcome.h, plan Task 8): a death, both deaths on one tick, or the scenario's match limit ends it. The
// stock win check stays off for the dojo's arena throughout (Task 5 ruling 2), so the core records no winner; its
// scoreboard (damage done, deaths, killing blows) still counts and is kept beside the dojo's own tallies of the same
// episode for reconciliation.
//
// An episode has a mode: console (dojo episode reset and decide), protocol (the endpoint's reset and steps,
// DojoEndpoint.h; the console may then not reset or decide it) or stock, the native playerbots system baseline: both
// actors handed back to stock behaviour for the rest of the episode (D25), owned again when it ends.
namespace Dojo
{
// {reason, winner} of an ended episode, or null.
nlohmann::json OutcomeToJson(Outcome const& outcome);

class Episode
{
public:
    static Episode& Instance();

    // Console.
    std::string Reset(std::string const& reason);
    std::string Abort(std::string const& reason);
    std::string Status() const;
    std::string Evidence() const;
    std::string Observe(bool full);
    std::string Decide(std::vector<std::string> const& actions, bool force);
    std::string Schema() const;
    std::string Debug(std::size_t last) const;
    std::string Fault(std::string const& point);

    std::string Stock();
    std::string Score() const;

    // True while a reset is in flight: the director's own console jobs are refused then.
    bool Busy() const;

    // The endpoint's side (DojoEndpoint.h).
    bool BeginProtocolReset(std::string const& reason, std::string& error);
    std::string AbortFor(std::string const& reason);
    std::uint64_t Generation() const { return _generation; }
    bool Running() const { return _phase == Phase::Running; }
    bool Ended() const { return _phase == Phase::Ended; }
    bool Stopped() const { return _phase == Phase::Aborted || _phase == Phase::Failed; }
    bool Protocol() const { return _mode == Mode::Protocol; }
    std::string const& Result() const { return _result; }
    Outcome const& GetOutcome() const { return _outcome; }
    std::uint64_t StartMs() const { return _startMs; }
    std::vector<Standing> Standings() const;
    // Damage and death events of a protocol episode since the last call, oldest first.
    std::vector<nlohmann::json> TakeEvents();

    // Hooks: damage dealt (after absorbs, as the core's scoreboard counts it) and a completed cast.
    void OnDamage(Unit* attacker, Unit* victim, std::uint32_t damage);
    void OnCast(Unit* caster, SpellInfo const* info, bool triggered);

    void Update(std::uint32_t diff);

private:
    enum class Phase { Idle, Teardown, Gates, Settle, Running, Ended, Aborted, Failed };
    enum class Mode { Console, Protocol, Stock };

    struct Tally
    {
        std::uint64_t damageDone = 0, damageTaken = 0, hits = 0, deaths = 0;
        std::map<std::uint32_t, std::uint32_t> casts;            // completed, untriggered, by spell id
        std::map<std::uint32_t, std::uint32_t> triggered;
    };

    static char const* PhaseName(Phase phase);
    static char const* ModeName(Mode mode);
    std::string StartReset(std::string const& reason, Mode mode, bool& started);
    void Fail(std::string const& why);
    void Stop(Phase phase, std::string const& why);
    void End(Outcome const& outcome, std::uint64_t now);
    bool Counting() const { return _phase == Phase::Gates || _phase == Phase::Settle || _phase == Phase::Running; }
    std::optional<std::size_t> SlotOf(Unit const* unit) const;
    nlohmann::json ScoreJson() const;
    nlohmann::json TallyJson() const;
    bool VerifyClean(std::vector<std::string>& problems);
    nlohmann::json CleanState(std::size_t slot, Player* player, std::vector<std::string>& problems) const;
    nlohmann::json ObservationJson(std::size_t slot, ActorObservation const& o, bool full) const;

    Phase _phase = Phase::Idle;
    Mode _mode = Mode::Console;
    Outcome _outcome;
    std::array<bool, kScenarioActors> _alive{};
    std::array<Tally, kScenarioActors> _tally{};
    std::vector<nlohmann::json> _events;
    std::uint64_t _eventsDropped = 0;
    nlohmann::json _finalScore;
    nlohmann::json _finalTally;
    std::uint64_t _generation = 0;
    std::uint64_t _token = 0;
    std::uint64_t _tokens = 0;
    std::string _reason;
    std::string _result = "no reset yet";
    std::uint64_t _resetBeganMs = 0;
    std::uint64_t _phaseBeganMs = 0;
    std::uint64_t _gatesOpenMs = 0;
    std::uint64_t _startMs = 0;
    std::uint64_t _readyMs = 0;
    std::uint64_t _endedMs = 0;
    std::uint64_t _decision = 0;
    std::array<std::uint64_t, kScenarioActors> _epochs{};
    std::uint64_t _resets = 0, _ready = 0, _failures = 0, _aborts = 0, _staleCompletions = 0, _ended = 0;
    std::uint64_t _abandonedToken = 0;
    nlohmann::json _clean;
    nlohmann::json _initial;
    nlohmann::json _timings;
};
}

#endif
