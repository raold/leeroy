// This file is part of leeroy, released under the GNU GPL v2. See LICENSE.
#ifndef MOD_DOJO_NAXX_PASS_H
#define MOD_DOJO_NAXX_PASS_H

#include "AiObjectContext.h"
#include "Creature.h"
#include "Event.h"
#include "Group.h"
#include "InstanceScript.h"
#include "Map.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "Playerbots.h"
#include "RandomPlayerbotMgr.h"
#include "Guild/OfflineRaidJob.h"
#include "Ai/Dungeon/DungeonClear/Util/DcRun.h"
#include <array>
#include <algorithm>
#include <cmath>
#include <unordered_set>

// Included only by the isolated training acceptance translation unit. The
// seven prerequisites are setup, never earned kills. After Ready no movement,
// combat, healing, skipping or encounter-state stimulus is supplied.
struct DojoNaxxPass
{
    std::string state = "none";
    bool control = false, skipped = false, engaged = false;
    uint32 elapsed = 0, skipMs = 0, engageMs = 0, passes = 0, setupMask = 0;
    std::array<bool, 10> held{};
    std::array<std::array<std::array<bool, 3>, 2>, 10> strategies{};
    static constexpr std::array<uint32, 7> Prerequisites{3, 4, 5, 6, 7, 8, 10};

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
            for (uint32 i : Prerequisites) instance->SetBossState(i, DONE);
            // DBC order differs from script indices: Anub, Faerlina, Maexxna,
            // Noth, Heigan, Loatheb, Razuvious occupy encounter-mask bits 0..6.
            setupMask = 0x7f;
            instance->SetCompletedEncountersMask(setupMask, false);
            if (!Leeroy::Guild::DojoPrimeNaxxStates(token))
            { error = "naxx_pass_prerequisites_refused"; Release(actors); return; }
            leader->GetMap()->LoadGrid(2778.f, -3294.f);
            for (std::size_t i = 0; i < actors.size(); ++i)
                actors[i]->NearTeleportTo(2778.f + float(i % 3), -3294.f + float(i / 3), 267.7f, 3.14f);
            state = "arriving"; elapsed = 0;
        }
        else if (elapsed >= 1000)
        {
            for (auto* p : actors)
                if (std::abs(p->GetPositionZ() - 267.7f) > 5.f
                    || std::hypot(p->GetPositionX() - 2778.f, p->GetPositionY() + 3294.f) > 15.f) return;
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
            {"setup_bosses", Prerequisites},
            {"strategies_restored", std::none_of(held.begin(), held.end(), [](bool value) { return value; })}};
    }
};
#endif
