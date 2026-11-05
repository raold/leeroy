// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_ATTEMPTS_H
#define MOD_LEEROY_REALM_ATTEMPTS_H

#include <compare>
#include <cstddef>
#include <cstdint>
#include <map>
#include <utility>
#include <vector>

namespace Leeroy::Realm
{
// Kill evidence carries an attempt count only when it is exact; 0 means unknown.
inline constexpr std::uint32_t kMaxAttempts = 99;

// The encounter states the counter distinguishes. Unset is the core's
// TO_BE_DECIDED (a boss never set since its instance script was created).
enum class EncounterPhase { Unset, Resting, Progress, Done, Special };

struct InstanceKey
{
    std::uint32_t map = 0, instanceId = 0;
    auto operator<=>(InstanceKey const&) const = default;
};

// An attempt is one confirmed engagement of a boss at one spawn mode in one
// instance: an intent from Resting or Unset to Progress, made while the complete
// present scan found the human or a native cast member of the configured guild,
// then confirmed by the actual Progress state on the owning map thread within
// kAttemptConfirmMs. A kill reports the count only when every engagement since
// the instance's creation was observable:
//   - the instance was witnessed: this process saw the core create it with no
//     saved instance (a new instance id) under the current native identity;
//     unloading and reloading the same id keeps the witness;
//   - no unqualified, unconfirmed, ambiguous or out-of-model transition touched
//     that boss and mode (Special, Done before a kill, a start while engaged,
//     a wipe or kill without an open engagement, a pending start at unload);
//   - the killing engagement is the open one, and the count is 1..kMaxAttempts.
// Anything else is 0, never a lower bound. A boss is consumed by its first
// verified kill. The adapter serializes every call (one mutex across map
// threads) and supplies actual states read on the owning map thread.
class AttemptLedger
{
public:
    static constexpr std::size_t InstanceCapacity = 1024, BossCapacity = 64, PendingCapacity = 128;
    static constexpr std::uint64_t kAttemptConfirmMs = 5000;

    void Clear();
    // Any change of native epoch or configured identity forgets every witness.
    void Synchronize(std::uint64_t epoch, std::uint32_t guild, std::uint32_t human);
    // Map creation. fresh: the core had no saved instance for this id.
    void Created(InstanceKey const& key, bool fresh);
    // Map destruction. An open engagement ends (an attempt that did not kill);
    // a start not yet confirmed becomes unknowable.
    void Unloaded(InstanceKey const& key);
    // Before SetBossState applies `next`, with the actual state read beforehand.
    void Intent(InstanceKey const& key, std::uint32_t boss, std::uint8_t spawnMode, EncounterPhase old,
        EncounterPhase next, EncounterPhase actual, bool qualified, std::uint64_t nowMs);
    // Map update: the actual state of a boss that has a start awaiting confirmation.
    void ObserveActual(InstanceKey const& key, std::uint32_t boss, std::uint8_t spawnMode,
        EncounterPhase actual, std::uint64_t nowMs);
    // Starts awaiting confirmation on this instance, as (boss, spawn mode).
    std::vector<std::pair<std::uint32_t, std::uint8_t>> Unconfirmed(InstanceKey const& key) const;
    // An actual DONE verified on the map thread. Returns the exact count or 0,
    // and consumes the boss at that mode either way.
    std::uint32_t Kill(InstanceKey const& key, std::uint32_t boss, std::uint8_t spawnMode, std::uint64_t nowMs);

    std::size_t Instances() const { return _instances.size(); }
    std::size_t Pending() const { return _pending.size(); }

private:
    struct Boss
    {
        std::uint32_t count = 0;
        bool open = false, pending = false, unknown = false;
        std::uint64_t pendingAtMs = 0;
    };
    struct Instance
    {
        std::uint64_t touched = 0;
        bool overflow = false;
        std::map<std::pair<std::uint32_t, std::uint8_t>, Boss> bosses;
    };
    struct PendingStart
    {
        InstanceKey key;
        std::uint32_t boss = 0;
        std::uint8_t spawnMode = 0;
    };
    Instance* Find(InstanceKey const& key);
    Boss* BossFor(Instance& instance, std::uint32_t boss, std::uint8_t spawnMode);
    void Expire(std::uint64_t nowMs);
    void DropPending(InstanceKey const& key, std::uint32_t boss, std::uint8_t spawnMode);
    void Resolve(InstanceKey const& key, std::uint32_t bossId, std::uint8_t spawnMode, Boss& boss,
        EncounterPhase actual, std::uint64_t nowMs);
    std::uint64_t _epoch = 0;
    std::uint32_t _guild = 0, _human = 0;
    std::uint64_t _clock = 0;
    std::map<InstanceKey, Instance> _instances;
    std::vector<PendingStart> _pending;
};
}

#endif
