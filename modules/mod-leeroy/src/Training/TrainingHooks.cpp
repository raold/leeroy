// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "TrainingTap.h"
#include "ScriptMgr.h"
#include "SpellInfo.h"

using Leeroy::Training::TrainingTap;

class LeeroyTrainingUnitScript : public UnitScript
{
public:
    LeeroyTrainingUnitScript() : UnitScript("LeeroyTrainingUnitScript", true,
        { UNITHOOK_ON_HEAL, UNITHOOK_MODIFY_PERIODIC_DAMAGE_AURAS_TICK, UNITHOOK_MODIFY_MELEE_DAMAGE,
          UNITHOOK_MODIFY_SPELL_DAMAGE_TAKEN, UNITHOOK_MODIFY_HEAL_RECEIVED, UNITHOOK_ON_AURA_APPLY,
          UNITHOOK_ON_UNIT_ENTER_COMBAT, UNITHOOK_ON_UNIT_DEATH }) { }

    uint32 DealDamage(Unit* attacker, Unit* victim, uint32 damage, DamageEffectType /*type*/) override
    {
        if (TrainingTap::AnyArmed()) TrainingTap::Instance().OnDealDamage(attacker, victim, damage);
        return damage;
    }

    void OnHeal(Unit* healer, Unit* target, uint32& gain) override
    {
        if (TrainingTap::AnyArmed()) TrainingTap::Instance().OnHeal(healer, target, gain);
    }

    void ModifyPeriodicDamageAurasTick(Unit* target, Unit* attacker, uint32& /*damage*/,
        SpellInfo const* info) override
    {
        if (TrainingTap::AnyArmed()) TrainingTap::Instance().OnSpellTag(attacker, target, info ? info->Id : 0);
    }

    void ModifyMeleeDamage(Unit* target, Unit* attacker, uint32& /*damage*/) override
    {
        if (TrainingTap::AnyArmed())
            TrainingTap::Instance().OnSpellTag(attacker, target, Leeroy::Training::kMeleeSpell);
    }

    void ModifySpellDamageTaken(Unit* target, Unit* attacker, int32& /*damage*/, SpellInfo const* info) override
    {
        if (TrainingTap::AnyArmed()) TrainingTap::Instance().OnSpellTag(attacker, target, info ? info->Id : 0);
    }

    void ModifyHealReceived(Unit* a, Unit* b, uint32& heal, SpellInfo const* info) override
    {
        if (TrainingTap::AnyArmed()) TrainingTap::Instance().OnHealTag(a, b, heal, info ? info->Id : 0);
    }

    void OnAuraApply(Unit* unit, Aura* aura) override
    {
        if (TrainingTap::AnyArmed()) TrainingTap::Instance().OnAura(unit, aura);
    }

    void OnUnitEnterCombat(Unit* unit, Unit* /*victim*/) override
    {
        if (TrainingTap::AnyArmed()) TrainingTap::Instance().OnEnterCombat(unit);
    }

    void OnUnitDeath(Unit* unit, Unit* /*killer*/) override
    {
        if (TrainingTap::AnyArmed()) TrainingTap::Instance().OnDeath(unit);
    }
};

class LeeroyTrainingSpellScript : public AllSpellScript
{
public:
    LeeroyTrainingSpellScript() : AllSpellScript("LeeroyTrainingSpellScript",
        { ALLSPELLHOOK_ON_PREPARE, ALLSPELLHOOK_ON_CAST, ALLSPELLHOOK_ON_CAST_CANCEL }) { }

    void OnSpellPrepare(Spell* spell, Unit* caster, SpellInfo const* info) override
    {
        if (TrainingTap::AnyArmed()) TrainingTap::Instance().OnSpell(spell, caster, info, 0);
    }

    void OnSpellCast(Spell* spell, Unit* caster, SpellInfo const* info, bool /*skipCheck*/) override
    {
        if (TrainingTap::AnyArmed()) TrainingTap::Instance().OnSpell(spell, caster, info, 1);
    }

    void OnSpellCastCancel(Spell* spell, Unit* caster, SpellInfo const* info, bool bySelf) override
    {
        if (!bySelf && TrainingTap::AnyArmed()) TrainingTap::Instance().OnSpell(spell, caster, info, 2);
    }
};

class LeeroyTrainingGlobalScript : public GlobalScript
{
public:
    LeeroyTrainingGlobalScript() : GlobalScript("LeeroyTrainingGlobalScript",
        { GLOBALHOOK_ON_BEFORE_SET_BOSS_STATE }) { }

    void OnBeforeSetBossState(uint32 id, EncounterState next, EncounterState old, Map* map) override
    {
        // Unlike combat callbacks, this is what arms the first slot, so AnyArmed cannot gate it.
        TrainingTap::Instance().OnBossState(id, static_cast<uint8>(next), static_cast<uint8>(old), map);
    }
};

void AddSC_leeroy_training_hooks()
{
    new LeeroyTrainingUnitScript();
    new LeeroyTrainingSpellScript();
    new LeeroyTrainingGlobalScript();
}
