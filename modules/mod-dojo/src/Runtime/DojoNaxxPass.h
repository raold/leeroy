// This file is part of leeroy, released under the GNU GPL v2. See LICENSE.
#ifndef MOD_DOJO_NAXX_PASS_H
#define MOD_DOJO_NAXX_PASS_H

#include "AiObjectContext.h"
#include "Creature.h"
#include "Event.h"
#include "Group.h"
#include "InstanceScript.h"
#include "Map.h"
#include "ObjectMgr.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "Playerbots.h"
#include "RandomPlayerbotMgr.h"
#include "TemporarySummon.h"
#include "Guild/OfflineRaidJob.h"
#include "Ai/Dungeon/DungeonClear/Util/DcRun.h"
#include <array>
#include <algorithm>
#include <cmath>
#include <list>
#include <unordered_set>
#include <vector>

// Included only by the isolated training acceptance translation unit. The
// seven prerequisites are setup, never earned kills. After Ready no movement,
// combat, healing, skipping or encounter-state stimulus is supplied.
struct DojoNaxxPass
{
    std::string state = "none";
    bool control = false, skipped = false, engaged = false;
    uint32 elapsed = 0, skipMs = 0, engageMs = 0, passes = 0, setupMask = 0;
    std::vector<uint32> setupDeaths;
    std::vector<uint32> setupTrashDeaths;
    uint32 setupAddDeaths = 0;
    std::array<bool, 10> held{};
    std::array<std::array<std::array<bool, 3>, 2>, 10> strategies{};
    static constexpr std::array<uint32, 7> Prerequisites{3, 4, 5, 6, 7, 8, 10};
    // Between the Necro Knight pack to the north and Cavaliers to the south.
    // The whole three-yard roster footprint is outside their aggro ranges.
    static constexpr float SetupX = 2778.f, SetupY = -3312.f, SetupZ = 267.7f;

    bool PrepareApproach(Map* map, std::string& error)
    {
        // The upper Military Quarter approach and ramp preceding Razuvious.
        // A raid that defeated him already cleared these packs. Explicit spawn
        // IDs keep the lower Gothik corridor and Construct Quarter untouched.
        static constexpr std::array<std::pair<uint32, uint32>, 45> spawns{{
            {127638, 16145}, {127646, 16145}, {127647, 16145}, {127648, 16145},
            {127650, 16145}, {127651, 16145}, {127652, 16145}, {127653, 16146},
            {127654, 16146}, {127666, 16146}, {127667, 16146}, {127668, 16146},
            {127669, 16146}, {127672, 16146}, {127673, 16146}, {127674, 16146},
            {127675, 16146}, {127693, 16156}, {127694, 16156}, {127695, 16156},
            {127696, 16156}, {127697, 16156}, {127698, 16156}, {127699, 16156},
            {127700, 16156}, {127703, 16156}, {127704, 16156}, {127705, 16156},
            {127750, 16163}, {127751, 16163}, {127752, 16163}, {127755, 16164},
            {127756, 16164}, {127758, 16165}, {127759, 16165}, {127764, 16167},
            {127765, 16167}, {127766, 16167}, {127767, 16167}, {127768, 16167},
            {127769, 16167}, {127790, 16193}, {127791, 16193}, {127792, 16193},
            {127793, 16193}}};
        for (auto const& [spawnId, entry] : spawns)
        {
            auto const* data = sObjectMgr->GetCreatureData(spawnId);
            if (!data || data->mapid != 533 || data->id != entry || data->spawntimesecs <= 600)
            { error = "naxx_pass_approach_spawn_changed"; return false; }
            map->LoadGrid(data->posX, data->posY);
            auto const bounds = map->GetCreatureBySpawnIdStore().equal_range(spawnId);
            Creature* found = nullptr;
            for (auto it = bounds.first; it != bounds.second; ++it)
                if (it->second)
                {
                    if (found) { error = "naxx_pass_duplicate_approach_spawn"; return false; }
                    found = it->second;
                }
            if (!found || found->GetEntry() != entry || !found->IsAlive())
            { error = "naxx_pass_approach_not_fresh"; return false; }
            Unit::Kill(found, found, false);
            if (found->IsAlive()) { error = "naxx_pass_approach_survived"; return false; }
            setupTrashDeaths.push_back(spawnId);
        }
        return true;
    }

    bool PrepareDeaths(Map* map, std::string& error)
    {
        struct Spawn { uint32 entry; float x, y; };
        // Real spawn positions from the isolated world database. Load each
        // prerequisite before killing it: setting DONE alone leaves a live
        // Razuvious waiting on the return route and lets his AI reactivate it.
        static constexpr std::array<Spawn, 7> spawns{{
            {15956, 3308.59f, -3476.29f}, {15953, 3353.25f, -3620.10f},
            {15952, 3511.38f, -3921.58f}, {15954, 2671.65f, -3489.11f},
            {15936, 2793.86f, -3707.38f}, {16011, 2909.00f, -3997.41f},
            {16061, 2758.93f, -3107.12f}}};
        std::array<Creature*, 7> bosses{};
        for (auto const& spawn : spawns) map->LoadGrid(spawn.x, spawn.y);
        for (std::size_t i = 0; i < spawns.size(); ++i)
        {
            for (auto const& [spawnId, creature] : map->GetCreatureBySpawnIdStore())
                if (creature && creature->GetEntry() == spawns[i].entry)
                {
                    if (bosses[i]) { error = "naxx_pass_duplicate_prerequisite"; return false; }
                    bosses[i] = creature;
                }
            if (!bosses[i] || !bosses[i]->IsAlive())
            { error = "naxx_pass_prerequisite_not_fresh"; return false; }
        }
        std::list<Creature*> understudies;
        bosses.back()->GetCreatureListWithEntryInGrid(understudies, 16803, 100.f);
        for (auto* add : understudies)
            if (auto* summoned = add->ToTempSummon(); summoned
                && summoned->GetSummonerGUID() == bosses.back()->GetGUID() && add->IsAlive())
            {
                Unit::Kill(add, add, false);
                if (add->IsAlive()) { error = "naxx_pass_setup_add_survived"; return false; }
                ++setupAddDeaths;
            }
        for (auto* boss : bosses)
        {
            Unit::Kill(boss, boss, false); // Setup, without player kill/loot credit.
            if (boss->IsAlive()) { error = "naxx_pass_prerequisite_survived"; return false; }
            setupDeaths.push_back(boss->GetEntry());
        }
        return true;
    }

    bool Release(std::array<Player*, 10> const& actors)
    {
        for (std::size_t i = 0; i < actors.size(); ++i)
            if (held[i])
            {
                bool restored = true;
                if (actors[i])
                    for (std::size_t s = 0; s < 2; ++s)
                    {
                        auto* ai = GET_PLAYERBOT_AI(actors[i]);
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
        if (state == "none" || !error.empty()) return;
        if (!native.value("active", false)) { Release(actors); return; }
        if (native.value("stage", "") != "running") return;
        elapsed += diff;
        auto* leader = actors[0];
        auto* instance = leader ? leader->GetInstanceScript() : nullptr;
        if (!leader || leader->GetMapId() != 533 || !instance || instance->GetEncounterCount() != 15) return;
        auto* ai = GET_PLAYERBOT_AI(leader);
        if (state == "ready")
        {
            for (uint32 spawnId : setupTrashDeaths)
            {
                auto const bounds = leader->GetMap()->GetCreatureBySpawnIdStore().equal_range(spawnId);
                for (auto it = bounds.first; it != bounds.second; ++it)
                    if (it->second && it->second->IsAlive())
                    { error = "naxx_pass_approach_respawned"; return; }
            }
            for (uint32 i : Prerequisites)
                if (instance->GetBossState(i) != DONE)
                { error = "naxx_pass_prerequisite_reactivated"; return; }
            passes = native.value("route_passes", 0u);
            auto const& skips = ai->GetAiObjectContext()->GetValue<std::unordered_set<uint32>&>(DcKey::Skipped)->Get();
            if (!skipped && skips.count(16060)) { skipped = true; skipMs = elapsed; }
            auto* patchwerk = instance->GetCreature(100); // Naxx DATA_PATCHWERK_BOSS, not boss-state slot 0.
            if (!engaged && instance->GetBossState(0) == IN_PROGRESS && patchwerk
                && patchwerk->GetEntry() == 16028 && patchwerk->IsAlive() && patchwerk->IsInCombat())
            { engaged = true; engageMs = elapsed; }
            return;
        }
        if (elapsed > 10000) { error = "naxx_pass_setup_timeout"; Release(actors); return; }
        for (auto* p : actors)
            if (!p || !p->IsAlive() || p->IsBeingTeleported() || p->GetMap() != leader->GetMap()
                || !Leeroy::Guild::OfflineRaidOwns(p->GetGUID().GetCounter())
                || !sRandomPlayerbotMgr.IsPinned(p->GetGUID().GetCounter())) return;
        for (auto* p : actors)
            if (p->IsInCombat()) { error = "naxx_pass_setup_in_combat"; Release(actors); return; }
        auto* driver = ai->GetMaster();
        if (!driver) { error = "naxx_pass_driver_missing"; Release(actors); return; }
        if (state == "pending")
        {
            if (native.value("stage_ms", 0u) > 2000 || instance->GetCompletedEncounterMask())
            { error = "naxx_pass_not_fresh"; return; }
            for (uint32 i = 0; i < 15; ++i)
                if (instance->GetBossState(i) != NOT_STARTED) { error = "naxx_pass_already_started"; return; }
            ai->DoSpecificAction("dc off", Event("dc", "", driver), true);
            if (DcRun::Of(ai).enabled) { error = "naxx_pass_driver_not_stopped"; return; }
            for (std::size_t i = 0; i < actors.size(); ++i)
            {
                auto* memberAI = GET_PLAYERBOT_AI(actors[i]);
                for (std::size_t s = 0; s < 2; ++s)
                {
                    auto const engine = s ? BOT_STATE_NON_COMBAT : BOT_STATE_COMBAT;
                    std::array<char const*, 3> const names{"stay", "passive", "follow"};
                    for (std::size_t f = 0; f < names.size(); ++f)
                        strategies[i][s][f] = memberAI->HasStrategy(names[f], engine);
                    memberAI->ChangeStrategy("+stay,+passive,-follow", engine);
                }
                held[i] = true;
                actors[i]->StopMoving();
            }
            if (!PrepareApproach(leader->GetMap(), error) || !PrepareDeaths(leader->GetMap(), error))
            { Release(actors); return; }
            for (uint32 i : Prerequisites) instance->SetBossState(i, DONE);
            // DBC order differs from script indices: Anub, Faerlina, Maexxna,
            // Noth, Heigan, Loatheb, Razuvious occupy encounter-mask bits 0..6.
            setupMask = 0x7f;
            instance->SetCompletedEncountersMask(setupMask, false);
            if (!Leeroy::Guild::DojoPrimeNaxxStates(token))
            { error = "naxx_pass_prerequisites_refused"; Release(actors); return; }
            leader->GetMap()->LoadGrid(SetupX, SetupY);
            for (std::size_t i = 0; i < actors.size(); ++i)
                actors[i]->NearTeleportTo(SetupX + float(i % 3), SetupY + float(i / 3), SetupZ, 3.14f);
            state = "arriving"; elapsed = 0;
        }
        else if (elapsed >= 1000)
        {
            for (auto* p : actors)
                if (std::abs(p->GetPositionZ() - SetupZ) > 5.f
                    || std::hypot(p->GetPositionX() - SetupX, p->GetPositionY() - SetupY) > 15.f) return;
            if (!Release(actors)) { error = "naxx_pass_strategies_not_restored"; return; }
            ai->DoSpecificAction("dc on", Event("dc", "", driver), true);
            if (!DcRun::Of(ai).enabled) { error = "naxx_pass_driver_not_resumed"; return; }
            state = "ready"; elapsed = 0;
        }
    }

    nlohmann::json Snapshot() const
    {
        return {{"state", state}, {"control", control}, {"measured_ms", elapsed},
            {"gothik_skipped", skipped}, {"skip_ms", skipMs}, {"patchwerk_engaged", engaged},
            {"engage_ms", engageMs}, {"passes", passes}, {"setup_completed_mask", setupMask},
            {"setup_boss_deaths", setupDeaths}, {"setup_add_deaths", setupAddDeaths},
            {"setup_trash_deaths", setupTrashDeaths},
            {"setup_bosses", Prerequisites},
            {"strategies_restored", std::none_of(held.begin(), held.end(), [](bool value) { return value; })}};
    }
};
#endif
