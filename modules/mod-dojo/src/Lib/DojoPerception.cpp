// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#include "DojoPerception.h"

#include "DojoManifest.h"
#include "DojoSha256.h"

#include <algorithm>
#include <cmath>
#include <limits>

using nlohmann::json;

namespace Dojo
{
namespace
{
constexpr std::array<EventKind, kEventKinds> kAllEventKinds = {EventKind::CastStarted, EventKind::CastCompleted,
    EventKind::CastStopped, EventKind::Died};

// Writes the schema's fields, or the values of one observation, in one order: both come from the same walk.
struct Emitter
{
    ObservationSchema* schema = nullptr;
    std::vector<double>* values = nullptr;

    void Field(std::string name, char const* unit, std::int64_t scale, std::int64_t low, std::int64_t high,
        std::int64_t missing, double raw, bool absent = false)
    {
        if (schema)
            schema->fields.push_back({std::move(name), unit, scale, low, high, missing});
        if (values)
        {
            double v = absent ? double(missing) : raw / double(scale);
            if (!std::isfinite(v))
                v = double(missing);
            values->push_back(RoundValue(std::clamp(v, double(low), double(high))));
        }
    }

    void Flag(std::string name, bool on, bool absent = false)
    {
        Field(std::move(name), "flag", 1, 0, 1, 0, on ? 1.0 : 0.0, absent);
    }
};

struct Inputs
{
    PerceptionConfig const* config = nullptr;
    std::vector<bool> const* kitEnemy = nullptr;   // per kit spell: needs the enemy as its target
    Percept const* now = nullptr;
    std::uint64_t startMs = 0;
    std::uint64_t seenAtMs = 0;
    EnemyView const* seen = nullptr;               // the enemy last seen, when not visible now
    std::vector<std::optional<std::uint64_t>> const* uses = nullptr;
    std::deque<PerceivedEvent> const* events = nullptr;
};

double Since(std::uint64_t now, std::uint64_t then)
{
    return now > then ? double(now - then) : 0.0;
}

void Emit(Emitter& e, std::vector<std::string> const& kit, std::vector<std::string> const& abilities, Inputs const& in)
{
    PerceptionConfig const& c = *in.config;
    Percept const& p = *in.now;
    SelfView const& s = p.self;

    // Context: who this actor is (D18: class, spec and level explicit).
    for (ClassName const& cls : kClasses)
        e.Flag("context.class." + std::string(cls.name), c.classId == cls.id);
    e.Field("context.level", "level", 80, 0, 1, 0, double(c.level));
    for (std::size_t t = 0; t < kTalentTrees; ++t)
        e.Field("context.tree." + std::to_string(t), "talent points", 71, 0, 1, 0, double(c.treePoints[t]));

    double const elapsed = Since(p.atMs, in.startMs);
    double const remaining = std::max(0.0, double(c.matchMs) - elapsed);
    e.Field("match.remaining", "fraction of the match limit", 1, 0, 1, 0, c.matchMs ? remaining / double(c.matchMs) : 0.0);

    // Self.
    e.Flag("self.alive", s.alive);
    e.Field("self.health", "fraction of max health", 1, 0, 1, 0, s.maxHealth ? double(s.health) / double(s.maxHealth) : 0.0);
    e.Field("self.mana", "fraction of max mana", 1, 0, 1, 0, s.maxMana ? double(s.mana) / double(s.maxMana) : 0.0);
    std::uint32_t gcd = 0;
    for (KitView const& k : s.kit)
        gcd = std::max(gcd, k.gcdMs);
    e.Field("self.gcd", "ms", 1500, 0, 1, 0, double(gcd));
    e.Flag("self.casting", s.casting);
    for (std::size_t k = 0; k < kit.size(); ++k)
        e.Flag("self.cast." + kit[k], s.casting && s.castKit == int(k));
    e.Field("self.cast.remaining", "ms", 3000, 0, 1, 0, s.casting ? double(s.castRemainingMs) : 0.0);
    e.Field("self.cast.progress", "fraction", 1, 0, 1, 0,
        s.casting && s.castTotalMs ? 1.0 - double(std::min(s.castRemainingMs, s.castTotalMs)) / double(s.castTotalMs) : 0.0);
    e.Flag("self.queued", s.queued);
    for (std::size_t k = 0; k < kit.size(); ++k)
    {
        KitView const kv = k < s.kit.size() ? s.kit[k] : KitView{};
        std::string const name = "self.kit." + kit[k];
        e.Flag(name + ".known", kv.known);
        e.Field(name + ".cooldown", "ms", 30000, 0, 1, 0, double(kv.cooldownMs));
        e.Field(name + ".cost", "fraction of max mana", 1, 0, 1, 0, kv.costKnown && s.maxMana ? double(kv.cost) / double(s.maxMana) : 0.0);
        bool const enemyTarget = in.kitEnemy && k < in.kitEnemy->size() && (*in.kitEnemy)[k];
        bool const inRange = !enemyTarget || (p.enemy && kv.rangeKnown && p.enemy->distance <= kv.maxRange);
        e.Flag(name + ".in_range", inRange);
    }
    double const radius = c.arenaRadius > 0 ? c.arenaRadius : 1.0;
    double const dx = double(s.x) - c.arenaX;
    double const dy = double(s.y) - c.arenaY;
    double const height = c.arenaZMax - c.arenaZMin;
    e.Field("self.x", "fraction of the arena radius", 1, -1, 1, 0, dx / radius);
    e.Field("self.y", "fraction of the arena radius", 1, -1, 1, 0, dy / radius);
    e.Field("self.z", "fraction of the arena height", 1, 0, 1, 0, height > 0 ? (double(s.z) - c.arenaZMin) / height : 0.0);
    e.Field("self.facing.sin", "unit", 1, -1, 1, 0, std::sin(double(s.o)));
    e.Field("self.facing.cos", "unit", 1, -1, 1, 0, std::cos(double(s.o)));
    e.Field("self.edge", "fraction of the arena radius", 1, -1, 1, 0, (radius - std::hypot(dx, dy)) / radius);
    e.Field("self.velocity.x", "yd/s", 14, -1, 1, 0, double(s.vx));
    e.Field("self.velocity.y", "yd/s", 14, -1, 1, 0, double(s.vy));
    e.Flag("self.moving", s.moving);
    e.Field("self.speed", "yd/s", 14, 0, 1, 0, double(s.runSpeed));
    e.Flag("self.rooted", s.rooted);
    e.Field("self.root.remaining", "ms", 10000, 0, 1, 0, s.rooted ? double(s.rootRemainingMs) : 0.0);
    e.Flag("self.stunned", s.stunned);
    e.Flag("self.silenced", s.silenced);
    e.Flag("self.frozen", s.frozen);
    e.Flag("self.in_combat", s.inCombat);
    e.Field("self.fingers_of_frost", "charges", 2, 0, 1, 0, double(s.fingersOfFrost));
    bool const pending = s.pending.has_value();
    e.Flag("self.pending", pending);
    for (std::size_t a = 0; a < abilities.size(); ++a)
        e.Flag("self.pending." + abilities[a], pending && s.pending->ability == a, !pending);
    e.Flag("self.pending.moves", pending && s.pending->moves, !pending);
    e.Field("self.pending.due", "ms", 200, 0, 1, 0, pending ? Since(s.pending->dueMs, p.atMs) : 0.0, !pending);

    // The enemy slot: as seen now, else as last seen (where it was and how it stood), else missing.
    bool const visible = p.enemy.has_value();
    EnemyView const* v = visible ? &*p.enemy : in.seen;
    bool const present = v != nullptr;
    e.Flag("enemy.present", present);
    e.Flag("enemy.visible", visible);
    e.Field("enemy.age", "ms", 10000, 0, 1, 1, visible ? 0.0 : Since(p.atMs, in.seenAtMs), !present);
    e.Flag("enemy.alive", present && v->alive, !present);
    e.Field("enemy.health", "fraction of max health", 1, 0, 1, 0,
        present && v->maxHealth ? double(v->health) / double(v->maxHealth) : 0.0, !present);
    e.Field("enemy.mana", "fraction of max mana", 1, 0, 1, 0,
        present && v->maxMana ? double(v->mana) / double(v->maxMana) : 0.0, !present);
    double const ex = present ? double(v->x) - double(s.x) : 0.0;
    double const ey = present ? double(v->y) - double(s.y) : 0.0;
    double const ez = present ? double(v->z) - double(s.z) : 0.0;
    double const co = std::cos(double(s.o));
    double const si = std::sin(double(s.o));
    e.Field("enemy.forward", "yd", 40, -1, 1, 0, ex * co + ey * si, !present);
    e.Field("enemy.left", "yd", 40, -1, 1, 0, -ex * si + ey * co, !present);
    e.Field("enemy.up", "yd", 10, -1, 1, 0, ez, !present);
    e.Field("enemy.distance", "yd", 40, 0, 1, 0, std::sqrt(ex * ex + ey * ey + ez * ez), !present);
    double const toSelf = std::atan2(-ey, -ex);
    e.Field("enemy.facing_me", "cosine", 1, -1, 1, 0, visible ? std::cos(toSelf - double(v->o)) : 0.0, !visible);
    e.Field("enemy.velocity.forward", "yd/s", 14, -1, 1, 0, visible ? double(v->vx) * co + double(v->vy) * si : 0.0, !visible);
    e.Field("enemy.velocity.left", "yd/s", 14, -1, 1, 0, visible ? -double(v->vx) * si + double(v->vy) * co : 0.0, !visible);
    e.Flag("enemy.casting", visible && v->casting, !visible);
    for (std::size_t k = 0; k < kit.size(); ++k)
        e.Flag("enemy.cast." + kit[k], visible && v->casting && v->castKit == int(k), !visible);
    e.Field("enemy.cast.progress", "fraction", 1, 0, 1, 0, visible && v->casting && v->castTotalMs
        ? 1.0 - double(std::min(v->castRemainingMs, v->castTotalMs)) / double(v->castTotalMs) : 0.0, !visible);
    e.Flag("enemy.rooted", visible && v->rooted, !visible);
    e.Field("enemy.root.remaining", "ms", 10000, 0, 1, 0, visible && v->rooted ? double(v->rootRemainingMs) : 0.0, !visible);
    e.Flag("enemy.stunned", visible && v->stunned, !visible);
    e.Flag("enemy.frozen", visible && v->frozen, !visible);
    e.Flag("enemy.slowed", visible && v->slowed, !visible);
    e.Flag("enemy.targeting_me", visible && v->targetingObserver, !visible);
    for (std::size_t k = 0; k < kit.size(); ++k)
    {
        // Inferred only from uses the actor saw: unknown until it has seen one this episode.
        bool const seen = in.uses && k < in.uses->size() && (*in.uses)[k].has_value();
        std::uint64_t const usedAt = seen ? (*in.uses)[k].value() : 0;
        std::uint32_t const base = k < c.kitCooldownMs.size() ? c.kitCooldownMs[k] : 0;
        std::string const name = "enemy.kit." + kit[k];
        e.Field(name + ".unknown", "flag", 1, 0, 1, 1, seen ? 0.0 : 1.0);
        e.Field(name + ".cooldown", "ms", 30000, 0, 1, 0, seen ? std::max(0.0, double(base) - Since(p.atMs, usedAt)) : 0.0, !seen);
    }

    // Recent events the actor saw, newest first.
    for (std::size_t i = 0; i < kEventSlots; ++i)
    {
        PerceivedEvent const* ev = in.events && i < in.events->size() ? &(*in.events)[i] : nullptr;
        bool const absent = ev == nullptr;
        std::string const name = "event." + std::to_string(i);
        e.Flag(name + ".present", !absent);
        e.Field(name + ".age", "ms", 2000, 0, 1, 1, absent ? 0.0 : Since(p.atMs, ev->atMs), absent);
        e.Flag(name + ".by_self", !absent && ev->bySelf, absent);
        for (EventKind kind : kAllEventKinds)
            e.Flag(name + ".kind." + std::string(EventKindName(kind)), !absent && ev->kind == kind, absent);
        for (std::size_t k = 0; k < kit.size(); ++k)
            e.Flag(name + ".kit." + kit[k], !absent && ev->kit == int(k), absent);
    }
}

std::vector<bool> KitTargets(ActionCatalog const& catalog)
{
    std::vector<bool> targets;
    for (KitAbility const& k : catalog.kit)
        targets.push_back(k.target == TargetSlot::Enemy);
    return targets;
}
}

std::string_view EventKindName(EventKind kind)
{
    switch (kind)
    {
        case EventKind::CastStarted: return "cast_started";
        case EventKind::CastCompleted: return "cast_completed";
        case EventKind::CastStopped: return "cast_stopped";
        case EventKind::Died: return "died";
    }
    return "?";
}

double RoundValue(double value)
{
    double const r = std::round(value * 1e6) / 1e6;
    return r == 0.0 ? 0.0 : r;
}

std::optional<std::size_t> ObservationSchema::Index(std::string_view name) const
{
    for (std::size_t i = 0; i < fields.size(); ++i)
        if (fields[i].name == name)
            return i;
    return std::nullopt;
}

ObservationSchema BuildObservationSchema(ActionCatalog const& catalog)
{
    ObservationSchema schema;
    for (Ability const& a : catalog.abilities)
    {
        schema.abilities.push_back(a.name);
        if (a.kind == AbilityKind::Cast)
            schema.kit.push_back(a.name);
    }
    schema.kitEnemyTarget = KitTargets(catalog);
    PerceptionConfig config;
    Percept p;
    p.self.kit.resize(schema.kit.size());
    Inputs in;
    in.config = &config;
    in.kitEnemy = &schema.kitEnemyTarget;
    in.now = &p;
    Emitter e;
    e.schema = &schema;
    Emit(e, schema.kit, schema.abilities, in);
    json doc = SchemaToJson(schema);
    schema.canonical = CanonicalJson(doc);
    schema.sha256 = Sha256Hex(schema.canonical);
    return schema;
}

json SchemaToJson(ObservationSchema const& schema)
{
    json fields = json::array();
    for (ObservationField const& f : schema.fields)
        fields.push_back({{"name", f.name}, {"unit", f.unit}, {"scale", f.scale}, {"low", f.low}, {"high", f.high},
            {"missing", f.missing}});
    json classes = json::array();
    for (ClassName const& c : kClasses)
        classes.push_back({{"id", c.id}, {"name", std::string(c.name)}});
    json kinds = json::array();
    for (EventKind k : kAllEventKinds)
        kinds.push_back(std::string(EventKindName(k)));
    return {{"schema", std::string(kObservationSchema)}, {"kit", schema.kit}, {"abilities", schema.abilities},
        {"classes", classes}, {"event_kinds", kinds}, {"event_slots", kEventSlots}, {"size", schema.fields.size()},
        {"fields", fields}};
}

EnemyView ViewOf(ActorTruth const& enemy, std::uint64_t observerGuid, float distance)
{
    SelfView const& s = enemy.self;
    EnemyView v;
    v.alive = s.alive;
    v.health = s.health;
    v.maxHealth = s.maxHealth;
    v.mana = s.mana;
    v.maxMana = s.maxMana;
    v.x = s.x;
    v.y = s.y;
    v.z = s.z;
    v.o = s.o;
    v.vx = s.vx;
    v.vy = s.vy;
    v.distance = distance;
    v.casting = s.casting;
    v.castKit = s.castKit;
    v.castRemainingMs = s.castRemainingMs;
    v.castTotalMs = s.castTotalMs;
    v.rooted = s.rooted;
    v.rootRemainingMs = s.rootRemainingMs;
    v.stunned = s.stunned;
    v.frozen = s.frozen;
    v.slowed = enemy.slowed;
    v.targetingObserver = observerGuid != 0 && enemy.target == observerGuid;
    return v;
}

Percept MakePercept(std::uint64_t atMs, ActorTruth const& self, ActorTruth const* visibleEnemy, float distance,
    std::vector<PerceivedEvent> events)
{
    Percept p;
    p.atMs = atMs;
    p.self = self.self;
    if (visibleEnemy)
        p.enemy = ViewOf(*visibleEnemy, self.guid, distance);
    p.events = std::move(events);
    return p;
}

namespace
{
json SelfToJson(SelfView const& s)
{
    json kit = json::array();
    for (KitView const& k : s.kit)
        kit.push_back({{"known", k.known}, {"cooldown_ms", k.cooldownMs}, {"gcd_ms", k.gcdMs}, {"cost_known", k.costKnown},
            {"cost", k.cost}, {"range_known", k.rangeKnown}, {"max_range", RoundValue(k.maxRange)}});
    return {{"alive", s.alive}, {"health", s.health}, {"max_health", s.maxHealth}, {"mana", s.mana},
        {"max_mana", s.maxMana}, {"mana_regen_bound", s.manaRegenBound}, {"casting", s.casting}, {"cast_kit", s.castKit},
        {"cast_remaining_ms", s.castRemainingMs}, {"cast_total_ms", s.castTotalMs}, {"queued", s.queued}, {"kit", kit},
        {"position", {RoundValue(s.x), RoundValue(s.y), RoundValue(s.z), RoundValue(s.o)}},
        {"velocity", {RoundValue(s.vx), RoundValue(s.vy)}}, {"moving", s.moving}, {"run_speed", RoundValue(s.runSpeed)},
        {"rooted", s.rooted}, {"root_remaining_ms", s.rootRemainingMs}, {"stunned", s.stunned}, {"silenced", s.silenced},
        {"frozen", s.frozen}, {"in_combat", s.inCombat}, {"fingers_of_frost", s.fingersOfFrost},
        {"pending", s.pending ? json{{"ability", s.pending->ability}, {"moves", s.pending->moves}, {"due_ms", s.pending->dueMs}}
                              : json(nullptr)}};
}
}

json TruthToJson(ActorTruth const& truth)
{
    return {{"guid", truth.guid}, {"self", SelfToJson(truth.self)}, {"slowed", truth.slowed}, {"target", truth.target}};
}

json PerceptToJson(Percept const& p)
{
    json enemy = nullptr;
    if (p.enemy)
    {
        EnemyView const& v = *p.enemy;
        enemy = {{"alive", v.alive}, {"health", v.health}, {"max_health", v.maxHealth}, {"mana", v.mana},
            {"max_mana", v.maxMana}, {"position", {RoundValue(v.x), RoundValue(v.y), RoundValue(v.z), RoundValue(v.o)}},
            {"velocity", {RoundValue(v.vx), RoundValue(v.vy)}}, {"distance", RoundValue(v.distance)},
            {"casting", v.casting}, {"cast_kit", v.castKit}, {"cast_remaining_ms", v.castRemainingMs},
            {"cast_total_ms", v.castTotalMs}, {"rooted", v.rooted}, {"root_remaining_ms", v.rootRemainingMs},
            {"stunned", v.stunned}, {"frozen", v.frozen}, {"slowed", v.slowed}, {"targeting_observer", v.targetingObserver}};
    }
    json events = json::array();
    for (PerceivedEvent const& e : p.events)
        events.push_back({{"at_ms", e.atMs}, {"by_self", e.bySelf}, {"kind", std::string(EventKindName(e.kind))}, {"kit", e.kit}});
    return {{"at_ms", p.atMs}, {"self", SelfToJson(p.self)}, {"enemy", enemy}, {"events", events}};
}

Perception::Perception(ObservationSchema schema, PerceptionConfig config)
    : _schema(std::move(schema)), _config(std::move(config))
{
    _enemyUses.assign(_schema.kit.size(), std::nullopt);
}

void Perception::Reset(std::uint64_t generation, std::uint64_t startMs)
{
    _generation = generation;
    _startMs = startMs;
    _newestMs = startMs;
    _lastDecisionMs = 0;
    _decided = false;
    _pending.clear();
    _current.reset();
    _lastSeen.reset();
    _enemyUses.assign(_schema.kit.size(), std::nullopt);
    _events.clear();
    _matured = 0;
}

std::string Perception::Push(Percept percept)
{
    if (percept.atMs < _startMs)
        return "a percept from before the episode started";
    if (percept.atMs < _newestMs)
        return "percepts out of time order";
    if (_pending.size() >= _config.maxPending)
        return "too many percepts waiting to mature";
    _newestMs = percept.atMs;
    _pending.push_back(std::move(percept));
    if (_newestMs >= _config.perceptualDelayMs)
        Mature(_newestMs - _config.perceptualDelayMs);
    return "";
}

void Perception::Mature(std::uint64_t upToMs)
{
    while (!_pending.empty() && _pending.front().atMs <= upToMs)
    {
        Percept p = std::move(_pending.front());
        _pending.pop_front();
        if (p.enemy)
            _lastSeen = Seen{p.atMs, *p.enemy};
        for (PerceivedEvent const& ev : p.events)
        {
            if (!ev.bySelf && ev.kind == EventKind::CastCompleted && ev.kit >= 0 && std::size_t(ev.kit) < _enemyUses.size())
                _enemyUses[std::size_t(ev.kit)] = ev.atMs;
            _events.push_front(ev);
            while (_events.size() > kEventSlots)
                _events.pop_back();
        }
        _current = std::move(p);
        ++_matured;
    }
}

std::optional<ActorObservation> Perception::Observe(std::uint64_t decisionMs, std::string& why)
{
    if (_decided && decisionMs < _lastDecisionMs)
    {
        why = "decision time went back";
        return std::nullopt;
    }
    if (decisionMs < _newestMs)
    {
        why = "a decision before the newest percept";
        return std::nullopt;
    }
    _decided = true;
    _lastDecisionMs = decisionMs;
    if (decisionMs >= _config.perceptualDelayMs)
        Mature(decisionMs - _config.perceptualDelayMs);
    if (!_current)
    {
        why = "no percept has matured yet";
        return std::nullopt;
    }
    ActorObservation o;
    o.generation = _generation;
    o.decisionMs = decisionMs;
    o.perceptMs = _current->atMs;
    o.values = Encode(*_current);
    o.knowledge = Knowledge(*_current);
    o.enemyVisible = _current->enemy.has_value();
    o.enemyPresent = o.enemyVisible || _lastSeen.has_value();
    return o;
}

std::vector<double> Perception::Encode(Percept const& p) const
{
    std::vector<double> values;
    values.reserve(_schema.fields.size());
    Inputs in;
    in.config = &_config;
    in.kitEnemy = &_schema.kitEnemyTarget;
    in.now = &p;
    in.startMs = _startMs;
    if (!p.enemy && _lastSeen)
    {
        in.seen = &_lastSeen->view;
        in.seenAtMs = _lastSeen->atMs;
    }
    in.uses = &_enemyUses;
    in.events = &_events;
    Emitter e;
    e.values = &values;
    Emit(e, _schema.kit, _schema.abilities, in);
    return values;
}

ActorKnowledge Perception::Knowledge(Percept const& p) const
{
    SelfView const& s = p.self;
    ActorKnowledge k;
    k.alive = s.alive;
    k.mana = s.mana;
    k.manaRegenBound = s.manaRegenBound;
    k.casting = s.casting;
    k.castRemainingMs = s.castRemainingMs;
    k.castQueued = s.queued;
    for (KitView const& kv : s.kit)
        k.kit.push_back({kv.known, kv.cooldownMs, kv.gcdMs, kv.costKnown, kv.cost, kv.rangeKnown, kv.maxRange});
    k.enemyKnown = p.enemy.has_value();
    if (p.enemy)
    {
        k.enemyAlive = p.enemy->alive;
        k.enemyDistance = p.enemy->distance;
    }
    return k;
}
}
