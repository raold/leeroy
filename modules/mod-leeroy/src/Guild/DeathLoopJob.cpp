// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "DeathLoopJob.h"
#include "GuildPorts.h"
#include "LeeroyDeathLoopPolicy.h"
#include "Realm/RealmModule.h"
#include "Config.h"
#include "DBCStores.h"
#include "Log.h"
#include "Map.h"
#include "MapMgr.h"
#include "Playerbots.h"
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
}

void ReloadDeathLoop()
{
    enabled = sConfigMgr->GetOption<bool>("Leeroy.CastDeathLoop.Enable", false);
    // Keep the attempt cooldown across disable/re-enable, but discard observations.
    for (auto& [guid, history] : histories) history.deaths.clear();
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
    }
    auto members = CastMembers();
    std::set<std::uint32_t> current(members.begin(), members.end());
    std::erase_if(histories, [&](auto const& item) { return !current.contains(item.first); });
    auto now = static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count());
    unsigned attempted = 0;
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
            state.grouped = bot->GetGroup() || bot->GetGroupInvite();
            state.instance = !bot->GetMap() || bot->GetMap()->Instanceable() || bot->HasPendingBind();
            state.battleground = bot->InBattleground() || bot->InBattlegroundQueue();
            state.owned = !Realm::RealmModule::Instance().MutationReason(guid).empty();
            state.dead = !bot->IsAlive();
            state.combat = bot->IsInCombat();
            state.transit = bot->IsBeingTeleported() || bot->IsInFlight() || bot->GetTransport();
            state.trading = bot->GetTradeData() != nullptr;
            state.duel = bot->duel != nullptr;
        }
        ObserveDeathLoop(history, state, now);
        if (!bot) continue;
        auto const* home = sMapStore.LookupEntry(bot->m_homebindMapId);
        if (!home || home->Instanceable() || !MapMgr::IsValidMapCoord(bot->m_homebindMapId,
            bot->m_homebindX, bot->m_homebindY, bot->m_homebindZ, 0.0f)) continue;
        if (auto deaths = ClaimDeathLoopRescue(history, state, now, attempted))
            if (bot->TeleportTo(bot->m_homebindMapId, bot->m_homebindX, bot->m_homebindY, bot->m_homebindZ, 0.0f))
                LOG_INFO("module.leeroy", "cast_death_loop_rescued guid={} deaths={}", guid, deaths);
    }
}
}
