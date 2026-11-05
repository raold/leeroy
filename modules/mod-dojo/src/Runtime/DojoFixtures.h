// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_DOJO_DOJO_FIXTURES_H
#define MOD_DOJO_DOJO_FIXTURES_H

#include "DojoManifest.h"

#include <array>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

class Battleground;
class Player;

// The scenario's two synthetic fixture actors and the training-only arena they meet in. Training build only, world
// thread only: commands start a job and return; Update advances it one tick at a time and every wait has a deadline.
//
// A logged-in fixture is owned by the external controller (DojoExecutor.h) from its AI's first tick: stock playerbots
// casts nothing, moves nowhere and reacts to nothing, while teleport acknowledgement and session housekeeping
// continue. Every untriggered cast by a fixture that no executor command produced is counted as a stock cast; an
// owned actor's count stays 0.
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
    // Logs the fixtures in, moves them to the staging point, restores the staging baseline (no transient aura or
    // cooldown, full health, mana and durability; items, talents and spells untouched), saves and logs out.
    std::string StartRestore();
    // Logs the fixtures in, verifies their loadout, opens a training arena and sends one actor to each side.
    std::string StartOpen();
    // Takes both actors out of the arena (no outcome is recorded), logs them out and lets the arena close.
    std::string StartClose();
    // The episode's reset (DojoReset.h): tears down whatever is there (out of the arena, the dead revived, transient
    // state restored, both logged out, the old instance gone), then reconstructs: fresh logins, the staging baseline
    // restored and the loadout verified, a new training arena, one actor per side. Done once both stand in the new
    // instance, before its gates open; the episode checks the rest.
    std::string StartReset(std::uint64_t token);
    // A one-shot fault for the next reset's teardown: "leave" gives the actors no time to leave the arena, so they are
    // logged out where they stand (the reset must still recover); "arena" gives the old instance no time to close, so
    // the reset fails and the next one must clean up.
    std::string ArmFault(std::string const& point);
    // The harness's disconnect: the actor's session ends, as when a client's connection drops.
    std::string Disconnect(std::size_t slot);
    // Progress and the last result, then one "dojo-evidence {json}" line with the last verification.
    std::string Status() const;

    bool JobRunning() const { return _job != Job::None; }
    bool LastJobOk() const { return _jobOk; }
    std::string const& LastResult() const { return _result; }
    std::uint64_t ResetToken() const { return _resetToken; }
    std::uint32_t ArenaInstance() const { return _arenaInstance; }
    Battleground* ArenaBattleground() const { return Arena(); }
    // What a fixture actor is now (its loadout observation), for the episode's clean-state check.
    std::optional<Observation> Inspect(std::size_t slot) const;

    void Update(std::uint32_t diff);

    // True for the arena this director opened, for as long as it exists: its stock win conditions stay unchecked, so
    // leaving, logging out or dying there ends no match and records no outcome. The dojo decides outcomes itself.
    bool SuppressesWinCheck(Battleground const* bg) const;
    void OnFixtureCast(Player const* player, std::uint32_t spellId, bool triggered, bool executor);

private:
    enum class Job { None, Build, Restore, Open, Close, Reset };
    enum class Step { Idle, Login, Stage, Apply, Verify, Send, Arrive, InspectArena, Leave, Logout, ArenaGone, Done };

    struct Actor
    {
        std::uint64_t guid = 0;
        // Verified state at the staging point, restored after leaving the arena: entering and leaving during
        // preparation leaves Arena Preparation, Honorless Target and the raid leader flag behind otherwise.
        std::vector<std::uint32_t> baselineAuras;
        bool baselineLeader = false;
        bool baselineRecorded = false;
    };

    bool Resolve(std::string& error);
    Player* Find(std::size_t index) const;
    bool AllOnline() const;
    void Begin(Job job, Step first);
    void Advance(Step next);
    void Finish(bool ok, std::string const& message);
    void Fail(std::string const& message);
    bool StepTimedOut(std::uint32_t limitMs);
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
    std::uint64_t _resetToken = 0;
    bool _resetOpened = false;          // the reset's reconstruction opened its arena: a failure closes it
    std::optional<Step> _fault;         // armed for the next reset's teardown
    std::string _faultName;
    bool _faultUsed = false;
    std::uint32_t _stockCasts = 0;
    std::uint32_t _executorCasts = 0;
    std::uint32_t _triggeredCasts = 0;
    std::string _evidence;
    std::vector<std::string> _log;
};
}

#endif
