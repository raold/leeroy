// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_DOJO_DOJO_OBSERVATION_H
#define MOD_DOJO_DOJO_OBSERVATION_H

#include "DojoActionCatalog.h"
#include "DojoManifest.h"
#include "DojoPerception.h"

#include <array>
#include <cstdint>
#include <deque>
#include <optional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

class Player;

// The fixture actors' senses (plan Task 7, D18-D19, D30). Training build only, world thread only.
//
// Once per world tick, after the maps have updated (MapUpdate.Threads = 0, and the dojo's tick runs after the map,
// battleground and session updates), the observer reads both actors' authoritative state into an ActorTruth each,
// keeps the pair as that tick's debug record, and pushes each actor the percept its own client could have: itself, the
// enemy only while visible (in the map, detectable and in line of sight from eye height), and the cast starts, casts
// and interruptions its session was told of whose caster it could see then. The reads call only getters.
//
// Debug records and actor percepts are separate types in separate buffers and leave the worker on separate console
// lines: nothing hidden is ever written into an actor's observation. Each actor's Perception (DojoPerception.h) holds
// one episode's percepts and memory and is cleared at every episode boundary, when an actor leaves the world or
// changes map, instance or ownership epoch, and whenever the episode asks for a hold (a reset in progress).
namespace Dojo
{
struct DebugRecord
{
    std::uint64_t atMs = 0;
    std::uint64_t generation = 0;
    std::array<std::optional<ActorTruth>, kScenarioActors> truth;
    std::array<bool, kScenarioActors> sees{};   // sees[i]: actor i could see the other
    float distance = 0;
};

class Observer
{
public:
    static Observer& Instance();

    // The schema, context and kit cooldowns for the scenario. Empty on success.
    std::string Prepare(Manifest const& manifest, ActionCatalog const& catalog);
    bool Ready() const { return _schema.has_value(); }
    ObservationSchema const& Schema() const { return *_schema; }
    PerceptionConfig const& Config() const { return _config; }
    nlohmann::json ContextJson() const;

    // An episode boundary: every percept, memory, event and debug record goes; capture restarts under generation.
    void Begin(std::uint64_t generation, std::uint64_t startMs);
    // No capture until the next Begin (a reset in progress).
    void Hold(std::string const& why);
    bool Holding() const { return _hold; }
    bool Active() const { return _active; }
    std::uint64_t Generation() const { return _generation; }
    std::uint64_t StartMs() const { return _startMs; }
    std::string const& Fault() const { return _fault; }

    void Capture(std::uint64_t nowMs);

    // From the packet hook: observer's session was told that source cast, completed or stopped spellId.
    void OnSessionEvent(std::size_t observer, std::size_t source, EventKind kind, std::uint32_t spellId, std::uint64_t nowMs);

    // The actor's observation at nowMs (its delayed percept and memory), or nullopt with why.
    std::optional<ActorObservation> Observe(std::size_t slot, std::uint64_t nowMs, std::string& why);
    MaskRules Rules() const;

    // n reads of both actors' state, percepts and masks from a copy of the actor's perception: proof that reading
    // changes nothing (dojo control maskcheck fingerprints both actors around it). The masks of every repetition.
    std::vector<std::vector<std::uint8_t>> Probe(std::size_t slot, std::uint64_t nowMs, std::uint32_t repeats);

    nlohmann::json DebugJson(std::size_t last) const;
    nlohmann::json StatsJson() const;

    // The authoritative state, for the reset's verification and the debug records.
    ActorTruth Read(Player* self, std::size_t slot) const;
    bool Sees(Player* self, Player* enemy) const;

private:
    int KitIndex(std::uint32_t spellId) const;
    void Deactivate(std::string const& why);
    void Activate(std::uint64_t nowMs);

    std::optional<ObservationSchema> _schema;
    PerceptionConfig _config;
    std::vector<std::uint32_t> _kitSpells;
    std::array<std::optional<Perception>, kScenarioActors> _perception;
    std::array<std::vector<PerceivedEvent>, kScenarioActors> _inbox;
    std::array<bool, kScenarioActors> _wasAlive{};
    std::array<std::string, kScenarioActors> _key;   // guid, epoch, map and instance the perception belongs to
    std::deque<DebugRecord> _debug;
    std::uint64_t _generation = 0;
    std::uint64_t _startMs = 0;
    bool _hold = false;
    bool _active = false;
    bool _startFixed = false;   // Begin set the start: the next activation keeps it
    std::string _fault;
    std::string _inactiveWhy = "not started";
    std::uint64_t _captures = 0;
    std::uint64_t _hiddenEvents = 0;
    std::uint64_t _seenEvents = 0;
    std::uint64_t _restarts = 0;
};
}

#endif
