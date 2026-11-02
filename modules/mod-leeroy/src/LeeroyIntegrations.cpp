// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyIntegrations.h"
#include "LeeroyModule.h"

#include "ObjectGuid.h"
#include "RandomPlayerbotMgr.h"

#include <type_traits>
#include <unordered_set>

#if !defined(LEEROY_HAS_OLLAMA_FILTER) || !defined(LEEROY_HAS_DUNGEON_CLEAR)
#error "LEEROY_HAS_OLLAMA_FILTER and LEEROY_HAS_DUNGEON_CLEAR come from mod-leeroy.cmake; re-run cmake"
#endif

#if LEEROY_HAS_OLLAMA_FILTER
#include "mod-ollama-chat_dispatch.h"
#endif

#if LEEROY_HAS_DUNGEON_CLEAR
#include "LeeroyProtocol.h"
#include "LeeroyTankEdges.h"
#include "ObjectAccessor.h"
#include "Player.h"
#include "Playerbots.h"
#include "Ai/Dungeon/DungeonClear/Util/DcRun.h"
#include "Ai/Dungeon/DungeonClear/Util/DcStatusPublisher.h"
#include "TestRun/DcTestRunManager.h"
#include <cstdint>
#include <string>
#include <vector>

namespace
{
// World thread only.
Leeroy::TankEdges& DcTanks()
{
    static Leeroy::TankEdges tanks;
    return tanks;
}

void PublishDcStatus(ObjectGuid tank, std::string const& payload)
{
    bool enabled = false;
    bool paused = false;
    if (Player* bot = ObjectAccessor::FindConnectedPlayer(tank))
    {
        if (PlayerbotAI* botAI = GET_PLAYERBOT_AI(bot))
        {
            DcRunState const& run = DcRun::Of(botAI);
            enabled = run.enabled;
            paused = run.paused;
        }
    }
    LeeroyModule::Instance().Publish(
        Leeroy::EncodeDcStatus(Leeroy::NowUnixMs(), tank.GetCounter(), enabled, paused, payload), false);
}
}
#endif

static_assert(std::is_same_v<ObjectGuid::LowType, uint32>, "the cast registry's GUID set is the pin set");

uint32 LeeroyApplyPins()
{
    LeeroyModule& module = LeeroyModule::Instance();
    LeeroyConfig const& cfg = module.Config();
    bool const enabled = module.Enabled();
    std::unordered_set<ObjectGuid::LowType> const bots =
        enabled ? module.Cast().Guids() : std::unordered_set<ObjectGuid::LowType>();
    return sRandomPlayerbotMgr.SetPinnedBots(bots, enabled && cfg.keepCast, enabled && cfg.bringCastOnline);
}

bool LeeroyInstallOllamaFilter()
{
#if LEEROY_HAS_OLLAMA_FILTER
    // Runs on mod-ollama-chat's submitting thread and must not throw. The flags are read on every call, so
    // `.reload config` takes effect at once.
    OllamaDispatch_SetSpeakerFilter([](uint64_t raw)
    {
        try
        {
            LeeroyModule& module = LeeroyModule::Instance();
            return !(module.Enabled() && module.ExcludeFromOllama()
                && module.Cast().IsCast(ObjectGuid(raw).GetCounter()));
        }
        catch (...)
        {
            return true;
        }
    });
    return true;
#else
    return false;
#endif
}

bool LeeroyInstallDcObserver()
{
#if LEEROY_HAS_DUNGEON_CLEAR
    // The observer slot has a setter but no getter, and mod-dungeon-clear fills it in Addmod_dungeon_clearScripts
    // (DungeonClearModule.cpp:868-872), before any OnStartup. This replaces it with a wrapper that first does what
    // dungeon-clear's own observer did. Called from DcStatusPublisher::TickStatusPushes on the world thread.
    DcStatusPublisher::SetStatusObserver([](ObjectGuid tank, std::string const& payload)
    {
        DcTestRunManager::Instance().OnStatusPayload(tank, payload);

        if (LeeroyModule::Instance().Enabled())
            PublishDcStatus(tank, payload);
    });
    return true;
#else
    return false;
#endif
}

void LeeroyPollDcTanks()
{
#if LEEROY_HAS_DUNGEON_CLEAR
    std::vector<std::uint32_t> active;
    for (ObjectGuid const& tank : DcStatusPublisher::ActiveTanks())
        active.push_back(tank.GetCounter());
    Leeroy::TankEdges::Edges const edges = DcTanks().Update(active);
    for (std::uint32_t tank : edges.started)
        PublishDcStatus(ObjectGuid::Create<HighGuid::Player>(tank), "");
    // Off whatever the tank's own state says: nothing reports on it from here on, so a tank dropped from the list
    // while it was out of the world must not leave the mind believing in a run for good.
    for (std::uint32_t tank : edges.ended)
        LeeroyModule::Instance().Publish(Leeroy::EncodeDcStatus(Leeroy::NowUnixMs(), tank, false, false, ""), false);
#endif
}

void LeeroyResetDcTanks()
{
#if LEEROY_HAS_DUNGEON_CLEAR
    DcTanks().Reset();
#endif
}
