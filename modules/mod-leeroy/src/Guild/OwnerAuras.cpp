// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "OwnerAuras.h"
#include "GuildPorts.h"
#include "LeeroyOwnerAuras.h"
#include "Config.h"
#include "Log.h"
#include "Map.h"
#include "Player.h"
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
// World thread: the configured ids, checked against the spell store on the first tick after a load
// (at startup the config loads before the spells do).
std::vector<std::uint32_t> configured;
bool checked = true;
bool wasOn = false;
OwnerAuraPass passTimer;
OwnerAuraLogGate gate;

// The aura-remove hook runs on map threads: it reads the checked list and notes reasons under this lock.
std::mutex hookLock;
std::vector<std::uint32_t> spells;
OwnerAuraReasons reasons;
std::atomic<bool> anyListed{false};
std::atomic<bool> woken{false};

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

void Check()
{
    checked = true;
    OwnerAuraChecked const result = CheckOwnerAuras(configured, Lookup);
    for (auto const& [spell, why] : result.invalid)
        LOG_WARN("module.leeroy", "owner_aura_invalid spell={} reason={}", spell, OwnerAuraSpellName(why));
    {
        std::lock_guard<std::mutex> guard(hookLock);
        spells = result.spells;
        reasons.Clear();
    }
    anyListed.store(!result.spells.empty());
    if (!result.spells.empty())
    {
        std::string list;
        for (std::uint32_t spell : result.spells)
            list += (list.empty() ? "" : ",") + std::to_string(spell);
        LOG_INFO("module.leeroy", "owner_auras spells={}", list);
    }
    else if (wasOn)
        LOG_INFO("module.leeroy", "owner_auras off");
    wasOn = !result.spells.empty();
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
    state.alive = player->IsAlive();
    state.ghost = player->HasPlayerFlag(PLAYER_FLAGS_GHOST);
    state.taxi = player->IsInFlight();
    state.vehicle = player->GetVehicle() != nullptr;
    state.battleground = player->InBattleground() || player->InArena()
        || (player->GetMap() && player->GetMap()->IsBattlegroundOrArena());
    state.teleporting = player->IsBeingTeleported();
    return state;
}

// Map thread (or world thread): never re-applies inside a removal, only notes it and wakes the next tick.
void AuraRemoved(Unit* unit, AuraApplication* app, AuraRemoveMode mode)
{
    if (!anyListed.load(std::memory_order_relaxed) || !unit || !app || !app->GetBase())
        return;
    Player* player = unit->ToPlayer();
    if (!player || player->GetGUID().GetCounter() != HumanGuid() || !player->IsInWorld()
        || player->IsDuringRemoveFromWorld())
        return;
    std::uint32_t const spell = app->GetBase()->GetId();
    {
        std::lock_guard<std::mutex> guard(hookLock);
        if (std::find(spells.begin(), spells.end(), spell) == spells.end())
            return;
        reasons.Note(spell, WhyOf(mode));
    }
    woken.store(true);
}

class OwnerAuraUnitScript : public UnitScript
{
public:
    OwnerAuraUnitScript() : UnitScript("LeeroyOwnerAuraUnitScript", true, {UNITHOOK_ON_AURA_REMOVE}) { }

    void OnAuraRemove(Unit* unit, AuraApplication* app, AuraRemoveMode mode) override { AuraRemoved(unit, app, mode); }
};
}

void ReloadOwnerAuras()
{
    OwnerAuraList const list = ParseOwnerAuras(sConfigMgr->GetOption<std::string>("Leeroy.OwnerAuras", ""));
    for (std::string const& entry : list.rejected)
        LOG_WARN("module.leeroy", "owner_aura_invalid entry=\"{}\" reason=not_a_spell_id", entry);
    configured = list.spells;
    checked = false;
}

void TickOwnerAuras(std::uint32_t diffMs)
{
    if (!checked)
        Check();
    if (!anyListed.load() || !GuildEnabled())
        return;
    if (!passTimer.Due(diffMs, woken.exchange(false)))
        return;

    Player* player = FindOnline(HumanGuid());
    std::vector<std::uint32_t> listed;
    {
        std::lock_guard<std::mutex> guard(hookLock);
        listed = spells;
    }
    std::vector<std::uint32_t> const missing = OwnerAurasToApply(listed, StateOf(player),
        [player](std::uint32_t spell) { return player->HasAura(spell); });
    if (missing.empty())
        return;

    std::uint64_t const now = NowMs();
    for (std::uint32_t spell : missing)
    {
        OwnerAuraWhy why = OwnerAuraWhy::Unknown;
        {
            std::lock_guard<std::mutex> guard(hookLock);
            why = reasons.Peek(spell);
        }
        Aura* aura = player->AddAura(spell, player);
        bool const on = aura && !aura->IsRemoved() && player->HasAura(spell);
        if (on)
        {
            std::lock_guard<std::mutex> guard(hookLock);
            reasons.Forget(spell);
        }
        if (gate.Allow(spell, !on, now))
        {
            if (on)
                LOG_INFO("module.leeroy", "owner_aura_reapplied spell={} why={}", spell, OwnerAuraWhyName(why));
            else
                LOG_INFO("module.leeroy", "owner_aura_refused spell={} why={}", spell, OwnerAuraWhyName(why));
        }
    }
}

void AddOwnerAuraScripts()
{
    new OwnerAuraUnitScript();
}
}
