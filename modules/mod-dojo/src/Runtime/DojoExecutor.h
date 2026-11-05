// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_DOJO_DOJO_EXECUTOR_H
#define MOD_DOJO_DOJO_EXECUTOR_H

#include "DojoActionCatalog.h"
#include "DojoActionQueue.h"
#include "DojoManifest.h"
#include "DojoPerception.h"

#include <array>
#include <cstdint>
#include <deque>
#include <optional>
#include <string>
#include <utility>
#include <vector>

class Player;
class Spell;
class WorldPacket;
class WorldSession;

// Exclusive control of the scenario's fixture actors and the narrow executor that carries out their commands (plan
// Task 6, D20-D23, D30). Training build only, world thread only (the training rules require MapUpdate.Threads = 0).
//
// Ownership goes through mod-playerbots' external-control interface (PlayerbotAI::AcquireExternalControl, the dojo
// patch series): only the manifest's fixture characters on the fixture account are registered, and only they are ever
// acquired. Each acquisition has an epoch; ownership ends with the bot's AI (logout, shutdown), and the controller
// notices by epoch on the next tick rather than holding any pointer across ticks. A fixture is owned whenever it is
// online unless the console handed it back.
//
// The executor plays the part of a client: casts, stop-cast, selection and auto-attack go through the core's own
// opcode handlers (WorldSession::HandleCastSpellOpcode and friends), so costs, cooldowns, the global cooldown, range,
// line of sight, facing, crowd control, immunity and the spell queue follow ordinary server rules, and their results
// come back as the packets a client would read. Facing and movement are ordinary movement splines at the actor's own
// speed, stopped by the first collision, refused while rooted, stunned or without control. Nothing else is castable:
// only the catalog's kit spells exist, and pets are unsupported (a pet found at acquisition is dismissed).
//
// Every action is traced with its producer: the executor (with the command's action id, input, due and execution
// times), the acquisition, the harness (acceptance placement), the core's own rules (a cast cancelled by movement, a
// lost target), or "stock" for anything no one here asked for, which is what a gate failure would look like. Records
// stay in a bounded buffer that the tooling drains into its run files (dojo control trace since <seq>); only
// ownership, generation and neutralize records, dropped or refused commands, changed fingerprints and stock actions
// also go to the server log (Task 7 ruling 5).
//
// Masks come from the actor's delayed observation (DojoObservation.h, Task 7 ruling 2), never from current state; the
// executor itself validates against actual state, so a choice the mask allowed can still be refused. Each episode is
// a generation: BeginGeneration drops every pending command and in-flight cast record of the last one, and a command
// with an action id from before it is refused if it ever reaches execution.
namespace Dojo
{
struct TraceRecord
{
    std::uint64_t seq = 0;
    std::uint64_t atMs = 0;
    std::size_t actor = 0;
    std::string producer;   // executor, acquire, release, harness, core, stock, queue, control
    std::string kind;       // command, cast, move, face, target, attack, ownership, mask, ...
    std::uint64_t actionId = 0;
    std::string origin;
    std::string action;
    std::uint64_t inputMs = 0;
    std::uint64_t dueMs = 0;
    std::uint64_t execMs = 0;
    std::string outcome;    // executed, rejected, superseded, kept, dropped, observed, ...
    std::string detail;
    std::uint64_t generation = 0;   // the episode generation the record was made in (set by Record)
};

class Control
{
public:
    static Control& Instance();

    // The catalog from the manifest's kit and the spell data. Empty on success.
    std::string Prepare(Manifest const& manifest);
    bool Ready() const { return _catalog.has_value(); }

    // The director registers the manifest's fixture characters once it has resolved them on the fixture account.
    void Register(std::size_t slot, std::uint64_t guid);
    bool IsRegistered(Player const* player) const;
    bool Owns(std::size_t slot) const;

    // Every world tick: ownership checks and acquisition, due commands, producer tracing.
    void Update();

    // The online fixture actor in a slot (any thread state the world thread sees), its ownership epoch and its own
    // pending command, as the observer reads them.
    Player* Actor(std::size_t slot) const { return Find(slot); }
    std::uint64_t Epoch(std::size_t slot) const { return slot < kScenarioActors ? _slots[slot].epoch : 0; }
    std::optional<PendingView> PendingFor(std::size_t slot) const;
    ActionCatalog const& Catalog() const { return *_catalog; }
    // The command delay and the core's spell queue window; the observer adds the perceptual delay.
    MaskRules ExecutionRules() const;

    // An episode boundary: pending commands and in-flight cast records of the last generation go, traced.
    void BeginGeneration(std::uint64_t generation, std::string const& why);
    std::uint64_t Generation() const { return _generation; }
    std::uint64_t FirstActionId() const { return _firstActionId; }
    std::uint64_t NextActionId() const { return _nextActionId; }

    // Queues a catalog action for an owned actor; the caller has checked the mask. 0 and error when refused.
    std::uint64_t Enqueue(std::size_t slot, std::size_t action, std::string const& producer, std::string const& origin,
        std::string& error);

    // Console.
    std::string Own(std::size_t slot);
    std::string Release(std::size_t slot);
    std::string Submit(std::size_t slot, std::vector<std::string> const& actions, bool force);
    std::string Masks(std::size_t slot, bool all);
    std::string MaskCheck(std::size_t slot, std::uint32_t repeats);
    std::string Status() const;
    std::string Trace(std::size_t last) const;
    std::string TraceSince(std::uint64_t seq, std::size_t max) const;
    // The records after seq still in the buffer, oldest first; lost is set when some were already dropped.
    std::vector<TraceRecord> RecordsSince(std::uint64_t seq, bool& lost) const;
    std::uint64_t TraceSeq() const { return _seq; }
    std::string CatalogText() const;
    std::string Place(std::size_t slot, float x, float y, float z, float o);
    std::string SetMana(std::size_t slot, std::uint32_t mana);
    std::string Restore(std::size_t slot);
    std::string Kill(std::size_t slot);
    std::string ClearCooldowns(std::size_t slot);
    std::string LineOfSight(float x1, float y1, float z1, float x2, float y2, float z2) const;
    // A plain record from another part of the runtime (the episode, the director's harness), traced like any other.
    void Note(std::size_t actor, std::string const& producer, std::string const& kind, std::string const& outcome,
        std::string const& detail);

    // The staging baseline: no transient aura, nothing on cooldown, full health and mana, full durability. Used by
    // the director's arena close and restore job and by the harness.
    static std::uint32_t RestoreBaseline(Player* player);

    // Hooks.
    void OnPacketSent(WorldSession* session, WorldPacket const& packet);
    // True when an executor command produced the cast.
    bool OnSpellCast(Player* player, Spell* spell);
    // Fixture actors earn no achievement progress: no progression rows change in the training realm.
    bool SuppressesProgress(Player const* player);
    void OnShutdown(char const* phase);
    void OnShutdownCancel();

    struct Counters
    {
        std::uint32_t commands = 0, executed = 0, rejected = 0, superseded = 0, kept = 0, dropped = 0;
        std::uint32_t executorCasts = 0, triggeredCasts = 0, stockCasts = 0, coreCancels = 0;
        std::uint32_t stockMoves = 0, stockTargets = 0, stockAttacks = 0, pets = 0;
        std::uint32_t acquisitions = 0, releases = 0, losses = 0, suppressedProgress = 0;
    };
    Counters const& CountersFor(std::size_t slot) const { return _slots[slot].counters; }

private:
    struct InFlight
    {
        std::uint8_t castCount = 0;
        std::uint32_t spellId = 0;
        std::uint64_t actionId = 0;
        std::string origin;
        bool started = false;
    };

    struct Slot
    {
        std::uint64_t guid = 0;
        std::uint64_t epoch = 0;
        bool wanted = true;               // owned whenever online, until the console hands it back
        std::uint8_t castCount = 0;
        std::vector<InFlight> casts;      // executor casts the core has not finished or refused yet
        std::uint32_t spline = 0;         // the last movement spline the executor launched
        std::uint32_t seenSpline = 0;     // the last foreign spline already traced
        std::uint8_t seenGenerator = 0;   // the last foreign motion generator already traced
        std::uint64_t target = 0;         // the selection the executor last set or saw
        std::uint64_t victim = 0;         // the auto-attack victim the executor last set or saw
        bool petSeen = false;
        bool refusalTraced = false;
        Counters counters;
    };

    struct Scope
    {
        std::size_t slot = 0;
        std::uint64_t actionId = 0;
        std::string origin;
        std::uint8_t castCount = 0;
        std::uint32_t spellId = 0;
        std::optional<std::uint8_t> failed;
        bool started = false;
        bool went = false;
    };

    struct Part
    {
        std::string part;
        bool ok = true;
        std::string detail;
    };

    Player* Find(std::size_t slot) const;
    Player* Enemy(std::size_t slot) const;
    bool Visible(Player* self, Player* enemy) const;
    std::uint64_t Now() const;
    void Record(TraceRecord record);
    void RecordQueue(std::vector<QueueEvent> const& events);
    bool Acquire(std::size_t slot, Player* player);
    void Neutralize(std::size_t slot, Player* player, std::string const& producer);
    void Lose(std::size_t slot, std::string const& why);
    void Execute(QueuedCommand const& command);
    Part Cast(std::size_t slot, Player* self, Player* enemy, Ability const& ability, QueuedCommand const& command);
    Part StopCast(std::size_t slot, Player* self, QueuedCommand const& command);
    Part Select(std::size_t slot, Player* self, Player* enemy, bool clear);
    Part Attack(std::size_t slot, Player* self, Player* enemy, bool on);
    Part Face(std::size_t slot, Player* self, Player* enemy, Facing facing);
    Part Move(std::size_t slot, Player* self, Movement movement);
    void Observe(std::size_t slot, Player* player);
    void Perceive(std::size_t observer, WorldPacket const& packet);
    std::string Fingerprint(Player* self) const;

    std::optional<ActionCatalog> _catalog;
    std::array<Slot, kScenarioActors> _slots{};
    ActionQueue _queue{kScenarioActors, 150};
    std::optional<Scope> _scope;
    std::optional<std::pair<std::size_t, std::uint64_t>> _stopping;   // slot and action id of a stop-cast in progress
    std::deque<TraceRecord> _trace;
    std::uint64_t _seq = 0;
    std::uint64_t _nextActionId = 1;
    std::uint64_t _firstActionId = 1;   // the first action id of the current generation
    std::uint64_t _generation = 0;
    std::uint64_t _nextOrigin = 1;
    bool _shuttingDown = false;
};
}

#endif
