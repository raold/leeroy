// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmAttempts.h"

#include <algorithm>

namespace Leeroy::Realm
{
void AttemptLedger::Clear()
{
    _instances.clear();
    _pending.clear();
    _epoch = 0;
    _guild = _human = 0;
}

void AttemptLedger::Synchronize(std::uint64_t epoch, std::uint32_t guild, std::uint32_t human)
{
    if (epoch == _epoch && guild == _guild && human == _human)
        return;
    Clear();
    _epoch = epoch;
    _guild = guild;
    _human = human;
}

AttemptLedger::Instance* AttemptLedger::Find(InstanceKey const& key)
{
    auto it = _instances.find(key);
    return it == _instances.end() ? nullptr : &it->second;
}

AttemptLedger::Boss* AttemptLedger::BossFor(Instance& instance, std::uint32_t boss, std::uint8_t spawnMode)
{
    auto it = instance.bosses.find({boss, spawnMode});
    if (it != instance.bosses.end())
        return &it->second;
    if (instance.bosses.size() >= BossCapacity)
    {
        instance.overflow = true; // An untracked boss could hide engagements of any other.
        return nullptr;
    }
    return &instance.bosses[{boss, spawnMode}];
}

void AttemptLedger::DropPending(InstanceKey const& key, std::uint32_t boss, std::uint8_t spawnMode)
{
    std::erase_if(_pending, [&](PendingStart const& start) {
        return start.key == key && start.boss == boss && start.spawnMode == spawnMode;
    });
}

void AttemptLedger::Expire(std::uint64_t nowMs)
{
    std::erase_if(_pending, [&](PendingStart const& start) {
        auto* instance = Find(start.key);
        if (!instance)
            return true;
        auto it = instance->bosses.find({start.boss, start.spawnMode});
        if (it == instance->bosses.end() || !it->second.pending)
            return true;
        auto& boss = it->second;
        if (nowMs >= boss.pendingAtMs && nowMs - boss.pendingAtMs <= kAttemptConfirmMs)
            return false;
        boss.pending = false;
        boss.unknown = true; // Unconfirmed in time, or a clock reversal: never guess.
        return true;
    });
}

void AttemptLedger::Resolve(InstanceKey const& key, std::uint32_t bossId, std::uint8_t spawnMode, Boss& boss,
    EncounterPhase actual, std::uint64_t nowMs)
{
    boss.pending = false;
    DropPending(key, bossId, spawnMode);
    if (nowMs < boss.pendingAtMs || nowMs - boss.pendingAtMs > kAttemptConfirmMs)
        boss.unknown = true;
    else if (actual == EncounterPhase::Progress)
    {
        if (boss.count >= kMaxAttempts) boss.unknown = true;
        else
        {
            ++boss.count;
            boss.open = true;
        }
    }
    else if (actual != EncounterPhase::Resting && actual != EncounterPhase::Unset)
        boss.unknown = true;
    // A start the core did not apply (actual still resting) is no attempt.
}

void AttemptLedger::Created(InstanceKey const& key, bool fresh)
{
    if (!key.instanceId)
        return;
    std::erase_if(_pending, [&](PendingStart const& start) {
        if (!(start.key == key)) return false;
        if (auto* instance = Find(key))
            if (auto it = instance->bosses.find({start.boss, start.spawnMode}); it != instance->bosses.end())
            {
                it->second.pending = false;
                it->second.unknown = true;
            }
        return true;
    });
    if (!fresh)
    {
        if (auto* instance = Find(key)) instance->touched = ++_clock;
        return; // An instance this process did not see created is never witnessed.
    }
    _instances[key] = Instance{++_clock, false, {}};
    if (_instances.size() <= InstanceCapacity)
        return;
    auto oldest = _instances.end();
    for (auto it = _instances.begin(); it != _instances.end(); ++it)
        if (!(it->first == key) && (oldest == _instances.end() || it->second.touched < oldest->second.touched))
            oldest = it;
    if (oldest != _instances.end())
    {
        auto const evicted = oldest->first;
        _instances.erase(oldest);
        std::erase_if(_pending, [&](PendingStart const& start) { return start.key == evicted; });
    }
}

void AttemptLedger::Unloaded(InstanceKey const& key)
{
    auto* instance = Find(key);
    if (!instance)
        return;
    for (auto& [id, boss] : instance->bosses)
    {
        (void)id;
        if (boss.pending) boss.unknown = true;
        boss.pending = false;
        boss.open = false; // The core saves IN_PROGRESS and reloads it as NOT_STARTED.
    }
    std::erase_if(_pending, [&](PendingStart const& start) { return start.key == key; });
}

void AttemptLedger::Intent(InstanceKey const& key, std::uint32_t bossId, std::uint8_t spawnMode,
    EncounterPhase old, EncounterPhase next, EncounterPhase actual, bool qualified, std::uint64_t nowMs)
{
    Expire(nowMs);
    auto* instance = Find(key);
    if (!instance)
        return;
    instance->touched = ++_clock;
    if (instance->overflow)
        return;
    auto* boss = BossFor(*instance, bossId, spawnMode);
    if (!boss || boss->unknown)
        return;
    if (boss->pending)
        Resolve(key, bossId, spawnMode, *boss, actual, nowMs);
    if (boss->unknown)
        return;
    using P = EncounterPhase;
    if (actual != old)
    {
        boss->unknown = true; // The hook's old state must be the actual one.
        return;
    }
    if ((old == P::Resting || old == P::Unset) && next == P::Progress)
    {
        if (boss->open || !qualified || _pending.size() >= PendingCapacity)
            boss->unknown = true;
        else
        {
            boss->pending = true;
            boss->pendingAtMs = nowMs;
            _pending.push_back({key, bossId, spawnMode});
        }
    }
    else if (old == P::Progress && next == P::Resting)
    {
        if (boss->open) boss->open = false;
        else boss->unknown = true;
    }
    else if (old == P::Progress && next == P::Done)
    {
        if (!boss->open) boss->unknown = true; // The verifier decides whether DONE applied.
    }
    else if (old == next || (old == P::Unset && next == P::Resting))
        return;
    else
        boss->unknown = true; // Special, a DONE without an engagement, or a boss leaving DONE.
}

void AttemptLedger::ObserveActual(InstanceKey const& key, std::uint32_t bossId, std::uint8_t spawnMode,
    EncounterPhase actual, std::uint64_t nowMs)
{
    Expire(nowMs);
    auto* instance = Find(key);
    if (!instance)
        return;
    auto it = instance->bosses.find({bossId, spawnMode});
    if (it == instance->bosses.end() || !it->second.pending || it->second.unknown)
        return;
    Resolve(key, bossId, spawnMode, it->second, actual, nowMs);
}

std::vector<std::pair<std::uint32_t, std::uint8_t>> AttemptLedger::Unconfirmed(InstanceKey const& key) const
{
    std::vector<std::pair<std::uint32_t, std::uint8_t>> result;
    for (auto const& start : _pending)
        if (start.key == key) result.emplace_back(start.boss, start.spawnMode);
    return result;
}

std::uint32_t AttemptLedger::Kill(InstanceKey const& key, std::uint32_t bossId, std::uint8_t spawnMode,
    std::uint64_t nowMs)
{
    Expire(nowMs);
    auto* instance = Find(key);
    if (!instance)
        return 0;
    instance->touched = ++_clock;
    auto* boss = instance->overflow ? nullptr : BossFor(*instance, bossId, spawnMode);
    if (!boss)
        return 0;
    std::uint32_t const exact = !boss->unknown && !boss->pending && boss->open
        && boss->count >= 1 && boss->count <= kMaxAttempts ? boss->count : 0;
    boss->unknown = true; // Consumed: a second kill of this boss and mode is never counted.
    boss->open = boss->pending = false;
    DropPending(key, bossId, spawnMode);
    return exact;
}
}
