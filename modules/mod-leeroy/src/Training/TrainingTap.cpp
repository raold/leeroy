// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "TrainingTap.h"
#include "LeeroyModule.h"
#include "LeeroyTrainingProtocol.h"
#include "LeeroyTrainingSlot.h"
#include "LeeroyTrainingTags.h"
#include "AiFactory.h"
#include "Config.h"
#include "Creature.h"
#include "InstanceScript.h"
#include "Log.h"
#include "Map.h"
#include "MapMgr.h"
#include "ObjectAccessor.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "Spell.h"
#include "SpellAuras.h"
#include "SpellInfo.h"
#include "UpdateTime.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <deque>
#include <limits>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <thread>
#include <unordered_map>
#include <utility>

namespace Leeroy::Training
{
namespace
{
constexpr std::size_t kSlots = 4, kEncoderJobs = 64;
std::atomic<bool> gAnyArmed{false};

std::uint64_t ClockMs()
{
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count());
}

std::uint64_t Key(uint32 map, uint32 instance) { return (std::uint64_t(map) << 32) | instance; }
std::uint64_t Key(Map const* map) { return Key(map->GetId(), map->GetInstanceId()); }
Map* MapOf(Unit* unit) { return unit && unit->IsInWorld() ? unit->GetMap() : nullptr; }

std::int32_t Coordinate(float value)
{
    auto scaled = std::round(double(value) * 10);
    if (!std::isfinite(scaled)) return 0;
    return static_cast<std::int32_t>(std::clamp(scaled,
        double(std::numeric_limits<std::int32_t>::min()), double(std::numeric_limits<std::int32_t>::max())));
}

std::uint8_t Percent(uint32 value, uint32 max)
{
    return max ? static_cast<std::uint8_t>(std::min<std::uint64_t>(100, std::uint64_t(value) * 100 / max)) : 0;
}

thread_local TrainingTags tTags;
std::uint64_t Raw(Unit* unit) { return unit ? unit->GetGUID().GetRawValue() : 0; }

bool Tracked(Map const* map)
{
    for (auto const& reference : map->GetPlayers())
        if (auto player = reference.GetSource())
            if (LeeroyModule::Instance().IsTracked(player->GetGUID().GetCounter()))
                return true;
    return false;
}
}

struct TrainingTap::Impl
{
    struct Slot
    {
        SlotPublication publication;
        SlotToken token{};
        std::unique_ptr<PullTable> table;
        ObjectGuid boss, dummy;
        std::array<uint32, 10> dummyMembers{};
        std::array<bool, kMaxMembers> enriched{};
        std::size_t dummyCount = 0;
        std::uint64_t started = 0, nextSample = 0, nextPulse = 0, closeAt = 0;
        std::uint64_t openingEpoch = 0;
        uint32 terminalAt = 0;
        Outcome outcome = Outcome::Aborted;
        bool closing = false, publish = true, awaitingStart = false, terminalIntent = false;
    };
    struct Work
    {
        std::unique_ptr<PullTable> table;
        std::function<std::vector<std::string>()> job;
        std::uint64_t epoch = 0;
        bool publish = false;
    };

    std::array<Slot, kSlots> slots;
    std::array<uint32, 32> watch{};
    std::size_t watchCount = 0;
    std::unordered_map<std::uint64_t, std::vector<uint32>> registered;
    std::atomic<bool> telemetry{false}, training{false}, mindArmed{false}, turnedOff{false}, running{false};
    std::atomic<std::uint64_t> epoch{0}, trainingEpoch{0}, lastFullLog{0};
    std::mutex mutex;
    std::condition_variable condition;
    std::deque<Work> work;
    std::vector<std::unique_ptr<PullTable>> spares;
    std::vector<CreatureCast> live;
    std::thread encoder;
    bool stopping = false;

    Slot* Find(Map* map, bool closingAllowed = false)
    {
        if (!map || !running.load(std::memory_order_relaxed)) return nullptr;
        auto key = Key(map);
        for (auto& slot : slots)
            if (auto token = slot.publication.Snapshot(); token && token->key == key)
            {
                if (closingAllowed) return &slot;
                if (slot.closing) return nullptr;
                if (!slot.dummyCount && (slot.awaitingStart || slot.terminalIntent))
                {
                    auto instance = map->ToInstanceMap();
                    auto script = instance ? instance->GetInstanceScript() : nullptr;
                    if (!script || script->GetBossState(slot.table->Header().bossId) != IN_PROGRESS)
                        return nullptr;
                }
                return &slot;
            }
        return nullptr;
    }

    uint32 Elapsed(Slot const& slot) const
    {
        auto now = ClockMs();
        return static_cast<uint32>(std::min<std::uint64_t>(now >= slot.started ? now - slot.started : 0,
            std::numeric_limits<uint32>::max()));
    }

    TagScope Scope(Slot const* slot) const
    {
        return slot ? TagScope{slot->token.key, slot->token.generation, std::size_t(slot - slots.data())} : TagScope{};
    }

    uint32 Member(Slot& slot, Unit* unit)
    {
        auto player = unit ? unit->GetCharmerOrOwnerPlayerOrPlayerItself() : nullptr;
        if (!player || !player->IsInWorld() || Key(player->GetMap()) != slot.token.key) return 0;
        auto guid = player->GetGUID().GetCounter();
        if (slot.dummyCount && std::find(slot.dummyMembers.begin(), slot.dummyMembers.begin() + slot.dummyCount,
            guid) == slot.dummyMembers.begin() + slot.dummyCount)
            return 0;
        // Spec lookup allocates; its unknown sentinel is filled by the world sampler, never a map hook.
        return slot.table->Member(guid, player->getClass(), -1) < 0 ? 0 : guid;
    }

    Slot* Open(Map* map, uint32 bossId, Source source)
    {
        if (!map || Find(map, true)) return nullptr;
        auto now = ClockMs();
        for (auto& slot : slots)
        {
            auto token = slot.publication.Claim(Key(map));
            if (!token) continue;
            slot.token = *token;
            PullHeader header{map->GetId(), map->GetInstanceId(), bossId, map->GetSpawnMode(),
                static_cast<std::uint8_t>(map->Is25ManRaid() ? 25 : 10),
                static_cast<std::uint64_t>(std::max<std::int64_t>(0, NowUnixMs())), source};
            if (!slot.table || !slot.table->Arm(header))
            {
                slot.publication.CancelClaim(*token);
                // A dirty table must never be republished; Tick/Start is responsible for preparing spares.
                LOG_ERROR("module.leeroy", "mod-leeroy: training slot was not prepared");
                return nullptr;
            }
            slot.boss.Clear();
            slot.dummy.Clear();
            slot.dummyCount = 0;
            slot.enriched.fill(false);
            slot.started = now;
            slot.nextSample = now + 1000;
            slot.nextPulse = now + 5000;
            slot.closeAt = 0;
            slot.openingEpoch = epoch.load();
            slot.terminalAt = 0;
            slot.outcome = Outcome::Aborted;
            slot.closing = false;
            slot.publish = true;
            slot.awaitingStart = true;
            slot.terminalIntent = false;
            slot.publication.Publish(*token);
            gAnyArmed.store(true, std::memory_order_relaxed);
            return &slot;
        }
        auto last = lastFullLog.load();
        if (now >= last && now - last >= 60000 && lastFullLog.compare_exchange_strong(last, now))
            LOG_WARN("module.leeroy", "mod-leeroy: training slots full; pull was not recorded");
        return nullptr;
    }

    void MarkClose(Slot& slot, Outcome outcome, bool publish)
    {
        if (slot.closing) { slot.publish = slot.publish && publish; return; }
        slot.closing = true;
        slot.outcome = outcome;
        slot.publish = publish;
        slot.closeAt = Elapsed(slot);
    }

    void Recycle(Slot& slot)
    {
        std::lock_guard lock(mutex);
        if (spares.empty()) return;
        auto token = slot.token;
        if (!slot.publication.Retire(token)) return;
        slot.table->Close(slot.outcome, SaturatingAdd(slot.table->Header().startMs, slot.closeAt),
            sWorldUpdateTime.GetAverageUpdateTime());
        work.push_back({std::move(slot.table), {}, slot.openingEpoch, slot.publish});
        slot.table = std::move(spares.back());
        spares.pop_back();
        slot.publication.Ready(token); // Fresh table is installed before another map can reserve this slot.
        condition.notify_one();
    }

    void EncodeLoop()
    {
        for (;;)
        {
            Work item;
            {
                std::unique_lock lock(mutex);
                condition.wait(lock, [&] { return stopping || !work.empty(); });
                if (stopping && work.empty()) return;
                item = std::move(work.front());
                work.pop_front();
            }
            try
            {
                if (item.publish && item.epoch == epoch.load() && running.load())
                {
                    auto lines = item.table ? EncodePull(*item.table, NowUnixMs()) : item.job();
                    for (auto& line : lines)
                    {
                        if (item.epoch != epoch.load() || !running.load()) break;
                        LeeroyModule::Instance().Publish(std::move(line), false);
                    }
                }
            }
            catch (std::exception const& error)
            {
                LOG_ERROR("module.leeroy", "mod-leeroy: training encoder failed: {}", error.what());
            }
            catch (...)
            {
                LOG_ERROR("module.leeroy", "mod-leeroy: training encoder failed");
            }
            if (item.table)
            {
                item.table->Reset({});
                std::lock_guard lock(mutex);
                spares.push_back(std::move(item.table));
            }
        }
    }
};

TrainingTap::TrainingTap() : _impl(std::make_unique<Impl>()) { }
TrainingTap::~TrainingTap() { Stop(); }
TrainingTap& TrainingTap::Instance() { static TrainingTap tap; return tap; }
bool TrainingTap::AnyArmed() { return gAnyArmed.load(std::memory_order_relaxed); }
bool TrainingTap::TelemetryOn() const { return _impl->telemetry.load(); }
bool TrainingTap::TrainingOn() const { return _impl->training.load(); }
bool TrainingTap::TrainingCleanupPending() const { return _impl->turnedOff.load(); }
std::uint64_t TrainingTap::TrainingEpoch() const { return _impl->trainingEpoch.load(); }
bool TrainingTap::MindArmed() const { return _impl->mindArmed.load(); }
bool TrainingTap::TakeTrainingTurnedOff() { return _impl->turnedOff.exchange(false); }

void TrainingTap::LoadConfig()
{
    bool telemetry = sConfigMgr->GetOption<bool>("Leeroy.Telemetry.Enable", false);
    bool training = sConfigMgr->GetOption<bool>("Leeroy.Training.Enable", false);
    bool oldTelemetry = _impl->telemetry.exchange(telemetry);
    bool oldTraining = _impl->training.exchange(training);
    if (oldTraining && !training)
    {
        _impl->turnedOff.store(true);
        _impl->trainingEpoch.fetch_add(1);
    }
    if (oldTelemetry && !telemetry) _impl->epoch.fetch_add(1);
    if (oldTelemetry != telemetry || oldTraining != training)
        LOG_INFO("module.leeroy", "mod-leeroy: telemetry {}, training {}",
            telemetry ? "on" : "off", training ? "on" : "off");
}

void TrainingTap::SetMindArmed(bool on)
{
    if (_impl->mindArmed.exchange(on) && !on) _impl->epoch.fetch_add(1);
}

void TrainingTap::SetWatch(std::vector<uint32> const& watch)
{
    _impl->watchCount = std::min(watch.size(), _impl->watch.size());
    std::copy_n(watch.begin(), _impl->watchCount, _impl->watch.begin());
}

void TrainingTap::RegisterInstance(uint32 map, uint32 instance, std::vector<uint32> bosses)
{
    if (!instance || bosses.empty() || bosses.size() > 20)
        throw std::invalid_argument("invalid training instance registration");
    auto key = Key(map, instance);
    if (!_impl->registered.contains(key) && _impl->registered.size() >= kSlots)
        throw std::length_error("training instance registrations full");
    _impl->registered[key] = std::move(bosses);
}

void TrainingTap::UnregisterInstance(uint32 map, uint32 instance)
{
    _impl->registered.erase(Key(map, instance));
    for (auto& slot : _impl->slots)
        if (auto token = slot.publication.Snapshot(); token && token->key == Key(map, instance))
            if (slot.table->Header().source == Source::Benchmark)
                _impl->MarkClose(slot, Outcome::Aborted, true);
}

void TrainingTap::OnBossState(uint32 id, uint8 newState, uint8 oldState, Map* map)
{
    if (!map || oldState == 5 || !_impl->running.load() || !TelemetryOn() || !MindArmed()
        || !LeeroyModule::Instance().Enabled()) return;
    auto transition = ClassifyBossTransition(newState, oldState);
    if (!transition) return;
    if (*transition == BossState::Engaged)
    {
        if (auto previous = _impl->Find(map, true))
        {
            // Two distinct engages before the world barrier cannot safely share a table. Discard the ambiguous
            // overlap rather than combine two pulls or turn a rejected terminal intent into a later false kill.
            if (!previous->dummyCount && previous->table->Header().bossId == id)
                _impl->MarkClose(*previous, Outcome::Aborted, false);
            return;
        }
        auto registered = _impl->registered.find(Key(map));
        bool known = registered != _impl->registered.end();
        bool allowed = !known || std::find(registered->second.begin(), registered->second.end(), id)
            != registered->second.end();
        if (ShouldArm({map->IsRaid(), TelemetryOn(), MindArmed(), Tracked(map), known, allowed, map->GetId(), id}))
            _impl->Open(map, id, known ? Source::Benchmark : Source::Tracked);
    }
    else if (auto slot = _impl->Find(map); slot && !slot->dummyCount && slot->table->Header().bossId == id)
    {
        // InstanceScript may reject this transition after the before-hook (e.g. living world-boss minions).
        slot->terminalIntent = true;
        slot->terminalAt = _impl->Elapsed(*slot);
        slot->awaitingStart = false; // oldState==IN_PROGRESS is already committed evidence of the start.
    }
}

void TrainingTap::OnSpellTag(Unit* a, Unit* b, uint32 spell)
{
    tTags.Clear();
    if (!b) return;
    auto slot = _impl->Find(MapOf(b));
    if (!slot || (a && (!MapOf(a) || Key(a->GetMap()) != slot->token.key))) return;
    tTags.Damage(Raw(a), Raw(b), _impl->Scope(slot), spell);
}

void TrainingTap::OnHealTag(Unit* a, Unit* b, uint32 raw, uint32 spell)
{
    tTags.Clear();
    if (!a || !b) return;
    auto slot = _impl->Find(MapOf(b));
    if (!slot || !MapOf(a) || Key(a->GetMap()) != slot->token.key) return;
    tTags.Heal(Raw(a), Raw(b), _impl->Scope(slot), spell, raw);
}

void TrainingTap::OnDealDamage(Unit* attacker, Unit* victim, uint32 damage)
{
    auto slot = _impl->Find(MapOf(victim));
    auto tag = tTags.TakeDamage(Raw(attacker), Raw(victim), _impl->Scope(slot));
    if (!slot || !LeeroyModule::Instance().Enabled()) return;
    if (slot->dummyCount && victim->GetGUID() != slot->dummy) return;
    auto owner = _impl->Member(*slot, attacker);
    auto target = victim->IsPlayer() ? _impl->Member(*slot, victim) : 0;
    auto creature = attacker ? attacker->ToCreature() : nullptr;
    slot->table->Damage({_impl->Elapsed(*slot), owner, creature ? creature->GetEntry() : 0, target,
        victim->GetGUID() == slot->boss || victim->GetGUID() == slot->dummy,
        tag.spell, damage, victim->GetHealth()});
}

void TrainingTap::OnHeal(Unit* healer, Unit* target, uint32 gain)
{
    auto slot = _impl->Find(MapOf(target));
    auto tag = tTags.TakeHeal(Raw(healer), Raw(target), _impl->Scope(slot));
    if (!slot || slot->dummyCount || !LeeroyModule::Instance().Enabled()) return;
    slot->table->Heal({_impl->Member(*slot, healer), _impl->Member(*slot, target), tag.spell,
        gain, tag.known ? tag.raw : gain});
}

void TrainingTap::OnAura(Unit* unit, Aura* aura)
{
    auto slot = _impl->Find(MapOf(unit));
    if (!slot || !aura || !unit->IsPlayer() || slot->dummyCount || !LeeroyModule::Instance().Enabled()) return;
    auto member = _impl->Member(*slot, unit);
    if (!member) return;
    auto info = aura->GetSpellInfo();
    if (!info) return;
    if (std::find(kAidSpells.begin(), kAidSpells.end(), info->Id) != kAidSpells.end())
        slot->table->MarkAssisted();
    bool watched = std::find(_impl->watch.begin(), _impl->watch.begin() + _impl->watchCount, info->Id)
        != _impl->watch.begin() + _impl->watchCount;
    auto caster = aura->GetCaster();
    if (watched || (!info->IsPositive() && (!caster || !caster->GetCharmerOrOwnerPlayerOrPlayerItself())))
        slot->table->Aura(member, info->Id, aura->GetStackAmount());
}

void TrainingTap::OnEnterCombat(Unit* unit)
{
    auto creature = unit ? unit->ToCreature() : nullptr;
    auto slot = _impl->Find(MapOf(creature));
    if (!slot || slot->dummyCount || !slot->boss.IsEmpty() || !LeeroyModule::Instance().Enabled()) return;
    if (creature->IsDungeonBoss() || creature->GetCreatureTemplate()->rank == CREATURE_ELITE_WORLDBOSS)
    {
        slot->boss = creature->GetGUID();
        slot->table->SetBoss(creature->GetEntry());
    }
}

void TrainingTap::OnDeath(Unit* unit)
{
    auto slot = _impl->Find(MapOf(unit));
    if (!slot || slot->dummyCount || !unit->IsPlayer() || !LeeroyModule::Instance().Enabled()) return;
    if (auto member = _impl->Member(*slot, unit))
        slot->table->Death(member, _impl->Elapsed(*slot),
            Coordinate(unit->GetPositionX()), Coordinate(unit->GetPositionY()));
}

void TrainingTap::OnSpell(Spell* spell, Unit* caster, SpellInfo const* info, uint8 state)
{
    auto slot = _impl->Find(MapOf(caster));
    if (!slot || !spell || !info || !LeeroyModule::Instance().Enabled()) return;
    auto elapsed = _impl->Elapsed(*slot);
    if (auto member = _impl->Member(*slot, caster))
    {
        if (state == 1) slot->table->Cast(member, info->Id, info->HasEffect(SPELL_EFFECT_INTERRUPT_CAST));
        return;
    }
    auto creature = caster->ToCreature();
    if (!creature || !creature->IsInCombat() || creature->GetCharmerOrOwnerPlayerOrPlayerItself()) return;
    if (state == 1 && creature->GetGUID() == slot->boss)
        slot->table->BossCast(info->Id, static_cast<uint16>(std::min<uint32>(elapsed / 1000, 65535)));
    auto target = spell->m_targets.GetUnitTarget();
    slot->table->Live({elapsed, creature->GetEntry(), creature->GetGUID().GetCounter(), info->Id,
        target && target->IsPlayer() ? target->GetGUID().GetCounter() : 0,
        state == 0 ? static_cast<uint32>(std::max<int32>(0, spell->GetCastTime())) : 0, state});
}

int TrainingTap::OpenDummy(std::vector<uint32> const& guids, ObjectGuid dummy, uint32 mapId)
{
    if (!_impl->running.load() || !TrainingOn() || !LeeroyModule::Instance().Enabled()
        || guids.empty() || guids.size() > 10 || dummy.IsEmpty()) return -1;
    Map* map = nullptr;
    for (auto guid : guids)
    {
        if (!LeeroyModule::Instance().Cast().IsCast(guid)) return -1;
        auto player = ObjectAccessor::FindConnectedPlayer(ObjectGuid::Create<HighGuid::Player>(guid));
        if (!player || !player->IsInWorld() || player->GetMapId() != mapId) return -1;
        if (map && map != player->GetMap()) return -1;
        map = player->GetMap();
    }
    auto target = map ? map->GetCreature(dummy) : nullptr;
    if (!target) return -1;
    auto slot = _impl->Open(map, 0, Source::Tracked);
    if (!slot) return -1;
    if (slot->token.generation > std::uint64_t(std::numeric_limits<int>::max()) / kSlots)
    {
        _impl->MarkClose(*slot, Outcome::Aborted, false);
        return -1;
    }
    slot->dummy = dummy;
    slot->awaitingStart = false;
    slot->openingEpoch = _impl->trainingEpoch.load();
    slot->dummyCount = guids.size();
    std::copy(guids.begin(), guids.end(), slot->dummyMembers.begin());
    slot->publish = false;
    for (auto guid : guids)
    {
        auto player = ObjectAccessor::FindConnectedPlayer(ObjectGuid::Create<HighGuid::Player>(guid));
        slot->table->Member(guid, player->getClass(), -1);
    }
    // The public int is an opaque generation-stamped handle, not a reusable array index.
    return static_cast<int>(slot->token.generation * kSlots + std::size_t(slot - _impl->slots.data()));
}

PullTable const* TrainingTap::Dummy(int handle) const
{
    if (handle < 0) return nullptr;
    auto const& slot = _impl->slots[std::size_t(handle) % kSlots];
    auto token = slot.publication.Snapshot();
    return token && token->generation == std::size_t(handle) / kSlots && slot.dummyCount && !slot.closing
        ? slot.table.get() : nullptr;
}

void TrainingTap::CloseDummy(int handle)
{
    if (handle < 0) return;
    auto& slot = _impl->slots[std::size_t(handle) % kSlots];
    auto token = slot.publication.Snapshot();
    if (token && token->generation == std::size_t(handle) / kSlots && slot.dummyCount)
        _impl->MarkClose(slot, Outcome::Aborted, false);
}

void TrainingTap::EnqueueEncode(std::function<std::vector<std::string>()> job)
{
    if (!job || !_impl->running.load()) throw std::runtime_error("training encoder unavailable");
    std::lock_guard lock(_impl->mutex);
    if (_impl->stopping || _impl->work.size() >= kEncoderJobs)
        throw std::length_error("training encoder queue full");
    _impl->work.push_back({{}, std::move(job), _impl->epoch.load(), true});
    _impl->condition.notify_one();
}

uint32 TrainingTap::ArmedCount() const
{
    uint32 count = 0;
    for (auto const& slot : _impl->slots)
        if (slot.publication.Snapshot()) ++count;
    return count;
}

void TrainingTap::Tick(uint32 /*diff*/)
{
    if (!_impl->running.load() || !AnyArmed()) return;
    auto now = ClockMs();
    auto& live = _impl->live;
    for (auto& slot : _impl->slots)
    {
        if (!slot.publication.Snapshot()) continue;
        auto const& header = slot.table->Header();
        auto map = sMapMgr->FindMap(header.map, header.instanceId);
        bool enabled = RecordingStillCurrent(LeeroyModule::Instance().Enabled()
            && (slot.dummyCount ? TrainingOn() : TelemetryOn() && MindArmed()), slot.openingEpoch,
            slot.dummyCount ? _impl->trainingEpoch.load() : _impl->epoch.load());
        if (!enabled) _impl->MarkClose(slot, Outcome::Aborted, false);
        else if (!map || ShouldAbort(slot.started, now))
            _impl->MarkClose(slot, Outcome::Aborted, slot.publish && !slot.awaitingStart);
        if (enabled && map && !slot.dummyCount && !slot.closing)
        {
            auto instance = map->ToInstanceMap();
            auto script = instance ? instance->GetInstanceScript() : nullptr;
            auto actual = script ? static_cast<uint8>(script->GetBossState(header.bossId)) : uint8(5);
            auto decision = ReviewBossState(slot.awaitingStart, slot.terminalIntent, actual);
            if (decision == BossRecordDecision::Discard)
                _impl->MarkClose(slot, Outcome::Aborted, false);
            else if (decision == BossRecordDecision::Confirm)
            {
                slot.awaitingStart = false;
                slot.terminalIntent = false;
            }
            else if (decision == BossRecordDecision::Killed || decision == BossRecordDecision::Wiped)
            {
                _impl->MarkClose(slot, decision == BossRecordDecision::Killed ? Outcome::Killed : Outcome::Wiped, true);
                slot.closeAt = slot.terminalAt;
            }
        }
        live.clear();
        slot.table->DrainLive(live);
        if (enabled && slot.publish && !slot.awaitingStart && !slot.dummyCount)
            for (auto const& cast : live)
                LeeroyModule::Instance().Publish(EncodeBossCast(header, cast, NowUnixMs()), true);
        if (enabled && map && !slot.closing && now >= slot.nextSample)
        {
            slot.nextSample = now + 1000;
            auto elapsed = _impl->Elapsed(slot);
            auto second = static_cast<uint16>(std::min<uint32>(elapsed / 1000, 65535));
            uint32 alive = 0;
            for (auto const& reference : map->GetPlayers())
            {
                auto player = reference.GetSource();
                auto member = _impl->Member(slot, player);
                if (!member) continue;
                auto index = slot.table->Member(member, player->getClass(), -1);
                if (index < 0) continue;
                if (!slot.enriched[std::size_t(index)])
                {
                    slot.table->SetSpecTab(member, static_cast<int8>(AiFactory::GetPlayerSpecTab(player)));
                    auto ilvl = player->GetAverageItemLevel();
                    slot.table->SetIlvl(member, std::isfinite(ilvl)
                        ? static_cast<uint16>(std::clamp(ilvl, 0.0f, 65535.0f)) : 0);
                    slot.table->SetTank(member, PlayerbotAI::IsTank(player, true));
                    slot.enriched[std::size_t(index)] = true;
                }
                if (player->IsAlive()) ++alive;
                slot.table->Sample(member, second,
                    Coordinate(player->GetPositionX()), Coordinate(player->GetPositionY()),
                    Percent(player->GetHealth(), player->GetMaxHealth()),
                    Percent(player->GetPower(player->getPowerType()), player->GetMaxPower(player->getPowerType())));
            }
            auto boss = slot.boss.IsEmpty() ? nullptr : map->GetCreature(slot.boss);
            if (boss)
                slot.table->BossSample(second, boss->GetEntry(), Coordinate(boss->GetPositionX()),
                    Coordinate(boss->GetPositionY()), static_cast<int16>(std::round(boss->GetOrientation() * 100)),
                    Percent(boss->GetHealth(), boss->GetMaxHealth()),
                    boss->GetVictim() && boss->GetVictim()->IsPlayer() ? boss->GetVictim()->GetGUID().GetCounter() : 0);
            if (!slot.dummyCount && now >= slot.nextPulse)
            {
                slot.nextPulse = now + 5000;
                LeeroyModule::Instance().Publish(EncodePulse(header, elapsed,
                    boss ? Percent(boss->GetHealth(), boss->GetMaxHealth()) : 0, alive, NowUnixMs()), true);
            }
        }
        if (slot.closing) _impl->Recycle(slot);
    }
    gAnyArmed.store(ArmedCount() != 0, std::memory_order_relaxed);
}

void TrainingTap::Start()
{
    if (_impl->running.load()) return;
    _impl->spares.reserve(kSlots * 2);
    _impl->live.reserve(kLiveRing);
    for (auto& slot : _impl->slots)
    {
        slot.table = std::make_unique<PullTable>();
        slot.table->Reset({});
    }
    for (std::size_t i = 0; i < kSlots; ++i)
    {
        auto table = std::make_unique<PullTable>();
        table->Reset({});
        _impl->spares.push_back(std::move(table));
    }
    _impl->stopping = false;
    _impl->running.store(true);
    try { _impl->encoder = std::thread([this] { _impl->EncodeLoop(); }); }
    catch (...)
    {
        _impl->running.store(false);
        _impl->spares.clear();
        for (auto& slot : _impl->slots) slot.table.reset();
        throw;
    }
}

void TrainingTap::Stop()
{
    if (!_impl->running.exchange(false) && !_impl->encoder.joinable()) return;
    gAnyArmed.store(false, std::memory_order_relaxed);
    _impl->epoch.fetch_add(1);
    {
        std::lock_guard lock(_impl->mutex);
        _impl->stopping = true;
        _impl->condition.notify_all();
    }
    if (_impl->encoder.joinable()) _impl->encoder.join();
    for (auto& slot : _impl->slots)
    {
        slot.publication.Retire(slot.token);
        slot.publication.CancelClaim(slot.token);
        slot.publication.Ready(slot.token);
        slot.table.reset();
    }
    _impl->spares.clear();
    _impl->registered.clear();
    _impl->work.clear();
}
}
