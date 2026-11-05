// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "TrainingRivals.h"
#include "TrainingCommands.h"
#include "LeeroyTrainingProtocol.h"
#include "LeeroyRaidRecovery.h"
#include "OfflineRaidJob.h"
#include "GuildPorts.h"
#include "AiFactory.h"
#include "Group.h"
#include "LFGMgr.h"
#include "InstanceSaveMgr.h"
#include "Map.h"
#include "Playerbots.h"
#include "RandomPlayerbotMgr.h"
#include "WorldSession.h"
#if !defined(LEEROY_HAS_DUNGEON_CLEAR)
#error "LEEROY_HAS_DUNGEON_CLEAR comes from mod-leeroy.cmake; re-run cmake"
#endif
#if LEEROY_HAS_DUNGEON_CLEAR
#include "TestRun/DcTestRunManager.h"
#include "TestRun/DcTestDriver.h"
#endif
#include <algorithm>
#include <cmath>
#include <vector>

namespace Leeroy::Training
{
bool RivalIdentity(Player* player)
{
    return player && GET_PLAYERBOT_AI(player) && !IsSelfBot(player) && sRandomPlayerbotMgr.IsRandomBot(player)
        && !Guild::IsCast(player->GetGUID().GetCounter()) && player->GetGUID().GetCounter() != Guild::HumanGuid();
}

bool RivalAvailable(Player* player, std::uint32_t level, bool ownRaid)
{
    if (!player) return false;
    auto guid = player->GetGUID().GetCounter();
    auto* ai = GET_PLAYERBOT_AI(player);
    RivalState state{player->IsInWorld() && !player->IsDuringRemoveFromWorld() && player->GetSession(),
        ai != nullptr && !IsSelfBot(player), sRandomPlayerbotMgr.IsRandomBot(player), Guild::IsCast(guid),
        guid == Guild::HumanGuid(), player->GetGroup() != nullptr || player->GetGroupInvite() != nullptr,
        player->GetMap() && player->GetMap()->IsDungeon(), player->InBattleground() || player->InBattlegroundQueue(),
        sLFGMgr->GetState(player->GetGUID()) != lfg::LFG_STATE_NONE,
        player->isDead() || player->HasPlayerFlag(PLAYER_FLAGS_GHOST),
        player->IsInFlight() || player->GetTransport() || player->GetVehicle(),
        player->GetLevel(), static_cast<uint8>(player->GetTeamId())};
    if (!RivalUsable(state, level) || !ai || ai->GetMaster() || player->IsInCombat()
        || !player->GetSession() || player->GetSession()->IsLoggingOut()
        || player->IsBeingTeleported() || player->HasPendingBind() || player->GetTradeData() || DummyTrialOwns(guid)
        || !RivalBindingsSafe(player) || (!ownRaid && Guild::OfflineRaidOwns(guid))) return false;
#if LEEROY_HAS_DUNGEON_CLEAR
    if (DcTestRunManager::Instance().IsReserved(player->GetGUID())) return false;
    if (auto* driver = DcTestDriver::Get(); driver && driver->GetGUID() == player->GetGUID()) return false;
#endif
    return true;
}

bool RivalBindingsSafe(Player* player, std::uint32_t targetMap, std::uint32_t ownedInstance, std::uint32_t ownedDifficulty)
{
    if (!player) return false;
    for (uint8 difficulty = 0; difficulty < MAX_DIFFICULTY; ++difficulty)
        for (auto const& [map, bind] : sInstanceSaveMgr->PlayerGetBoundInstances(player->GetGUID(), Difficulty(difficulty)))
            if (!bind.save || !Guild::BenchmarkBindAllowed(map, bind.save->GetInstanceId(), difficulty,
                bind.perm, targetMap, ownedInstance, ownedDifficulty)) return false;
    return true;
}

LeeroyCommandResult HandleGetRivals(nlohmann::json const& message)
{
    std::string error;
    auto cmd = ParseGetRivals(message, error);
    if (!cmd) return {false, error};
    if (!Guild::GuildEnabled()) return {false, "guild_off"};
    struct Row { uint32 guid; float average; nlohmann::json value; };
    std::vector<Row> rows;
    for (auto const& [guid, player] : ObjectAccessor::GetPlayers())
    {
        if (!RivalAvailable(player, cmd->level)) continue;
        auto average = player->GetAverageItemLevel();
        if (!std::isfinite(average) || average < 0 || average > 10000) continue;
        int const ilvl = static_cast<int>(average);
        uint32 const low = guid.GetCounter();
        rows.push_back({low, average, {{"guid", low}, {"name", player->GetName()},
            {"class", player->getClass()}, {"spec_tab", AiFactory::GetPlayerSpecTab(player)},
            {"guild_id", player->GetGuildId()}, {"ilvl", ilvl}, {"zone", player->GetZoneId()},
            {"team", static_cast<uint8>(player->GetTeamId())}}});
    }
    std::sort(rows.begin(), rows.end(), [](Row const& a, Row const& b)
        { return a.average != b.average ? a.average > b.average : a.guid < b.guid; });
    auto members = nlohmann::json::array();
    for (std::size_t index = 0; index < std::min<std::size_t>(cmd->max, rows.size()); ++index)
        members.push_back(std::move(rows[index].value));
    LeeroyModule::Instance().Publish(EncodeEvent(NowUnixMs(), {{"type", "rivals"},
        {"id", message.at("id")}, {"members", std::move(members)}}), false);
    return {true, {}};
}

LeeroyCommandResult HandleStartBenchmark(nlohmann::json const& message)
{
    std::string error;
    auto parsed = ParseStartBenchmark(message, error);
    if (!parsed) return {false, error};
    Guild::OfflineRaidCmd cmd;
    cmd.mapId = parsed->mapId;
    cmd.size = parsed->size;
    cmd.heroic = parsed->heroic;
    cmd.leaderGuid = cmd.lootMasterGuid = parsed->leaderGuid;
    cmd.members = parsed->members;
    cmd.maxWipes = parsed->maxWipes;
    cmd.maxWipesPerBoss = parsed->maxWipesPerBoss;
    cmd.timeoutMin = parsed->timeoutMin;
    cmd.minMembers = parsed->minMembers;
    return Guild::StartRaidJob(message.at("id").get<std::string>(), cmd, Guild::RaidJobMode::Benchmark,
        parsed->bosses);
}
}
