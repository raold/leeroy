// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "RaidAssembly.h"
#include "GuildPorts.h"

#include "AiObjectContext.h"
#include "DBCStores.h"
#include "Event.h"
#include "Formations.h"
#include "Group.h"
#include "GroupMgr.h"
#include "InstanceSaveMgr.h"
#include "Map.h"
#include "MapMgr.h"
#include "ObjectMgr.h"
#include "Opcodes.h"
#include "PlayerbotAI.h"
#include "Playerbots.h"
#include "WorldPacket.h"

#if !defined(LEEROY_HAS_DUNGEON_CLEAR)
#error "LEEROY_HAS_DUNGEON_CLEAR comes from mod-leeroy.cmake; re-run cmake"
#endif
#if LEEROY_HAS_DUNGEON_CLEAR
#include "Ai/Dungeon/DungeonClear/Util/DcLeaderSignal.h"
#include "TestRun/DcTestDriver.h"
#include "Util/DcDungeonAccess.h"
#endif

#include <algorithm>
#include <chrono>
#include <cmath>
#include <memory>
#include <string>
#include <utility>
#include <vector>

namespace Leeroy::Guild
{
namespace
{
using json = nlohmann::json;
using SteadyClock = std::chrono::steady_clock;

ObjectGuid Guid(uint32 low) { return ObjectGuid::Create<HighGuid::Player>(low); }
bool Owned(uint32 low) { return low && (low == HumanGuid() || IsCast(low)); }
LeeroyCommandResult Fail(std::string error) { return {false, std::move(error)}; }
LeeroyCommandResult Ok() { return {true, {}}; }
std::string NotCast(uint32 low) { return "not_cast:" + std::to_string(low); }

std::string GroupOwnership(Group const* group)
{
    for (auto const& slot : group->GetMemberSlots())
        if (!Owned(slot.guid.GetCounter()))
            return NotCast(slot.guid.GetCounter());
    return {};
}

struct SummonJob
{
    JobEvent result;
    AreaTriggerTeleport destination;
    ObjectGuid group;
    uint32 instance = 0;
    bool inside = false;
    std::vector<uint32> pending;
    SteadyClock::time_point deadline;
};

// World thread only. Completion is queued after returning from a command handler,
// so P1a can enqueue its ack before the next guild tick publishes the job.
std::vector<JobEvent> completed;
std::vector<SummonJob> summons;

bool Arrived(Player* player, SummonJob const& job)
{
    auto const& dest = job.destination;
    return player && !player->IsBeingTeleported() && player->GetMapId() == dest.target_mapId
        && (!job.inside || (player->GetInstanceId() == job.instance && player->GetGroup()
                            && player->GetGroup()->GetGUID() == job.group))
        && player->GetDistance2d(dest.target_X, dest.target_Y) <= 40.0f
        && std::abs(player->GetPositionZ() - dest.target_Z) <= 40.0f;
}

std::string SummonFailure(Player* player)
{
    if (!player)
        return "offline";
    if (player->isDead())
        return "dead";
    if (player->InBattleground() || player->InBattlegroundQueue())
        return "in_bg";
    if (player->IsInCombat())
        return "in_combat";
    return {};
}

#if LEEROY_HAS_DUNGEON_CLEAR
void RaidHabits(Player* bot, Player* leader, uint32 map, Difficulty difficulty)
{
    if (PlayerbotAI* ai = GET_PLAYERBOT_AI(bot))
    {
        if (leader->GetGUID().GetCounter() == HumanGuid())
            ai->SetMaster(leader);
        ai->ResetStrategies();
        // Directly load the value; the chat action whispers "Formation set to".
        auto* formation = static_cast<FormationValue*>(ai->GetAiObjectContext()->GetValue<Formation*>("formation"));
        if (formation)
            formation->Load("arrow");
        if (!PlayerbotAI::IsTank(bot, true) && !PlayerbotAI::IsHeal(bot, true))
        {
            ai->ChangeStrategy("+wait for attack", BOT_STATE_COMBAT);
            ai->GetAiObjectContext()->GetValue<uint8>("wait for attack time")->Set(4);
        }
    }
    DcDungeonAccess::GrantEntry(bot, map, difficulty);
}
#endif
}

LeeroyCommandResult HandleClearLockout(json const& msg)
{
    std::string error;
    auto const cmd = ParseClearLockout(msg, error);
    if (!cmd)
        return Fail(error);
    for (uint32 low : cmd->guids)
        if (!Owned(low))
            return Fail(NotCast(low));

    std::string inside;
    for (uint32 low : cmd->guids)
        if (Player* player = ObjectAccessor::FindConnectedPlayer(Guid(low)))
            if (player->GetMapId() == cmd->mapId || (player->IsBeingTeleported()
                && player->GetTeleportDest().GetMapId() == cmd->mapId))
            {
                if (!inside.empty())
                    inside += ',';
                inside += std::to_string(low);
            }
    if (!inside.empty())
        return Fail("in_instance:" + inside);

    for (uint32 low : cmd->guids)
    {
        ObjectGuid const guid = Guid(low);
        // PlayerUnbindInstance dereferences this storage even for never-loaded characters.
        sInstanceSaveMgr->PlayerCreateBoundInstancesMaps(guid);
        Player* player = FindOnline(low);
        for (uint8 difficulty : cmd->difficulties)
            sInstanceSaveMgr->PlayerUnbindInstance(guid, cmd->mapId, Difficulty(difficulty), true, player);
    }
    return Ok();
}

LeeroyCommandResult HandleFormRaid(json const& msg)
{
    std::string error;
    auto const cmd = ParseFormRaid(msg, error);
    if (!cmd)
        return Fail(error);
    if (!Owned(cmd->leaderGuid))
        return Fail(NotCast(cmd->leaderGuid));
    for (uint32 low : cmd->members)
        if (!IsCast(low))
            return Fail(NotCast(low));
    Player* leader = FindOnline(cmd->leaderGuid);
    if (!leader)
        return Fail("leader_offline");
    MapEntry const* entry = sMapStore.LookupEntry(cmd->mapId);
    if (!entry || !entry->IsRaid())
        return Fail("bad field: map_id");
    Difficulty const difficulty = Difficulty(RaidDifficulty(cmd->size, cmd->heroic));
    if (!GetMapDifficultyData(cmd->mapId, difficulty))
        return Fail("no_difficulty");
    if (leader->GetMap()->IsDungeon() || leader->IsBeingTeleported())
        return Fail("in_instance");
    Group* group = leader->GetGroup();
    if (group)
    {
        if (!group->IsLeader(leader->GetGUID()))
            return Fail("not_leader");
        if (group->isLFGGroup() || group->isBGGroup() || group->isBFGroup())
            return Fail("lfg_group");
        if (auto const ownership = GroupOwnership(group); !ownership.empty())
            return Fail(ownership);
        if (group->GetMembersCount() > cmd->size)
            return Fail("bad field: size");
    }
#if !LEEROY_HAS_DUNGEON_CLEAR
    return Fail("driver_unavailable:no_dungeon_clear");
#else
    if (!group)
    {
        auto created = std::make_unique<Group>();
        if (!created->Create(leader))
            return Fail("not_leader");
        group = created.release();
        sGroupMgr->AddGroup(group);
    }
    if (!group->isRaidGroup())
        group->ConvertToRaid();

    JobEvent result;
    result.id = msg.at("id").get<std::string>();
    result.kind = "form_raid";
    result.state = "done";
    for (uint32 low : cmd->members)
    {
        Player* bot = FindOnline(low);
        std::string skip;
        if (!bot)
            skip = "offline";
        else if (bot->InBattleground() || bot->InBattlegroundQueue())
            skip = "in_bg";
        else if (bot->isDead())
            skip = "dead";
        else if (bot->IsInFlight())
            skip = "in_flight";
        else if (bot->GetGroup() && bot->GetGroup()->isLFGGroup())
            skip = "in_lfg";
        else if (bot->IsBeingTeleported() || (bot->GetMap()->IsDungeon() && bot->GetMapId() != cmd->mapId))
            skip = "in_instance";
        else if (bot->GetGroup() != group && (group->IsFull() || group->GetMembersCount() >= cmd->size))
            skip = "group_full";
        if (!skip.empty())
        {
            result.skipped.emplace_back(low, skip);
            continue;
        }
        if (bot->GetGroup() != group)
        {
            if (bot->GetGroup())
                bot->RemoveFromGroup();
            if (!group->AddMember(bot))
            {
                result.skipped.emplace_back(low, "group_full");
                continue;
            }
        }
        RaidHabits(bot, leader, cmd->mapId, difficulty);
        result.guids.push_back(low);
    }
    group->ChangeMembersGroup(leader->GetGUID(), 0);
    uint32 position = 1;
    // Copy GUIDs: flag/group changes send updates and must not hold a member iterator.
    std::vector<ObjectGuid> members;
    for (auto const& slot : group->GetMemberSlots())
        members.push_back(slot.guid);
    for (ObjectGuid guid : members)
    {
        if (guid != leader->GetGUID())
            group->ChangeMembersGroup(guid, uint8(position++ % MAX_RAID_SUBGROUPS));
        group->SetGroupMemberFlag(guid, false, MEMBER_FLAG_MAINTANK);
        group->SetGroupMemberFlag(guid, false, MEMBER_FLAG_ASSISTANT);
    }
    if (group->IsMember(Guid(cmd->mainTankGuid)))
        group->SetGroupMemberFlag(Guid(cmd->mainTankGuid), true, MEMBER_FLAG_MAINTANK);
    for (uint32 low : cmd->assistants)
        if (group->IsMember(Guid(low)))
            group->SetGroupMemberFlag(Guid(low), true, MEMBER_FLAG_ASSISTANT);
    group->SetRaidDifficulty(difficulty);
    group->SendUpdate();
    completed.push_back(std::move(result));
    return Ok();
#endif
}

LeeroyCommandResult HandleSetLootMaster(json const& msg)
{
    std::string error;
    auto const cmd = ParseSetLootMaster(msg, error);
    if (!cmd)
        return Fail(error);
    for (uint32 low : {cmd->memberGuid, cmd->looterGuid})
        if (!Owned(low))
            return Fail(NotCast(low));
    Player* member = FindOnline(cmd->memberGuid);
    Group* group = member ? member->GetGroup() : nullptr;
    if (!group)
        return Fail("no_group");
    if (group->isLFGGroup() || group->isBGGroup() || group->isBFGroup())
        return Fail("no_group");
    if (auto const ownership = GroupOwnership(group); !ownership.empty())
        return Fail(ownership);
    if (!group->IsMember(Guid(cmd->looterGuid)))
        return Fail("not_member:" + std::to_string(cmd->looterGuid));
    group->SetLootMethod(MASTER_LOOT);
    group->SetMasterLooterGuid(Guid(cmd->looterGuid));
    group->SetLootThreshold(ItemQualities(cmd->threshold));
    group->SendUpdate();
    return Ok();
}

LeeroyCommandResult HandleSummon(json const& msg)
{
    std::string error;
    auto const cmd = ParseSummon(msg, error);
    if (!cmd)
        return Fail(error);
    for (uint32 low : cmd->guids)
        if (!IsCast(low))
            return Fail(NotCast(low));
    AreaTriggerTeleport const* dest = cmd->where == "inside"
        ? sObjectMgr->GetMapEntranceTrigger(cmd->mapId) : sObjectMgr->GetGoBackTrigger(cmd->mapId);
    if (!dest || !MapMgr::IsValidMapCoord(dest->target_mapId, dest->target_X, dest->target_Y,
                                         dest->target_Z, dest->target_Orientation))
        return Fail("no_entrance");
    SummonJob job;
    job.destination = *dest;
    job.inside = cmd->where == "inside";
    if (job.inside)
    {
        Player* first = FindOnline(cmd->guids.front());
        Group* group = first ? first->GetGroup() : nullptr;
        Player* leader = group ? FindOnline(group->GetLeaderGUID().GetCounter()) : nullptr;
        if (!leader || leader->GetMapId() != cmd->mapId || leader->IsBeingTeleported())
            return Fail("leader_not_inside");
        if (auto const ownership = GroupOwnership(group); !ownership.empty())
            return Fail(ownership);
        for (uint32 low : cmd->guids)
            if (Player* bot = FindOnline(low); bot && bot->GetGroup() != group)
                return Fail("leader_not_inside");
        job.group = group->GetGUID();
        job.instance = leader->GetInstanceId();
    }
    uint32 const summonerLow = cmd->summonerGuid ? cmd->summonerGuid : cmd->guids.front();
    Player* summoner = nullptr;
    if (cmd->summonHuman)
    {
        if (!Owned(summonerLow) || !(summoner = FindOnline(summonerLow)))
            return Fail("bad field: summoner_guid");
    }
    job.result.id = msg.at("id").get<std::string>();
    job.result.kind = "summon";
    job.result.state = "done";
    // A verified inside arrival gives the mind an instance boundary for loot events.
    job.result.instanceId = job.instance;
    job.deadline = SteadyClock::now() + std::chrono::seconds(60);
    constexpr double tau = 6.2831853071795864769;
    for (std::size_t index = 0; index < cmd->guids.size(); ++index)
    {
        uint32 const low = cmd->guids[index];
        Player* bot = FindOnline(low);
        if (auto const failure = SummonFailure(bot); !failure.empty())
        {
            job.result.skipped.emplace_back(low, failure);
            continue;
        }
        if (Arrived(bot, job))
        {
            job.result.guids.push_back(low);
            continue;
        }
        job.pending.push_back(low);
        // A personal permanent bind can outrank the group leader's destination.
        // Never teleport into another copy and then report same-map arrival.
        if (job.inside && sInstanceSaveMgr->PlayerGetDestinationInstanceId(
            bot, dest->target_mapId, bot->GetDifficulty(true)) != job.instance)
            continue;
        double const angle = tau * static_cast<double>(index) / static_cast<double>(cmd->guids.size());
        bool const changeInstance = job.inside && bot->GetMapId() == dest->target_mapId
            && bot->GetInstanceId() != job.instance;
        bot->TeleportTo(dest->target_mapId, dest->target_X + 4.0f * float(std::cos(angle)),
                        dest->target_Y + 4.0f * float(std::sin(angle)), dest->target_Z,
                        dest->target_Orientation, 0, nullptr, changeInstance);
    }
    if (cmd->summonHuman)
        if (Player* human = FindOnline(HumanGuid()); human && human->GetMapId() != dest->target_mapId)
        {
            human->SetSummonPoint(dest->target_mapId, dest->target_X, dest->target_Y, dest->target_Z);
            WorldPacket packet(SMSG_SUMMON_REQUEST, 8 + 4 + 4);
            packet << summoner->GetGUID();
            packet << uint32(sMapMgr->GetZoneId(PHASEMASK_NORMAL, dest->target_mapId,
                                               dest->target_X, dest->target_Y, dest->target_Z));
            packet << uint32(MAX_PLAYER_SUMMON_DELAY * IN_MILLISECONDS);
            human->SendDirectMessage(&packet);
        }
    summons.push_back(std::move(job));
    return Ok();
}

void TickSummons()
{
    for (auto const& result : completed)
        PublishJob(result);
    completed.clear();
    auto const now = SteadyClock::now();
    for (auto job = summons.begin(); job != summons.end();)
    {
        for (auto guid = job->pending.begin(); guid != job->pending.end();)
        {
            if (Arrived(FindOnline(*guid), *job))
            {
                job->result.guids.push_back(*guid);
                guid = job->pending.erase(guid);
            }
            else if (now >= job->deadline)
            {
                job->result.skipped.emplace_back(*guid, "timeout");
                guid = job->pending.erase(guid);
            }
            else
                ++guid;
        }
        if (job->pending.empty())
        {
            PublishJob(job->result);
            job = summons.erase(job);
        }
        else
            ++job;
    }
}

LeeroyCommandResult HandleRaidControl(json const& msg)
{
    std::string error;
    auto const cmd = ParseRaidControl(msg, error);
    if (!cmd)
        return Fail(error);
    if (!Owned(cmd->memberGuid))
        return Fail(NotCast(cmd->memberGuid));
    if (cmd->leaderGuid && !Owned(cmd->leaderGuid))
        return Fail(NotCast(cmd->leaderGuid));
    Player* member = FindOnline(cmd->memberGuid);
    Group* group = member ? member->GetGroup() : nullptr;
    if (!group)
        return Fail("no_group");
    if (auto const ownership = GroupOwnership(group); !ownership.empty())
        return Fail(ownership);
    if (group->isLFGGroup() || group->isBGGroup() || group->isBFGroup())
        return Fail("no_group");
    if (cmd->action == "disband")
    {
        if (group->GetLeaderGUID() == Guid(HumanGuid()))
        {
            ObjectGuid const original = group->GetGUID();
            std::vector<ObjectGuid> cast;
            for (auto const& slot : group->GetMemberSlots())
                if (IsCast(slot.guid.GetCounter()))
                    cast.push_back(slot.guid);
            for (ObjectGuid guid : cast)
            {
                Player* human = FindOnline(HumanGuid());
                Group* current = human ? human->GetGroup() : sGroupMgr->GetGroupByGUID(original.GetCounter());
                if (!current || current->GetGUID() != original || current->GetLeaderGUID() != Guid(HumanGuid()))
                    break;
                // The final removal can delete the Group, even for a raid.
                current->RemoveMember(guid);
            }
        }
        else
            group->Disband();
        return Ok();
    }
    if (cmd->action == "ready_check")
    {
        ObjectGuid const leader = Guid(cmd->leaderGuid);
        if (!group->IsLeader(leader) && !group->IsAssistant(leader))
            return Fail("not_leader");
        WorldPacket packet(MSG_RAID_READY_CHECK, 8);
        packet << leader;
        group->BroadcastPacket(&packet, false, -1);
        group->OfflineReadyCheck();
        return Ok();
    }
#if LEEROY_HAS_DUNGEON_CLEAR
    Player* leader = FindOnline(cmd->leaderGuid);
    if (!leader || leader->GetGroup() != group)
        return Fail("leader_mismatch:0");
    Player* elected = DcLeaderSignal::FindLeaderTank(leader);
    if (elected != leader)
        return Fail("leader_mismatch:" + std::to_string(elected ? elected->GetGUID().GetCounter() : 0));
    PlayerbotAI* ai = GET_PLAYERBOT_AI(leader);
    if (!ai)
        return Fail("dc_refused");
    Player* issuer = FindOnline(HumanGuid());
    if (!issuer || issuer->GetGroup() != group)
        issuer = DcTestDriver::Get();
    if (!issuer)
        return Fail("dc_refused");
    std::string const action = cmd->action == "dc_on" ? "dc on"
        : cmd->action == "dc_off" ? "dc off" : "dc pause";
    return ai->DoSpecificAction(action, Event("dc", cmd->param, issuer), true) ? Ok() : Fail("dc_refused");
#else
    return Fail("driver_unavailable:no_dungeon_clear");
#endif
}

void RegisterRaidAssemblyHandlers()
{
    RegisterCommand("clear_lockout", HandleClearLockout);
    RegisterCommand("form_raid", HandleFormRaid);
    RegisterCommand("set_loot_master", HandleSetLootMaster);
    RegisterCommand("summon", HandleSummon);
    RegisterCommand("raid_control", HandleRaidControl);
}
}
