// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_DOJO_STALL_H
#define MOD_DOJO_STALL_H

#include "Player.h"
#include "AccountMgr.h"
#include "CharacterCache.h"
#include "PlayerbotAI.h"
#include "Playerbots.h"
#include "Guild/OfflineRaidJob.h"
#include <algorithm>
#include <array>

// A recorded plateau stimulus: hold only the ten owned synthetic actors through
// the existing external-control fence. The real job's clock, stop and teardown
// still run; no timestamp or production timeout is shortened by the fixture.
struct DojoStall
{
    std::string state = "none";
    std::array<uint64, 10> epochs{};
    uint32 heldMs = 0, samples = 0, maxGap = 0;

    template<class Command> static bool Configure(Command& cmd)
    {
        if constexpr (requires { cmd.stallTimeoutMin; })
        { cmd.stallTimeoutMin = 10; return true; }
        return false;
    }

    bool Release(std::array<Player*, 10> const& actors)
    {
        for (std::size_t i = 0; i < actors.size(); ++i)
            if (epochs[i] && actors[i])
                if (auto* ai = GET_PLAYERBOT_AI(actors[i]); ai && ai->ReleaseExternalControl(epochs[i]))
                    epochs[i] = 0;
        return std::none_of(epochs.begin(), epochs.end(), [](uint64 value) { return value != 0; });
    }

    void Tick(std::array<Player*, 10> const& actors, nlohmann::json const& native,
        std::string& error, uint32 diff)
    {
        if (state == "none") return;
        if (!error.empty() || !native.value("active", false) || native.value("stage", "") == "tearing_down")
        { Release(actors); return; }
        if (native.value("stage", "") != "running") return;
        for (auto* p : actors)
            if (!p || !p->IsAlive() || !GET_PLAYERBOT_AI(p) || p->IsInCombat()
                || p->GetMapId() != 649 || !Leeroy::Guild::OfflineRaidOwns(p->GetGUID().GetCounter()))
            { error = "native_stall_plateau_actor_changed"; Release(actors); return; }
        if (state == "pending")
        {
            if (native.value("stage_ms", 0u) > 2000)
            { error = "native_stall_plateau_not_fresh"; return; }
            for (std::size_t i = 0; i < actors.size(); ++i)
            {
                auto* ai = GET_PLAYERBOT_AI(actors[i]);
                auto* driver = ai->GetMaster();
                uint32 const account = AccountMgr::GetId("DOJODRIVER");
                if (!driver || !account || sCharacterCache->GetCharacterAccountIdByGuid(driver->GetGUID()) != account)
                { error = "native_stall_plateau_driver_changed"; Release(actors); return; }
                // The synthetic driver looks like a game client to playerbots.
                // Detach only during this setup call, then restore it immediately;
                // the real job retains its normal driver for the entire measurement.
                ai->SetMaster(nullptr);
                epochs[i] = ai->AcquireExternalControl("dojo-offline-plateau");
                ai->SetMaster(driver);
                if (!epochs[i]) { error = "native_stall_plateau_hold_refused"; Release(actors); return; }
                actors[i]->StopMoving();
            }
            state = "held";
        }
        heldMs += diff; ++samples; maxGap = std::max(maxGap, diff);
        for (auto* p : actors)
            if (!GET_PLAYERBOT_AI(p)->IsExternallyControlled())
            { error = "native_stall_plateau_hold_lost"; Release(actors); return; }
        if (heldMs > 610000) { error = "native_stall_did_not_stop"; Release(actors); }
    }

    nlohmann::json Snapshot() const
    {
        return {{"state", state}, {"held_ms", heldMs}, {"samples", samples}, {"max_gap_ms", maxGap},
            {"limit_ms", 600000}, {"released", std::none_of(epochs.begin(), epochs.end(), [](uint64 v) { return v != 0; })}};
    }
};
#endif
