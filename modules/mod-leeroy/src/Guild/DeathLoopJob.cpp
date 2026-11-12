// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "DeathLoopJob.h"
#include "GuildPorts.h"
#include "LeeroyDeathLoopPolicy.h"
#include "Realm/RealmModule.h"
#include "Training/TrainingCommands.h"
#include "OfflineRaidJob.h"
#include "Config.h"
#include "DBCStores.h"
#include "Log.h"
#include "Map.h"
#include "MapMgr.h"
#include "Playerbots.h"
#include "PlayerbotAI.h"
#include "MotionMaster.h"
#include "RandomPlayerbotMgr.h"
#include <chrono>
#include <map>
#include <set>

namespace Leeroy::Guild
{
namespace
{
bool enabled = false;
std::uint32_t untilPass = 0, ownerGuild = 0, ownerHuman = 0;
std::map<std::uint32_t, DeathLoopHistory> histories; // World thread only.
DeathRescueBudget budget;
std::set<std::pair<std::uint32_t,std::string>> refusals;
bool trainingReported = false;

void Refused(std::uint32_t guid, std::string const& reason)
{
    if (refusals.emplace(guid,reason).second)
        LOG_INFO("module.leeroy", "cast_death_loop_refused guid={} reason={}", guid, reason);
}
}

void ReloadDeathLoop()
{
    enabled = sConfigMgr->GetOption<bool>("Leeroy.CastDeathLoop.Enable", false);
    // Reload never resets deaths, the rolling budget or per-member cooldowns.
    untilPass = 0;
}

void TickDeathLoop(std::uint32_t diffMs)
{
    if (!enabled || !GuildEnabled() || !KeepCast()) return;
    if (diffMs < untilPass) { untilPass -= diffMs; return; }
    untilPass = 1000; // No catch-up bursts after a stalled world tick.
    if (ownerGuild != LeeroyModule::Instance().GuildId() || ownerHuman != HumanGuid())
    {
        ownerGuild = LeeroyModule::Instance().GuildId();
        ownerHuman = HumanGuid();
        histories.clear();
        refusals.clear();
    }
    auto members = CastMembers();
    std::set<std::uint32_t> current(members.begin(), members.end());
    std::erase_if(histories, [&](auto const& item) { return !current.contains(item.first); });
    std::erase_if(refusals, [&](auto const& item) { return !current.contains(item.first); });
    auto now = static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count());
    bool const trainingFence = Training::TrainingCleanupPending() || OfflineTrainingCleanupPending();
    if (trainingFence && !trainingReported)
        LOG_INFO("module.leeroy", "cast_death_loop_skipped reason=training_cleanup");
    trainingReported = trainingFence;
    for (auto guid : members)
    {
        auto& history = histories[guid];
        auto* bot = FindOnline(guid);
        DeathLoopState state;
        if (bot && GET_PLAYERBOT_AI(bot) && !IsSelfBot(bot))
        {
            state.cast = guid != ownerHuman && bot->GetGuildId() == ownerGuild && bot->GetLevel() >= 80;
            state.pinned = sRandomPlayerbotMgr.IsPinned(guid);
            state.online = true;
            state.grouped = bot->GetGroup() != nullptr;
            state.invited = bot->GetGroupInvite() != nullptr;
            state.instance = !bot->GetMap() || bot->GetMap()->Instanceable() || bot->HasPendingBind();
            state.battleground = bot->InBattleground();
            state.queued = bot->InBattlegroundQueue();
            state.owned = !Realm::RealmModule::Instance().MutationReason(guid).empty();
            state.dead = !bot->IsAlive();
            state.combat = bot->IsInCombat();
            state.transit = bot->IsBeingTeleported() || bot->IsInFlight() || bot->GetTransport();
            state.trading = bot->GetTradeData() != nullptr;
            state.duel = bot->duel != nullptr;
            state.map = bot->GetMapId();
            state.x = bot->GetPositionX(); state.y = bot->GetPositionY(); state.z = bot->GetPositionZ();
        }
        ObserveDeathLoop(history, state, now);
        if (!bot || !DeathLoopRescueReady(history,state,now)) continue;
        auto const* home = sMapStore.LookupEntry(bot->m_homebindMapId);
        if (!home || home->Instanceable() || !MapMgr::IsValidMapCoord(bot->m_homebindMapId,
            bot->m_homebindX, bot->m_homebindY, bot->m_homebindZ, 0.0f))
        { Refused(guid,"invalid_homebind"); continue; }
        if (auto deaths = ClaimDeathLoopRescue(history, state, now, budget))
        {
            bot->GetMotionMaster()->Clear();
            GET_PLAYERBOT_AI(bot)->Reset(true);
            if (bot->TeleportTo(bot->m_homebindMapId, bot->m_homebindX, bot->m_homebindY, bot->m_homebindZ, 0.0f))
                LOG_INFO("module.leeroy", "cast_death_loop_rescued guid={} deaths={}", guid, deaths);
            else Refused(guid,"teleport_refused");
        }
    }
}
}
