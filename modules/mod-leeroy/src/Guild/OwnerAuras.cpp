// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "OwnerAuras.h"
#include "GuildPorts.h"
#include "LeeroyOwnerAuras.h"
#include "Config.h"
#include "Creature.h"
#include "Log.h"
#include "Map.h"
#include "ObjectAccessor.h"
#include "Pet.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "Playerbots.h"
#include "ScriptMgr.h"
#include "SpellAuraDefines.h"
#include "SpellAuras.h"
#include "SpellInfo.h"
#include "SpellMgr.h"
#include <algorithm>
#include <atomic>
#include <chrono>
#include <mutex>
#include <string>
#include <vector>

namespace Leeroy::Guild
{
namespace
{
std::uint64_t NowMs()
{
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count());
}

OwnerAuraSpell Lookup(std::uint32_t spell)
{
    SpellInfo const* info = sSpellMgr->GetSpellInfo(spell);
    if (!info)
        return OwnerAuraSpell::Unknown;
    for (std::uint8_t i = 0; i < MAX_SPELL_EFFECTS; ++i)
        if (info->Effects[i].IsUnitOwnedAuraEffect() && info->Effects[i].ApplyAuraName != SPELL_AURA_NONE)
            return OwnerAuraSpell::Ok;
    return OwnerAuraSpell::NotAnAura;
}

// One configured list: his own auras (Leeroy.OwnerAuras) or his pets' (Leeroy.OwnerPetAuras). The world
// thread loads it, checks it against the spell store on the first tick after a load (at startup the config
// loads before the spells do) and runs the passes; the hooks (map threads) read the checked list, note how a
// listed aura came off and wake the next pass, under the lock.
class Keeper
{
public:
    Keeper(char const* key, char const* name, char const* invalid) : _key(key), _name(name), _invalid(invalid) { }

    // World thread, at each config load.
    void Reload()
    {
        OwnerAuraList const list = ParseOwnerAuras(sConfigMgr->GetOption<std::string>(_key, ""));
        for (std::string const& entry : list.rejected)
            LOG_WARN("module.leeroy", "{} entry=\"{}\" reason=not_a_spell_id", _invalid, entry);
        _configured = list.spells;
        _checked = false;
    }

    // World thread: the checked list when a pass is due, else nothing.
    std::vector<std::uint32_t> Due(std::uint32_t diffMs)
    {
        if (!_checked)
            Check();
        if (!_any.load() || !GuildEnabled() || !_pass.Due(diffMs, _woken.exchange(false)))
            return {};
        std::lock_guard<std::mutex> guard(_lock);
        return _spells;
    }

    // Any thread.
    bool Any() const { return _any.load(std::memory_order_relaxed); }
    void Wake() { _woken.store(true); }
    void Note(std::uint32_t spell, OwnerAuraWhy why)
    {
        {
            std::lock_guard<std::mutex> guard(_lock);
            if (std::find(_spells.begin(), _spells.end(), spell) == _spells.end())
                return;
            _reasons.Note(spell, why);
        }
        _woken.store(true);
    }

    // World thread, in a pass.
    OwnerAuraWhy Why(std::uint32_t spell)
    {
        std::lock_guard<std::mutex> guard(_lock);
        return _reasons.Peek(spell);
    }
    void Restored(std::uint32_t spell)
    {
        std::lock_guard<std::mutex> guard(_lock);
        _reasons.Forget(spell);
    }

private:
    void Check()
    {
        _checked = true;
        OwnerAuraChecked const result = CheckOwnerAuras(_configured, Lookup);
        for (auto const& [spell, why] : result.invalid)
            LOG_WARN("module.leeroy", "{} spell={} reason={}", _invalid, spell, OwnerAuraSpellName(why));
        {
            std::lock_guard<std::mutex> guard(_lock);
            _spells = result.spells;
            _reasons.Clear();
        }
        _any.store(!result.spells.empty());
        if (!result.spells.empty())
        {
            std::string list;
            for (std::uint32_t spell : result.spells)
                list += (list.empty() ? "" : ",") + std::to_string(spell);
            LOG_INFO("module.leeroy", "{} spells={}", _name, list);
        }
        else if (_wasOn)
            LOG_INFO("module.leeroy", "{} off", _name);
        _wasOn = !result.spells.empty();
    }

    char const* _key;
    char const* _name;
    char const* _invalid;
    std::vector<std::uint32_t> _configured;
    bool _checked = true;
    bool _wasOn = false;
    OwnerAuraPass _pass;

    std::mutex _lock;
    std::vector<std::uint32_t> _spells;
    OwnerAuraReasons _reasons;
    std::atomic<bool> _any{false};
    std::atomic<bool> _woken{false};
};

Keeper own("Leeroy.OwnerAuras", "owner_auras", "owner_aura_invalid");
Keeper pets("Leeroy.OwnerPetAuras", "owner_pet_auras", "owner_pet_aura_invalid");
// World thread: his controlled pet and his guardians log apart, each once a minute per spell and outcome.
OwnerAuraLogGate ownGate;
OwnerAuraLogGate petGate;
OwnerAuraLogGate guardianGate;

bool IsHis(ObjectGuid owner)
{
    return owner.IsPlayer() && owner.GetCounter() == HumanGuid();
}

OwnerAuraWhy WhyOf(AuraRemoveMode mode)
{
    switch (mode)
    {
        case AURA_REMOVE_BY_ENEMY_SPELL: return OwnerAuraWhy::Dispelled;
        case AURA_REMOVE_BY_DEATH: return OwnerAuraWhy::Death;
        case AURA_REMOVE_BY_EXPIRE: return OwnerAuraWhy::Expired;
        case AURA_REMOVE_BY_CANCEL: return OwnerAuraWhy::Cancelled;
        default: return OwnerAuraWhy::Unknown;
    }
}

OwnerAuraState StateOf(Player* player)
{
    OwnerAuraState state;
    if (!player)
        return state;
    state.online = true;
    state.bot = GET_PLAYERBOT_AI(player) && !IsSelfBot(player);
    state.alive = player->IsAlive();
    state.ghost = player->HasPlayerFlag(PLAYER_FLAGS_GHOST);
    state.taxi = player->IsInFlight();
    state.vehicle = player->GetVehicle() != nullptr;
    state.battleground = player->InBattleground() || player->InArena()
        || (player->GetMap() && player->GetMap()->IsBattlegroundOrArena());
    state.teleporting = player->IsBeingTeleported();
    return state;
}

OwnerPetState PetStateOf(Player* player, Creature* pet)
{
    OwnerPetState state;
    if (!pet || !pet->IsInWorld() || pet->IsDuringRemoveFromWorld())
        return state;
    state.present = true;
    state.his = pet->GetOwnerGUID() == player->GetGUID();
    state.alive = pet->IsAlive();
    state.charmed = !pet->GetCharmerGUID().IsEmpty() && pet->GetCharmerGUID() != player->GetGUID();
    return state;
}

// His controlled pet, then the guardians he summoned (Mirror Image, a ghoul, treants): never a totem, a
// companion or a unit he charms, which are not guardians.
std::vector<ObjectGuid> PetsOf(Player* player)
{
    std::vector<std::uint64_t> guardians;
    for (Unit* unit : player->m_Controlled)
        if (unit && unit->IsGuardian())
            guardians.push_back(unit->GetGUID().GetRawValue());
    Pet* pet = player->GetPet();
    std::vector<ObjectGuid> found;
    for (std::uint64_t raw : OwnerPetTargets(pet ? pet->GetGUID().GetRawValue() : 0, guardians))
        found.emplace_back(raw);
    return found;
}

// Cast by the unit on itself, as a GM aura is; true when it stayed on.
bool Put(Unit* unit, std::uint32_t spell)
{
    Aura* aura = unit->AddAura(spell, unit);
    return aura && !aura->IsRemoved() && unit->HasAura(spell);
}

void TickOwn(std::uint32_t diffMs)
{
    std::vector<std::uint32_t> const listed = own.Due(diffMs);
    if (listed.empty())
        return;
    Player* player = FindOnline(HumanGuid());
    std::vector<std::uint32_t> const missing = OwnerAurasToApply(listed, StateOf(player),
        [player](std::uint32_t spell) { return player->HasAura(spell); });
    if (missing.empty())
        return;

    std::uint64_t const now = NowMs();
    for (std::uint32_t spell : missing)
    {
        OwnerAuraWhy const why = own.Why(spell);
        bool const on = Put(player, spell);
        if (on)
            own.Restored(spell);
        if (ownGate.Allow(spell, !on, now))
        {
            if (on)
                LOG_INFO("module.leeroy", "owner_aura_reapplied spell={} why={}", spell, OwnerAuraWhyName(why));
            else
                LOG_INFO("module.leeroy", "owner_aura_refused spell={} why={}", spell, OwnerAuraWhyName(why));
        }
    }
}

void TickPets(std::uint32_t diffMs)
{
    std::vector<std::uint32_t> const listed = pets.Due(diffMs);
    if (listed.empty())
        return;
    Player* player = FindOnline(HumanGuid());
    OwnerAuraState const owner = StateOf(player);
    if (!OwnerAuraBlock(owner).empty())
        return;

    std::uint64_t const now = NowMs();
    ObjectGuid const controlled = player->GetPetGUID();
    for (ObjectGuid const& guid : PetsOf(player))
    {
        // Looked up again for each unit: a pass never holds a pointer across an AddAura.
        Creature* pet = ObjectAccessor::GetCreatureOrPetOrVehicle(*player, guid);
        std::vector<std::uint32_t> const missing = OwnerPetAurasToApply(listed, owner, PetStateOf(player, pet),
            [pet](std::uint32_t spell) { return pet->HasAura(spell); });
        OwnerAuraLogGate& gate = guid == controlled ? petGate : guardianGate;
        for (std::uint32_t spell : missing)
        {
            OwnerAuraWhy const why = pets.Why(spell);
            bool const on = Put(pet, spell);
            if (on)
                pets.Restored(spell);
            if (gate.Allow(spell, !on, now))
            {
                if (on)
                    LOG_INFO("module.leeroy", "owner_pet_aura_reapplied spell={} pet_entry={} why={}", spell,
                        pet->GetEntry(), OwnerAuraWhyName(why));
                else
                    LOG_INFO("module.leeroy", "owner_pet_aura_refused spell={} pet_entry={} why={}", spell,
                        pet->GetEntry(), OwnerAuraWhyName(why));
            }
        }
    }
}

// Map thread (or world thread): never re-applies inside a removal, only notes it and wakes the next tick.
void AuraRemoved(Unit* unit, AuraApplication* app, AuraRemoveMode mode)
{
    if ((!own.Any() && !pets.Any()) || !unit || !app || !app->GetBase() || !unit->IsInWorld()
        || unit->IsDuringRemoveFromWorld())
        return;
    if (Player* player = unit->ToPlayer())
    {
        if (own.Any() && player->GetGUID().GetCounter() == HumanGuid())
            own.Note(app->GetBase()->GetId(), WhyOf(mode));
        return;
    }
    if (pets.Any() && unit->IsGuardian() && IsHis(unit->GetOwnerGUID()))
        pets.Note(app->GetBase()->GetId(), WhyOf(mode));
}

// Map thread: a pet or guardian of his just summoned, resummoned or loaded after a teleport.
void PetArrived(Unit* pet)
{
    if (pets.Any() && pet && IsHis(pet->GetOwnerGUID()))
        pets.Wake();
}

class OwnerAuraUnitScript : public UnitScript
{
public:
    OwnerAuraUnitScript() : UnitScript("LeeroyOwnerAuraUnitScript", true, {UNITHOOK_ON_AURA_REMOVE}) { }

    void OnAuraRemove(Unit* unit, AuraApplication* app, AuraRemoveMode mode) override { AuraRemoved(unit, app, mode); }
};

class OwnerPetAuraScript : public PetScript
{
public:
    OwnerPetAuraScript() : PetScript("LeeroyOwnerPetAuraScript",
        {PETHOOK_ON_INIT_STATS_FOR_LEVEL, PETHOOK_ON_PET_ADD_TO_WORLD}) { }

    void OnInitStatsForLevel(Guardian* guardian, uint8 /*petlevel*/) override { PetArrived(guardian); }
    void OnPetAddToWorld(Pet* pet) override { PetArrived(pet); }
};
}

void ReloadOwnerAuras()
{
    own.Reload();
    pets.Reload();
}

void TickOwnerAuras(std::uint32_t diffMs)
{
    TickOwn(diffMs);
    TickPets(diffMs);
}

void AddOwnerAuraScripts()
{
    new OwnerAuraUnitScript();
    new OwnerPetAuraScript();
}
}
