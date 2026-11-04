// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_DOJO_DOJO_FIXTURES_H
#define MOD_DOJO_DOJO_FIXTURES_H

#include "DojoManifest.h"

#include <array>
#include <cstdint>
#include <string>
#include <vector>

class Battleground;
class Player;

// The scenario's two synthetic fixture actors and the training-only arena they meet in. Training build only, world
// thread only: commands start a job and return; Update advances it one tick at a time and every wait has a deadline.
//
// Until the external-controller hook arrives (plan Task 6) a logged-in fixture is held dormant: its playerbots
// engines carry no strategy and its next AI check stays a minute away, so stock playerbots casts nothing, moves
// nowhere and reacts to nothing. Teleport acknowledgement and session housekeeping run outside that check and
// continue. Every untriggered cast by a fixture is counted; a dormant actor's count stays 0.
namespace Dojo
{
class Director
{
public:
    static Director& Instance();

    // Creates the fixture characters the manifest names that do not exist yet, on its fixture account.
    std::string Create();
    // Logs the fixtures in, moves them to the staging point, applies the manifest's loadout, saves and logs out.
    std::string StartBuild();
    // Logs the fixtures in, verifies their loadout, opens a training arena and sends one actor to each side.
    std::string StartOpen();
    // Takes both actors out of the arena (no outcome is recorded), logs them out and lets the arena close.
    std::string StartClose();
    // Progress and the last result, then one "dojo-evidence {json}" line with the last verification.
    std::string Status() const;

    void Update(std::uint32_t diff);

    // True for the arena this director opened while it tears it down: the arena's win conditions stay unchecked, so
    // leaving during preparation ends nothing and records nothing.
    bool SuppressesWinCheck(Battleground const* bg) const;
    void OnFixtureCast(Player const* player, std::uint32_t spellId, bool triggered);

private:
    enum class Job { None, Build, Open, Close };
    enum class Step { Idle, Login, Stage, Apply, Verify, Send, Arrive, InspectArena, Leave, Logout, ArenaGone, Done };

    struct Actor
    {
        std::uint64_t guid = 0;
        bool held = false;
        // Verified state at the staging point, restored after leaving the arena: entering and leaving during
        // preparation leaves Arena Preparation, Honorless Target and the raid leader flag behind otherwise.
        std::vector<std::uint32_t> baselineAuras;
        bool baselineLeader = false;
        bool baselineRecorded = false;
    };

    bool Resolve(std::string& error);
    Player* Find(std::size_t index) const;
    bool AllOnline() const;
    void Hold();
    void Begin(Job job, Step first);
    void Advance(Step next);
    void Finish(bool ok, std::string const& message);
    void Fail(std::string const& message);
    bool StepTimedOut(std::uint32_t limitMs) const;
    void LogoutAll();
    void RecordBaseline(std::size_t index, Player* player);
    std::uint32_t RestoreBaseline(std::size_t index, Player* player);
    bool ApplyLoadout(Player* player, std::string& error);
    Observation Observe(Player* player) const;
    Battleground* Arena() const;
    bool OpenArena(std::string& error);
    bool InspectArena(std::string& report);

    Job _job = Job::None;
    Step _step = Step::Idle;
    std::uint32_t _stepMs = 0;
    bool _entered = false;
    bool _jobOk = false;
    std::string _failure;
    std::string _jobName;
    std::string _result = "no job run yet";
    std::array<Actor, kScenarioActors> _actors{};
    std::uint32_t _arenaInstance = 0;
    std::uint32_t _arenaType = 0;
    bool _closing = false;
    std::uint32_t _untriggeredCasts = 0;
    std::uint32_t _triggeredCasts = 0;
    std::string _evidence;
    std::vector<std::string> _log;
};
}

#endif
