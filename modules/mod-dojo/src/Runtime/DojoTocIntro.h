// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_DOJO_TOC_INTRO_H
#define MOD_DOJO_TOC_INTRO_H

#include "Creature.h"
#include "AccountMgr.h"
#include "CharacterCache.h"
#include "InstanceScript.h"
#include "Map.h"
#include "ObjectMgr.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "Playerbots.h"
#include "TemporarySummon.h"
#include "Guild/OfflineRaidJob.h"
#include "Ai/Dungeon/DungeonClear/Data/Events/DungeonEventTables.h"
#include <algorithm>
#include <array>

// Prepare the completed arena, then let the real floor-break scene, dungeon-clear
// descent and chamber driver run. No player movement or boss action is supplied
// after the native Anub'arak has spawned and measurement begins.
struct DojoTocIntro
{
    std::string state = "none";
    uint32 setupMs = 0, measuredMs = 0, samples = 0, maxGap = 0, triggerSamples = 0, emergeMs = 0, engageMs = 0;
    bool initiallyBlocked = false, emerged = false, engaged = false, nativeSpawn = false;
    ObjectGuid bossGuid;
    std::array<bool, 10> held{};
    std::array<uint64, 10> epochs{};
    std::array<std::array<std::array<bool, 3>, 2>, 10> strategies{};

    bool Release(std::array<Player*, 10> const& actors)
    {
        for (std::size_t i = 0; i < actors.size(); ++i)
            if (held[i])
            {
                auto* ai = actors[i] ? GET_PLAYERBOT_AI(actors[i]) : nullptr;
                bool restored = ai != nullptr;
                if (ai && epochs[i] && ai->ReleaseExternalControl(epochs[i])) epochs[i] = 0;
                restored = restored && !epochs[i];
                if (ai)
                    for (std::size_t s = 0; s < 2; ++s)
                    {
                        auto const engine = s ? BOT_STATE_NON_COMBAT : BOT_STATE_COMBAT;
                        std::array<char const*, 3> const names{"stay", "passive", "follow"};
                        for (std::size_t f = 0; f < names.size(); ++f)
                        {
                            bool const want = strategies[i][s][f];
                            ai->ChangeStrategy(std::string(want ? "+" : "-") + names[f], engine);
                            restored = restored && ai->HasStrategy(names[f], engine) == want;
                        }
                    }
                held[i] = !restored;
            }
        return std::none_of(held.begin(), held.end(), [](bool value) { return value; });
    }

    void Tick(std::array<Player*, 10> const& actors, nlohmann::json const& native,
        std::string const& token, std::string& error, uint32 diff)
    {
        using namespace DcTrialOfTheCrusader;
        if (state == "none") return;
        if (!error.empty() || !native.value("active", false) || native.value("stage", "") == "tearing_down")
        { Release(actors); return; }
        if (native.value("stage", "") != "running") return;
        auto* leader = actors[0];
        auto* instance = leader ? leader->GetInstanceScript() : nullptr;
        if (!leader || leader->GetMapId() != MAP_ID || !instance || instance->GetEncounterCount() != BOSS_COUNT)
        { error = "toc_intro_instance_missing"; return; }
        for (auto* p : actors)
            if (!p || !p->IsAlive() || !GET_PLAYERBOT_AI(p) || p->GetMap() != leader->GetMap()
                || !Leeroy::Guild::OfflineRaidOwns(p->GetGUID().GetCounter()))
            { error = "toc_intro_actor_lost_or_dead"; return; }
        if (state == "ready")
        {
            measuredMs += diff; ++samples; maxGap = std::max(maxGap, diff);
            auto* boss = instance->GetCreature(DATA_ANUBARAK);
            if (!boss || boss->GetGUID() != bossGuid || !boss->IsAlive())
            { error = "toc_intro_native_boss_lost"; return; }
            for (uint32 slot = 0; slot < BOSS_ANUBARAK; ++slot)
                if (instance->GetBossState(slot) != DONE)
                { error = "toc_intro_prerequisite_reactivated"; return; }
            if (auto const* trigger = sObjectMgr->GetAreaTrigger(5475); trigger && leader->IsInAreaTriggerRadius(trigger))
                ++triggerSamples;
            if (!emerged && leader->IsValidAttackTarget(boss)) { emerged = true; emergeMs = measuredMs; }
            if (!engaged && boss->IsInCombat() && instance->GetBossState(BOSS_ANUBARAK) == IN_PROGRESS)
            { engaged = true; engageMs = measuredMs; }
            if (measuredMs > 120000) error = "toc_intro_surface_or_pull_timeout";
            return;
        }
        setupMs += diff;
        if (setupMs > 180000) { error = "toc_intro_scene_timeout"; Release(actors); return; }
        if (state == "pending")
        {
            if (native.value("stage_ms", 0u) > 2000 || instance->IsEncounterInProgress())
            { error = "toc_intro_not_fresh"; return; }
            for (std::size_t i = 0; i < actors.size(); ++i)
            {
                auto* memberAI = GET_PLAYERBOT_AI(actors[i]);
                auto* driver = memberAI->GetMaster();
                uint32 const account = AccountMgr::GetId("DOJODRIVER");
                if (!driver || !account || sCharacterCache->GetCharacterAccountIdByGuid(driver->GetGUID()) != account)
                { error = "toc_intro_driver_missing"; Release(actors); return; }
                for (std::size_t s = 0; s < 2; ++s)
                {
                    auto const engine = s ? BOT_STATE_NON_COMBAT : BOT_STATE_COMBAT;
                    std::array<char const*, 3> const names{"stay", "passive", "follow"};
                    for (std::size_t f = 0; f < names.size(); ++f)
                        strategies[i][s][f] = memberAI->HasStrategy(names[f], engine);
                    memberAI->ChangeStrategy("+stay,+passive,-follow", engine);
                }
                held[i] = true;
                // Keep the real job and DC run enabled during the native scene.
                // dc off clears masters and triggers the ordinary restart path.
                // This setup-only fence belongs solely to the synthetic actors.
                memberAI->SetMaster(nullptr);
                epochs[i] = memberAI->AcquireExternalControl("dojo-toc-intro");
                memberAI->SetMaster(driver);
                if (!epochs[i]) { error = "toc_intro_hold_refused"; Release(actors); return; }
                actors[i]->StopMoving();
            }
            // Earlier encounters are prepared save state. Do not schedule their
            // old speeches, loot or kill credit alongside the real Twins outro.
            for (uint32 slot = 0; slot < BOSS_TWIN_VALKYR; ++slot)
                instance->InstanceScript::SetBossState(slot, DONE);
            // A fresh boss starts TO_BE_DECIDED. Its first state assignment is
            // treated as save loading and deliberately suppresses core scenes.
            instance->InstanceScript::SetBossState(BOSS_TWIN_VALKYR, NOT_STARTED);
            if (!instance->SetBossState(BOSS_TWIN_VALKYR, DONE))
            { error = "toc_intro_scene_not_scheduled"; Release(actors); return; }
            instance->SetBossState(BOSS_ANUBARAK, NOT_STARTED);
            instance->SetCompletedEncountersMask(0x0f, false);
            if (!Leeroy::Guild::DojoPrimeTocStates(token))
            { error = "toc_intro_prerequisites_refused"; Release(actors); return; }
            state = "scene";
        }
        if (instance->GetData(TYPE_INSTANCE_PROGRESS) != PROGRESS_ANUB_ARAK) return;
        auto* boss = instance->GetCreature(DATA_ANUBARAK);
        auto* summon = boss ? boss->ToTempSummon() : nullptr;
        constexpr uint32 TirionData = 1; // TrialOfTheCrusaderObjectData::DATA_TIRION.
        auto* tirion = instance->GetCreature(TirionData);
        if (!boss || !boss->IsAlive() || !summon || !tirion || summon->GetSummonerGUID() != tirion->GetGUID())
        { error = "toc_intro_not_native_summon"; Release(actors); return; }
        nativeSpawn = true; bossGuid = boss->GetGUID();
        initiallyBlocked = !leader->IsValidAttackTarget(boss);
        if (!initiallyBlocked || !Release(actors)) { error = "toc_intro_setup_not_blocked_or_restored"; return; }
        state = "ready";
    }

    nlohmann::json Snapshot() const
    {
        return {{"state", state}, {"setup_ms", setupMs}, {"measured_ms", measuredMs}, {"samples", samples},
            {"max_gap_ms", maxGap}, {"trigger_samples", triggerSamples}, {"initially_blocked", initiallyBlocked},
            {"native_spawn", nativeSpawn}, {"emerged", emerged}, {"emerge_ms", emergeMs},
            {"engaged", engaged}, {"engage_ms", engageMs}, {"setup_bosses", {0, 1, 2, 3}},
            {"strategies_restored", std::none_of(held.begin(), held.end(), [](bool value) { return value; })}};
    }
};
#endif
