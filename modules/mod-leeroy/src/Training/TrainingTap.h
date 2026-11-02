// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_TRAINING_TAP_H
#define MOD_LEEROY_TRAINING_TAP_H

#include "Define.h"
#include "LeeroyPullRecorder.h"
#include <functional>
#include <memory>
#include <string>
#include <vector>

class Aura;
class Map;
class ObjectGuid;
class Spell;
class Unit;
class SpellInfo;

namespace Leeroy::Training
{
class TrainingTap
{
public:
    static TrainingTap& Instance();
    static bool AnyArmed();
    ~TrainingTap();
    void LoadConfig();
    bool TelemetryOn() const;
    bool TrainingOn() const;
    bool TrainingCleanupPending() const;
    std::uint64_t TrainingEpoch() const;
    bool MindArmed() const;
    bool TakeTrainingTurnedOff();
    void SetMindArmed(bool);
    void SetWatch(std::vector<uint32> const&);
    void RegisterInstance(uint32 map, uint32 instance, std::vector<uint32> bosses);
    void UnregisterInstance(uint32 map, uint32 instance);
    int OpenDummy(std::vector<uint32> const& guids, ObjectGuid dummy, uint32 map);
    PullTable const* Dummy(int slot) const;
    bool DummyAttacked(int slot, uint32 member) const;
    void CloseDummy(int slot);
    void EnqueueEncode(std::function<std::vector<std::string>()> job);

    void OnBossState(uint32 id, uint8 newState, uint8 oldState, Map*);
    void OnDealDamage(Unit* attacker, Unit* victim, uint32 damage);
    void OnSpellTag(Unit* a, Unit* b, uint32 spell);
    void OnHeal(Unit* healer, Unit* target, uint32 gain);
    void OnHealTag(Unit* a, Unit* b, uint32 raw, uint32 spell);
    void OnAura(Unit*, Aura*);
    void OnEnterCombat(Unit*);
    void OnDeath(Unit*);
    void OnSpell(Spell*, Unit* caster, SpellInfo const*, uint8 state);

    void Tick(uint32 diff);
    uint32 ArmedCount() const;
    void Start();
    void Stop();

private:
    TrainingTap();
    struct Impl;
    std::unique_ptr<Impl> _impl;
};
}
#endif
