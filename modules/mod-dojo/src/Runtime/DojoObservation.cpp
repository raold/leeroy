// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#include "DojoObservation.h"

#include "DojoExecutor.h"

#ifndef DOJO_TRAINING_BUILD
#error "DOJO_TRAINING_BUILD is not set: mod-dojo.cmake was skipped (the module folder must be named exactly mod-dojo)"
#endif

#if DOJO_TRAINING_BUILD

#include "DBCStores.h"
#include "Log.h"
#include "MoveSpline.h"
#include "Player.h"
#include "Spell.h"
#include "SpellAuraEffects.h"
#include "SpellAuras.h"
#include "SpellInfo.h"
#include "SpellMgr.h"
#include "World.h"

#include <algorithm>
#include <cmath>

using nlohmann::json;

namespace Dojo
{
namespace
{
constexpr std::uint32_t kPerceptualDelayMs = 100;   // D30
constexpr std::size_t kDebugRecords = 600;          // six seconds of ticks at the 10 ms minimum update
constexpr std::size_t kInboxLimit = 256;
constexpr std::uint32_t kFingersOfFrost = 74396;    // the charges a mage's client shows

std::string KeyOf(Player const* p, std::uint64_t epoch)
{
    return std::to_string(p->GetGUID().GetRawValue()) + ":" + std::to_string(epoch) + ":" + std::to_string(p->GetMapId())
        + ":" + std::to_string(p->GetInstanceId());
}
}

Observer& Observer::Instance()
{
    static Observer observer;
    return observer;
}

std::string Observer::Prepare(Manifest const& manifest, ActionCatalog const& catalog)
{
    PerceptionConfig c;
    c.perceptualDelayMs = kPerceptualDelayMs;
    c.matchMs = manifest.rules.matchSeconds * 1000;
    c.arenaX = manifest.arena.bounds.x;
    c.arenaY = manifest.arena.bounds.y;
    c.arenaRadius = manifest.arena.bounds.radius;
    c.arenaZMin = manifest.arena.bounds.zMin;
    c.arenaZMax = manifest.arena.bounds.zMax;
    c.classId = manifest.loadout.cls.id;
    c.level = manifest.loadout.level;
    std::uint32_t placed = 0;
    for (TalentPick const& t : manifest.loadout.talents)
    {
        TalentEntry const* talent = sTalentStore.LookupEntry(t.talentId);
        TalentTabEntry const* tab = talent ? sTalentTabStore.LookupEntry(talent->TalentTab) : nullptr;
        if (!tab || tab->tabpage >= kTalentTrees)
            return "talent " + std::to_string(t.talentId) + " has no talent tree in the data";
        c.treePoints[tab->tabpage] += t.rank;
        placed += t.rank;
    }
    _kitSpells.clear();
    for (KitAbility const& k : catalog.kit)
    {
        SpellInfo const* info = sSpellMgr->GetSpellInfo(k.spellId);
        if (!info)
            return "kit spell " + std::to_string(k.spellId) + " is not in the spell data";
        c.kitCooldownMs.push_back(std::max(info->RecoveryTime, info->CategoryRecoveryTime));
        _kitSpells.push_back(k.spellId);
    }
    _config = c;
    _schema = BuildObservationSchema(catalog);
    for (std::optional<Perception>& p : _perception)
        p.reset();
    LOG_INFO("module.dojo", "mod-dojo: observation {}: {} values, sha256 {}; perceptual delay {} ms, {} talent points by tree",
        kObservationSchema, _schema->fields.size(), _schema->sha256, c.perceptualDelayMs, placed);
    return "";
}

json Observer::ContextJson() const
{
    json kit = json::array();
    for (std::size_t k = 0; k < _kitSpells.size(); ++k)
        kit.push_back({{"spell_id", _kitSpells[k]}, {"name", _schema ? _schema->kit[k] : ""},
            {"base_cooldown_ms", _config.kitCooldownMs[k]},
            {"target", _schema && _schema->kitEnemyTarget[k] ? "enemy" : "self"}});
    return {{"class_id", _config.classId}, {"level", _config.level}, {"tree_points", _config.treePoints},
        {"perceptual_delay_ms", _config.perceptualDelayMs}, {"match_ms", _config.matchMs}, {"kit", kit},
        {"arena", {{"x", _config.arenaX}, {"y", _config.arenaY}, {"radius", _config.arenaRadius},
            {"z_min", _config.arenaZMin}, {"z_max", _config.arenaZMax}}},
        {"schema", std::string(kObservationSchema)}, {"schema_sha256", _schema ? _schema->sha256 : ""},
        {"size", _schema ? _schema->fields.size() : 0}};
}

MaskRules Observer::Rules() const
{
    MaskRules rules = Control::Instance().ExecutionRules();
    rules.perceptualDelayMs = _config.perceptualDelayMs;
    return rules;
}

int Observer::KitIndex(std::uint32_t spellId) const
{
    for (std::size_t k = 0; k < _kitSpells.size(); ++k)
        if (_kitSpells[k] == spellId)
            return int(k);
    return -1;
}

void Observer::Begin(std::uint64_t generation, std::uint64_t startMs)
{
    Deactivate("episode " + std::to_string(generation) + " begins");
    _generation = generation;
    _startMs = startMs;
    _startFixed = true;
    _hold = false;
    _fault.clear();
    _debug.clear();
}

void Observer::Hold(std::string const& why)
{
    _hold = true;
    Deactivate(why);
    _debug.clear();
}

void Observer::Deactivate(std::string const& why)
{
    if (_active)
        ++_restarts;
    _active = false;
    _inactiveWhy = why;
    for (std::size_t i = 0; i < kScenarioActors; ++i)
    {
        _perception[i].reset();
        _inbox[i].clear();
        _key[i].clear();
    }
}

void Observer::Activate(std::uint64_t nowMs)
{
    if (!_startFixed)
        _startMs = nowMs;
    _startFixed = false;
    for (std::size_t i = 0; i < kScenarioActors; ++i)
    {
        _perception[i].emplace(*_schema, _config);
        _perception[i]->Reset(_generation, _startMs);
        _inbox[i].clear();
    }
    _active = true;
    _inactiveWhy.clear();
}

bool Observer::Sees(Player* self, Player* enemy) const
{
    return self && enemy && self->IsInWorld() && enemy->IsInWorld() && !enemy->IsBeingTeleported() && self->IsInMap(enemy)
        && self->CanSeeOrDetect(enemy) && self->IsWithinLOSInMap(enemy);
}

// Reads only: what the actor's own client shows of itself (its cooldowns, global cooldown, spell queue and pending
// command included) and what another client could see of it. The cost and range lookups pass no spell and run only
// while no spell is taking modifiers, so ApplySpellMod registers and drops no charge.
ActorTruth Observer::Read(Player* self, std::size_t slot) const
{
    ActorTruth t;
    t.guid = self->GetGUID().GetRawValue();
    SelfView& s = t.self;
    s.alive = self->IsAlive();
    s.health = self->GetHealth();
    s.maxHealth = self->GetMaxHealth();
    s.mana = self->GetPower(POWER_MANA);
    s.maxMana = self->GetMaxPower(POWER_MANA);
    float const regen = std::max(self->GetFloatValue(UNIT_FIELD_POWER_REGEN_FLAT_MODIFIER),
        self->GetFloatValue(UNIT_FIELD_POWER_REGEN_INTERRUPTED_FLAT_MODIFIER));
    s.manaRegenBound = std::uint32_t(std::ceil(std::max(regen, 0.0f) * 2.0f * sWorld->getRate(RATE_POWER_MANA))) + 1;
    Spell* current = self->GetCurrentSpell(CURRENT_GENERIC_SPELL);
    if (current && current->getState() == SPELL_STATE_PREPARING)
    {
        s.casting = true;
        s.castKit = KitIndex(current->GetSpellInfo()->Id);
        s.castRemainingMs = std::uint32_t(std::max(current->GetCastTimeRemaining(), 0));
        s.castTotalMs = std::uint32_t(std::max(current->GetCastTime(), 0));
    }
    else if (Spell* channel = self->GetCurrentSpell(CURRENT_CHANNELED_SPELL))
    {
        s.casting = channel->getState() != SPELL_STATE_FINISHED;
        s.castKit = KitIndex(channel->GetSpellInfo()->Id);
        s.castRemainingMs = UINT32_MAX;
    }
    s.queued = !self->SpellQueue.empty();
    bool const modsFree = self->m_spellModTakingSpell == nullptr;
    for (std::uint32_t const spellId : _kitSpells)
    {
        KitView k;
        SpellInfo const* info = sSpellMgr->GetSpellInfo(spellId);
        k.known = info && self->HasActiveSpell(spellId);
        if (info)
        {
            k.cooldownMs = self->GetSpellCooldownDelay(spellId);
            k.gcdMs = self->GetGlobalCooldownMgr().GetGlobalCooldown(info);
            if (modsFree)
            {
                k.costKnown = info->PowerType == POWER_MANA;
                k.cost = k.costKnown ? std::uint32_t(std::max(info->CalcPowerCost(self, info->GetSchoolMask(), nullptr), 0)) : 0;
                k.rangeKnown = true;
                k.maxRange = info->GetMaxRange(false, self, nullptr);
            }
        }
        s.kit.push_back(k);
    }
    s.x = self->GetPositionX();
    s.y = self->GetPositionY();
    s.z = self->GetPositionZ();
    s.o = self->GetOrientation();
    s.moving = !self->movespline->Finalized();
    if (s.moving && self->movespline->Duration() > 0)
    {
        // What a client sees of a moving unit: its spline's destination and speed.
        G3D::Vector3 const to = self->movespline->FinalDestination();
        float const dx = to.x - s.x;
        float const dy = to.y - s.y;
        float const length = std::hypot(dx, dy);
        if (length > 0.01f)
        {
            s.vx = dx / length * self->movespline->Velocity();
            s.vy = dy / length * self->movespline->Velocity();
        }
    }
    s.runSpeed = self->GetSpeed(MOVE_RUN);
    s.rooted = self->HasUnitState(UNIT_STATE_ROOT) || self->IsRooted();
    for (AuraEffect const* effect : self->GetAuraEffectsByType(SPELL_AURA_MOD_ROOT))
        s.rootRemainingMs = std::max(s.rootRemainingMs, std::uint32_t(std::max(effect->GetBase()->GetDuration(), 0)));
    s.stunned = self->HasUnitState(UNIT_STATE_STUNNED);
    s.silenced = self->HasUnitFlag(UNIT_FLAG_SILENCED);
    s.frozen = self->HasAuraState(AURA_STATE_FROZEN);
    s.inCombat = self->IsInCombat();
    if (Aura const* fingers = self->GetAura(kFingersOfFrost))
        s.fingersOfFrost = fingers->GetCharges();
    s.pending = Control::Instance().PendingFor(slot);
    t.slowed = self->HasAuraType(SPELL_AURA_MOD_DECREASE_SPEED);
    t.target = self->GetTarget().GetRawValue();
    return t;
}

void Observer::Capture(std::uint64_t nowMs)
{
    if (!Ready() || _hold)
        return;
    Control& control = Control::Instance();
    std::array<Player*, kScenarioActors> players{};
    std::array<std::string, kScenarioActors> keys;
    for (std::size_t i = 0; i < kScenarioActors; ++i)
    {
        players[i] = control.Actor(i);
        if (!players[i] || !players[i]->IsInWorld() || !control.Owns(i))
        {
            if (_active)
                Deactivate("actor " + std::to_string(i) + " is not owned and in the world");
            return;
        }
        keys[i] = KeyOf(players[i], control.Epoch(i));
    }
    bool const fresh = !_active || keys != _key;
    if (fresh)
    {
        if (_active)
            Deactivate("an actor changed map, instance or ownership");
        _key = keys;
        Activate(nowMs);
    }
    ++_captures;
    DebugRecord record;
    record.atMs = nowMs;
    record.generation = _generation;
    std::array<ActorTruth, kScenarioActors> truth;
    for (std::size_t i = 0; i < kScenarioActors; ++i)
    {
        truth[i] = Read(players[i], i);
        record.truth[i] = truth[i];
    }
    bool const together = players[0]->IsInMap(players[1]);
    record.distance = together ? players[0]->GetDistance(players[1]) : 0.0f;
    for (std::size_t i = 0; i < kScenarioActors; ++i)
        record.sees[i] = Sees(players[i], players[1 - i]);
    for (std::size_t i = 0; i < kScenarioActors; ++i)
    {
        bool const alive = truth[i].self.alive;
        if (!fresh && _wasAlive[i] && !alive)
        {
            _inbox[i].push_back({nowMs, true, EventKind::Died, -1});
            if (record.sees[1 - i])
            {
                _inbox[1 - i].push_back({nowMs, false, EventKind::Died, -1});
                ++_seenEvents;
            }
            else
                ++_hiddenEvents;
        }
        _wasAlive[i] = alive;
    }
    for (std::size_t i = 0; i < kScenarioActors; ++i)
    {
        if (players[i]->IsBeingTeleported())
            continue;   // its client is loading; what it was told waits for the next percept
        Percept p = MakePercept(nowMs, truth[i], record.sees[i] ? &truth[1 - i] : nullptr, record.distance,
            std::move(_inbox[i]));
        _inbox[i].clear();
        std::string const error = _perception[i]->Push(std::move(p));
        if (!error.empty())
        {
            _fault = "actor " + std::to_string(i) + ": " + error;
            LOG_ERROR("module.dojo", "mod-dojo: perception stopped: {}", _fault);
            Deactivate(_fault);
            return;
        }
    }
    _debug.push_back(std::move(record));
    while (_debug.size() > kDebugRecords)
        _debug.pop_front();
}

void Observer::OnSessionEvent(std::size_t observer, std::size_t source, EventKind kind, std::uint32_t spellId,
    std::uint64_t nowMs)
{
    // v1 events are the kit's casts (and deaths, from Capture): procs and triggered spells show as effects instead.
    int const kit = KitIndex(spellId);
    if (!_active || observer >= kScenarioActors || source >= kScenarioActors || kit < 0)
        return;
    Control& control = Control::Instance();
    bool const own = observer == source;
    if (!own && !Sees(control.Actor(observer), control.Actor(source)))
    {
        ++_hiddenEvents;
        return;
    }
    if (_inbox[observer].size() >= kInboxLimit)
    {
        _fault = "actor " + std::to_string(observer) + ": more than " + std::to_string(kInboxLimit) + " events in one tick";
        LOG_ERROR("module.dojo", "mod-dojo: perception stopped: {}", _fault);
        Deactivate(_fault);
        return;
    }
    if (!own)
        ++_seenEvents;
    _inbox[observer].push_back({nowMs, own, kind, kit});
}

std::optional<ActorObservation> Observer::Observe(std::size_t slot, std::uint64_t nowMs, std::string& why)
{
    if (slot >= kScenarioActors || !_active || !_perception[slot])
    {
        why = _hold ? "held: " + _inactiveWhy : _inactiveWhy.empty() ? "no perception" : _inactiveWhy;
        return std::nullopt;
    }
    return _perception[slot]->Observe(nowMs, why);
}

std::vector<std::vector<std::uint8_t>> Observer::Probe(std::size_t slot, std::uint64_t nowMs, std::uint32_t repeats)
{
    std::vector<std::vector<std::uint8_t>> masks;
    Control& control = Control::Instance();
    Player* self = control.Actor(slot);
    Player* enemy = control.Actor(1 - slot);
    if (!_active || !_perception[slot] || !self || !enemy)
        return masks;
    for (std::uint32_t r = 0; r < repeats; ++r)
    {
        ActorTruth const mine = Read(self, slot);
        ActorTruth const theirs = Read(enemy, 1 - slot);
        bool const sees = Sees(self, enemy);
        float const distance = self->IsInMap(enemy) ? self->GetDistance(enemy) : 0.0f;
        Perception copy = *_perception[slot];
        std::string why;
        if (!copy.Push(MakePercept(nowMs, mine, sees ? &theirs : nullptr, distance, {})).empty())
            break;
        std::optional<ActorObservation> o = copy.Observe(nowMs, why);
        if (!o)
            break;
        masks.push_back(ComputeMask(control.Catalog(), o->knowledge, Rules()).mask);
    }
    return masks;
}

json Observer::DebugJson(std::size_t last) const
{
    json out = json::array();
    std::size_t const from = _debug.size() > last ? _debug.size() - last : 0;
    for (std::size_t i = from; i < _debug.size(); ++i)
    {
        DebugRecord const& r = _debug[i];
        json truths = json::array();
        for (std::optional<ActorTruth> const& t : r.truth)
            truths.push_back(t ? TruthToJson(*t) : json(nullptr));
        out.push_back({{"at_ms", r.atMs}, {"generation", r.generation}, {"sees", r.sees},
            {"distance", RoundValue(r.distance)}, {"truth", truths}});
    }
    return out;
}

json Observer::StatsJson() const
{
    json pending = json::array();
    json matured = json::array();
    for (std::optional<Perception> const& p : _perception)
    {
        pending.push_back(p ? p->PendingCount() : 0);
        matured.push_back(p ? p->MaturedCount() : 0);
    }
    return {{"active", _active}, {"hold", _hold}, {"generation", _generation}, {"start_ms", _startMs},
        {"captures", _captures}, {"seen_events", _seenEvents}, {"hidden_events", _hiddenEvents}, {"restarts", _restarts},
        {"pending", pending}, {"matured", matured}, {"debug_records", _debug.size()}, {"fault", _fault},
        {"inactive", _inactiveWhy}};
}
}

#endif
