// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_DOJO_DOJO_RESET_H
#define MOD_DOJO_DOJO_RESET_H

#include "DojoManifest.h"
#include "DojoPerception.h"

#include <array>
#include <cstdint>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

class Player;

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
// arena aborts it: an aborted episode is not a game result. The scenario's match limit ends it (a time limit, not an
// outcome: Task 8 scores episodes). The stock win check stays off for the dojo's arena throughout (Task 5 ruling 2).
namespace Dojo
{
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

    // True while a reset is in flight: the director's own console jobs are refused then.
    bool Busy() const;

    void Update(std::uint32_t diff);

private:
    enum class Phase { Idle, Teardown, Gates, Settle, Running, Ended, Aborted, Failed };

    static char const* PhaseName(Phase phase);
    void Fail(std::string const& why);
    void Stop(Phase phase, std::string const& why);
    bool VerifyClean(std::vector<std::string>& problems);
    nlohmann::json CleanState(std::size_t slot, Player* player, std::vector<std::string>& problems) const;
    nlohmann::json ObservationJson(std::size_t slot, ActorObservation const& o, bool full) const;

    Phase _phase = Phase::Idle;
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
    std::uint64_t _resets = 0, _ready = 0, _failures = 0, _aborts = 0, _staleCompletions = 0;
    std::uint64_t _abandonedToken = 0;
    nlohmann::json _clean;
    nlohmann::json _initial;
    nlohmann::json _timings;
};
}

#endif
