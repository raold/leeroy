// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_DOJO_DOJO_PERCEPTION_H
#define MOD_DOJO_DOJO_PERCEPTION_H

#include "DojoActionCatalog.h"

#include <array>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include <nlohmann/json.hpp>

// The v1 actor observation (D18), its visibility and perceptual delay (D19, D30), and the masks that follow from it.
// No AzerothCore includes and no clock: every time is a millisecond count the caller passes (ordinary game time in
// plan Task 7, simulation time once the step driver arrives), so the configured delays are checked from timestamps
// alone. leeroy_dojo.observation builds the same schema; tests/fixtures/observation_cases.json pins its hash.
//
// The runtime captures one Percept per actor and world tick: the actor's own state as its client shows it, the enemy
// only while the actor can see it (in the map, detectable, in line of sight), and the events its client was told of
// that it could see. Hidden state (the enemy's cooldowns, where it is while out of sight, seeds, the core's internals)
// never enters a Percept: it lives in the runtime's separate debug records.
//
// Perception holds one actor's percepts for one episode. A percept matures once it is perceptualDelayMs old; only
// matured percepts update the actor's memory (the enemy last seen, its kit uses seen, recent events) and only the
// newest matured percept and that memory make the observation and the mask knowledge at a decision. So a change in
// the world reaches an observation or a mask no sooner than perceptualDelayMs after it happened, and a change the actor
// cannot see does not reach it at all. Reset clears everything at an episode boundary.
namespace Dojo
{
inline constexpr std::string_view kObservationSchema = "leeroy-dojo-observation-v1";
inline constexpr std::size_t kEventSlots = 4;
inline constexpr std::size_t kTalentTrees = 3;

enum class EventKind : std::uint8_t
{
    CastStarted,     // a cast bar appeared (SMSG_SPELL_START)
    CastCompleted,   // the spell went off (SMSG_SPELL_GO)
    CastStopped,     // a cast failed, was interrupted or cancelled
    Died,
};
inline constexpr std::size_t kEventKinds = 4;

std::string_view EventKindName(EventKind kind);

// The WotLK class ids and the names the schema uses for them, in schema order.
struct ClassName
{
    std::uint32_t id;
    std::string_view name;
};
inline constexpr std::array<ClassName, 10> kClasses = {{{1, "warrior"}, {2, "paladin"}, {3, "hunter"}, {4, "rogue"},
    {5, "priest"}, {6, "death_knight"}, {7, "shaman"}, {8, "mage"}, {9, "warlock"}, {11, "druid"}}};

// One observation value: its unit, the scale the raw value is divided by, its clip bounds, and the value it takes when
// what it describes is missing (no enemy seen, no event in the slot, nothing pending).
struct ObservationField
{
    std::string name;
    std::string unit;
    std::int64_t scale = 1;
    std::int64_t low = 0;
    std::int64_t high = 1;
    std::int64_t missing = 0;
};

struct ObservationSchema
{
    std::vector<ObservationField> fields;
    std::vector<std::string> kit;         // kit ability names, catalog kit order
    std::vector<std::string> abilities;   // catalog ability names
    std::vector<bool> kitEnemyTarget;     // per kit spell: cast at the enemy (else centred on the caster)
    std::string canonical;                // the canonical JSON the hash covers
    std::string sha256;

    std::optional<std::size_t> Index(std::string_view name) const;
};

ObservationSchema BuildObservationSchema(ActionCatalog const& catalog);
nlohmann::json SchemaToJson(ObservationSchema const& schema);

// Fixed for an episode: the arena frame from the manifest, the class, level and talent points, the match limit and the
// kit's base cooldowns (public spell data, used to infer the enemy's cooldowns from uses the actor saw).
struct PerceptionConfig
{
    std::uint32_t perceptualDelayMs = 100;   // D30
    std::uint32_t matchMs = 180000;
    double arenaX = 0, arenaY = 0, arenaRadius = 1, arenaZMin = 0, arenaZMax = 1;
    std::uint32_t classId = 0;
    std::uint32_t level = 0;
    std::array<std::uint32_t, kTalentTrees> treePoints{};
    std::vector<std::uint32_t> kitCooldownMs;   // catalog kit order
    std::size_t maxPending = 1024;              // percepts not yet mature; more is an error, never a silent drop
};

struct KitView
{
    bool known = false;
    std::uint32_t cooldownMs = 0;
    std::uint32_t gcdMs = 0;
    bool costKnown = false;
    std::uint32_t cost = 0;
    bool rangeKnown = false;
    float maxRange = 0.0f;
};

struct PendingView
{
    std::size_t ability = 0;      // catalog ability index
    bool moves = false;
    std::uint64_t dueMs = 0;
};

// What an actor's own client shows of itself.
struct SelfView
{
    bool alive = false;
    std::uint32_t health = 0, maxHealth = 0, mana = 0, maxMana = 0;
    std::uint32_t manaRegenBound = 0;
    bool casting = false;
    int castKit = -1;                // the kit spell being cast, -1 for none or another spell
    std::uint32_t castRemainingMs = 0;
    std::uint32_t castTotalMs = 0;
    bool queued = false;             // a request waiting in the core's spell queue
    std::vector<KitView> kit;        // catalog kit order
    float x = 0, y = 0, z = 0, o = 0;
    float vx = 0, vy = 0;            // yards per second, world frame
    bool moving = false;
    float runSpeed = 0;
    bool rooted = false, stunned = false, silenced = false, frozen = false, inCombat = false;
    std::uint32_t rootRemainingMs = 0;
    std::uint32_t fingersOfFrost = 0;
    std::optional<PendingView> pending;   // the actor's own pending command
};

// What an actor's client shows of a visible enemy.
struct EnemyView
{
    bool alive = false;
    std::uint32_t health = 0, maxHealth = 0, mana = 0, maxMana = 0;
    float x = 0, y = 0, z = 0, o = 0;
    float vx = 0, vy = 0;
    float distance = 0;              // yards between the two units' edges, as range checks measure it
    bool casting = false;
    int castKit = -1;
    std::uint32_t castRemainingMs = 0;
    std::uint32_t castTotalMs = 0;
    bool rooted = false, stunned = false, frozen = false, slowed = false;
    std::uint32_t rootRemainingMs = 0;
    bool targetingObserver = false;
};

struct PerceivedEvent
{
    std::uint64_t atMs = 0;
    bool bySelf = false;
    EventKind kind = EventKind::CastStarted;
    int kit = -1;
};

struct Percept
{
    std::uint64_t atMs = 0;
    SelfView self;
    std::optional<EnemyView> enemy;          // set only while the enemy is visible
    std::vector<PerceivedEvent> events;      // what the actor saw happen since its previous percept
};

// One actor's authoritative state in one tick, as the runtime reads it. Debug records keep it whole; a Percept carries
// the observer's own part as its self view and, only while the enemy is visible, what an observer can see of it.
struct ActorTruth
{
    std::uint64_t guid = 0;
    SelfView self;                 // what this actor's own client shows of itself, its cooldowns included
    bool slowed = false;
    std::uint64_t target = 0;      // its selection
};

// What an observer's client shows of a visible enemy: health, mana, position, motion, its cast bar, visible effects and
// whom it targets. Never its cooldowns, global cooldown, spell queue, regeneration, kit or pending command.
EnemyView ViewOf(ActorTruth const& enemy, std::uint64_t observerGuid, float distance);

// The observer's percept for one tick. visibleEnemy is null when the observer cannot see the enemy.
Percept MakePercept(std::uint64_t atMs, ActorTruth const& self, ActorTruth const* visibleEnemy, float distance,
    std::vector<PerceivedEvent> events);

nlohmann::json TruthToJson(ActorTruth const& truth);
nlohmann::json PerceptToJson(Percept const& percept);

struct ActorObservation
{
    std::uint64_t generation = 0;
    std::uint64_t decisionMs = 0;
    std::uint64_t perceptMs = 0;             // the matured percept it was made from
    std::vector<double> values;              // schema order
    ActorKnowledge knowledge;                // what the mask is computed from
    bool enemyPresent = false, enemyVisible = false;
};

class Perception
{
public:
    Perception(ObservationSchema schema, PerceptionConfig config);

    // An episode boundary: no percept, memory or event survives it.
    void Reset(std::uint64_t generation, std::uint64_t startMs);

    // Percepts arrive in time order, none before the episode's start. Empty on success; an error means the caller
    // must stop using this episode's observations.
    std::string Push(Percept percept);

    // The observation at decisionMs, made from the newest percept at least perceptualDelayMs old. Decisions never go
    // back in time. nullopt, with why, before any percept has matured.
    std::optional<ActorObservation> Observe(std::uint64_t decisionMs, std::string& why);

    std::uint64_t Generation() const { return _generation; }
    std::uint64_t StartMs() const { return _startMs; }
    std::size_t PendingCount() const { return _pending.size(); }
    std::size_t MaturedCount() const { return _matured; }
    PerceptionConfig const& Config() const { return _config; }
    ObservationSchema const& Schema() const { return _schema; }

private:
    struct Seen
    {
        std::uint64_t atMs = 0;
        EnemyView view;
    };

    void Mature(std::uint64_t upToMs);
    std::vector<double> Encode(Percept const& p) const;
    ActorKnowledge Knowledge(Percept const& p) const;

    ObservationSchema _schema;
    PerceptionConfig _config;
    std::uint64_t _generation = 0;
    std::uint64_t _startMs = 0;
    std::uint64_t _newestMs = 0;
    std::uint64_t _lastDecisionMs = 0;
    bool _decided = false;
    std::deque<Percept> _pending;
    std::optional<Percept> _current;
    std::optional<Seen> _lastSeen;
    std::vector<std::optional<std::uint64_t>> _enemyUses;   // per kit spell: when the actor last saw the enemy use it
    std::deque<PerceivedEvent> _events;                     // newest first, at most kEventSlots
    std::size_t _matured = 0;
};

// Rounds an observation value to the six decimals the wire carries.
double RoundValue(double value);
}

#endif
