// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "RealmCleanup.h"
#include "RealmPorts.h"
#include "RealmPvp.h"
#include "RealmLfgReceipts.h"
#include "LeeroyRealmCleanup.h"
#include "Battleground.h"
#include "BattlegroundMgr.h"
#include "BattlegroundQueue.h"
#include "GroupMgr.h"
#include <cmath>
#include <map>

namespace Leeroy::Realm
{
namespace
{
using Json = nlohmann::json;
WintergraspMemberFacts WintergraspFacts(Player* player, Battlefield* field)
{
    WintergraspMemberFacts facts;
    facts.foreignHistory = RealmPvpForeignWintergraspHistory(player->GetGUID().GetCounter(),
        reinterpret_cast<std::uintptr_t>(player->GetSession()));
    facts.inZone = player->GetZoneId() == 4197;
    facts.inBattlefieldGroup = player->GetGroup() && player->GetGroup()->isBFGroup();
    if (field)
        for (auto team : {TEAM_ALLIANCE, TEAM_HORDE})
        {
            facts.queued = facts.queued || field->GetPlayersQueueSet(team).contains(player->GetGUID());
            facts.invited = facts.invited || field->GetInvitedPlayersMap(team).contains(player->GetGUID());
            facts.inWar = facts.inWar || field->GetPlayersInWarSet(team).contains(player->GetGUID());
        }
    return facts;
}
constexpr std::uint32_t ExitRestoreMask = DELAYED_BG_MOUNT_RESTORE | DELAYED_BG_TAXI_RESTORE | DELAYED_BG_GROUP_RESTORE;
struct ExitPoint
{
    std::uint32_t map = 0, instance = 0;
    float x = 0, y = 0, z = 0, o = 0;
};
Json Save(ExitPoint const& point) { return {point.map, point.instance, point.x, point.y, point.z, point.o}; }
ExitPoint Load(Json const& point)
{
    return {point.at(0).get<std::uint32_t>(), point.at(1).get<std::uint32_t>(), point.at(2).get<float>(),
        point.at(3).get<float>(), point.at(4).get<float>(), point.at(5).get<float>()};
}
ExitPoint Position(Player* player)
{
    return {player->GetMapId(), player->GetInstanceId(), player->GetPositionX(), player->GetPositionY(),
        player->GetPositionZ(), player->GetOrientation()};
}
ExitPoint Entry(Player* player)
{
    auto const& point = player->GetEntryPoint();
    if (point.GetMapId() == MAPID_INVALID)
        return {player->m_homebindMapId, 0, player->m_homebindX, player->m_homebindY, player->m_homebindZ, player->GetOrientation()};
    return {point.GetMapId(), 0, point.GetPositionX(), point.GetPositionY(), point.GetPositionZ(), point.GetOrientation()};
}
bool ValidEntry(ExitPoint const& point)
{
    auto const* map = sMapStore.LookupEntry(point.map);
    return map && !map->Instanceable() && MapMgr::IsValidMapCoord(point.map, point.x, point.y, point.z, point.o);
}
bool At(Player* player, ExitPoint const& point, bool exactPosition)
{
    return player && !player->IsBeingTeleported() && player->GetMapId() == point.map && player->GetInstanceId() == point.instance
        && (!exactPosition || player->GetDistance(point.x, point.y, point.z) <= 3.0f);
}
bool PortMatches(Player* player, ExitPoint const& point)
{
    auto const& target = player->GetTeleportDest();
    return player->IsBeingTeleported() && target.GetMapId() == point.map
        && std::abs(target.GetPositionX() - point.x) < 0.1f && std::abs(target.GetPositionY() - point.y) < 0.1f
        && std::abs(target.GetPositionZ() - point.z) < 0.1f;
}
bool EntryMatches(Player* player, Json const& member)
{
    auto const expected = Load(member.at("destination"));
    auto const actual = Entry(player);
    return player->GetEntryPoint().GetMapId() == member.at("entry_map").get<std::uint32_t>()
        && actual.map == expected.map && std::abs(actual.x - expected.x) < 0.1f
        && std::abs(actual.y - expected.y) < 0.1f && std::abs(actual.z - expected.z) < 0.1f;
}
std::vector<std::uint32_t> Roster(Group* group)
{
    std::vector<std::uint32_t> members;
    for (auto const& slot : group->GetMemberSlots())
    {
        if (!slot.guid.IsPlayer()) return {};
        members.push_back(slot.guid.GetCounter());
    }
    return members;
}
std::vector<std::uint32_t> Linked(Group* group)
{
    std::vector<std::uint32_t> members;
    for (auto* ref = group->GetFirstMember(); ref; ref = ref->next())
        if (auto* player = ref->GetSource()) members.push_back(player->GetGUID().GetCounter());
    return members;
}
Group* Lookup(std::uint64_t raw)
{
    auto const guid = ObjectGuid(raw);
    auto* group = sGroupMgr->GetGroupByGUID(guid.GetCounter());
    return group && group->GetGUID() == guid ? group : nullptr;
}
std::vector<BodyScope> MemberScopes(Json const& member)
{
    auto const origin = Load(member.at("origin")), destination = Load(member.at("destination"));
    return {{origin.map, origin.instance, member.at("group").get<std::uint64_t>()},
        {origin.map, origin.instance, 0}, {destination.map, destination.instance, 0}};
}
void CurrentScopes(Json& state, Json const& receipt, std::uint32_t current)
{
    for (auto const& member : receipt.at("members"))
        if (member.at("guid").get<std::uint32_t>() == current)
        {
            state["scopes"] = Json::array();
            for (auto const& scope : MemberScopes(member)) state["scopes"].push_back({scope.map, scope.instance, scope.group});
            return;
        }
}
RealmStep Result(Queued const& entry, StepState disposition, std::string why = "")
{
    std::vector<BodyScope> scopes;
    auto const& state = entry.payload.at("step");
    if (state.contains("scopes"))
        for (auto const& scope : state.at("scopes"))
            scopes.push_back({scope.at(0).get<std::uint32_t>(), scope.at(1).get<std::uint32_t>(), scope.at(2).get<std::uint64_t>()});
    return {disposition, std::move(why), 0, std::move(scopes)};
}
RealmStep Failure(Queued const& entry, std::string why)
{
    bool const changed = entry.payload.at("step").value("changed", false) || entry.payload.value("cleanup_mutated", false);
    auto result = Result(entry, changed ? StepState::Unknown : StepState::Refused, std::move(why));
    // Every producer marks both flags before mutation. Tick also requires all earlier
    // members and pending steps to be unchanged before publishing changed:false.
    result.unchanged = !changed;
    return result;
}
std::string ValidateMembers(Queued const& entry, std::vector<std::uint32_t> const& members)
{
    return ValidateCleanupClosure(members, entry.guids, [&](std::uint32_t guid)
    {
        if (auto why = AdmissionReason(guid, Purpose::LeavePvp, entry.id); !why.empty()) return why;
        auto* player = FindOnline(guid);
        if (!player) return std::string("cleanup_member_offline");
        if (player->IsBeingTeleported() || player->duel || (player->GetDelayedOperations() & ExitRestoreMask))
            return std::string("cleanup_member_busy");
        return std::string{};
    });
}
std::string ChargeMembers(Queued const& entry, std::vector<std::uint32_t> const& members)
{
    auto& module = RealmModule::Instance();
    return PrepareCleanupMutation(members, entry.guids,
        [&](std::uint32_t guid) { return module.MutationReason(guid, entry.id); },
        [&](auto const& actual) { return module.ChargeAffectedBodies(entry.id, actual); });
}
void MarkChanged(Queued& entry, std::vector<std::uint32_t> const& members)
{
    entry.payload["cleanup_mutated"] = true;
    if (!entry.payload.contains("cleanup_changed_guids")) entry.payload["cleanup_changed_guids"] = Json::array();
    auto& changed = entry.payload["cleanup_changed_guids"];
    for (auto guid : members)
        if (std::find(changed.begin(), changed.end(), Json(guid)) == changed.end()) changed.push_back(guid);
}
bool SameQueue(GroupQueueInfo const& a, GroupQueueInfo const& b)
{
    return a.Players == b.Players && a.teamId == b.teamId && a.RealTeamID == b.RealTeamID && a.BgTypeId == b.BgTypeId
        && a.IsRated == b.IsRated && a.ArenaType == b.ArenaType && a.ArenaTeamId == b.ArenaTeamId && a.JoinTime == b.JoinTime
        && a.RemoveInviteTime == b.RemoveInviteTime && a.IsInvitedToBGInstanceGUID == b.IsInvitedToBGInstanceGUID
        && a.ArenaTeamRating == b.ArenaTeamRating && a.ArenaMatchmakerRating == b.ArenaMatchmakerRating
        && a.OpponentsTeamRating == b.OpponentsTeamRating && a.OpponentsMatchmakerRating == b.OpponentsMatchmakerRating
        && a.PreviousOpponentsTeamId == b.PreviousOpponentsTeamId && a.BracketId == b.BracketId && a.GroupType == b.GroupType;
}
bool DungeonState(lfg::LfgState state)
{
    return state == lfg::LFG_STATE_DUNGEON || state == lfg::LFG_STATE_FINISHED_DUNGEON;
}
std::vector<std::uint32_t> QueueMembers(std::uint32_t guid, GroupQueueInfo const& info)
{
    if (!info.IsRated || info.IsInvitedToBGInstanceGUID) return {guid};
    std::vector<std::uint32_t> members;
    for (auto member : info.Players)
    {
        if (!member.IsPlayer()) return {};
        members.push_back(member.GetCounter());
    }
    return members;
}
std::vector<std::uint32_t> ReceiptGuids(Json const& receipt, bool remainingOnly = false)
{
    std::vector<std::uint32_t> members;
    for (auto const& member : receipt.at("members"))
        if (!remainingOnly || !member.at("left").get<bool>()) members.push_back(member.at("guid").get<std::uint32_t>());
    return members;
}
CleanupReadback ReadLeftMembers(Queued const& entry, Json const& receipt)
{
    std::vector<std::uint32_t> expected;
    std::vector<GroupCleanupReadback> actual;
    auto& module = RealmModule::Instance();
    for (auto const& member : receipt.at("members"))
    {
        if (!member.at("left").get<bool>()) continue;
        auto const guid = member.at("guid").get<std::uint32_t>();
        auto* player = ObjectAccessor::FindConnectedPlayer(PlayerGuid(guid));
        bool const life = module.ValidateOwnedBody(guid, entry.id).empty() && player && player->GetSession();
        auto const destination = Load(member.at("destination"));
        bool const teleports = member.at("teleports").get<bool>();
        bool const delayedExit = player && (player->GetDelayedOperations() & ExitRestoreMask);
        bool const arrived = life && At(FindOnline(guid), destination, teleports);
        expected.push_back(guid);
        actual.push_back({guid, life, player && !player->GetGroup(), player && !player->GetOriginalGroup(),
            sLFGMgr->GetState(PlayerGuid(guid)) == lfg::LFG_STATE_NONE, !sLFGMgr->GetGroup(PlayerGuid(guid)),
            player && player->IsBeingTeleported(), player && teleports && (PortMatches(player, destination) || (delayedExit && arrived)),
            arrived, delayedExit});
    }
    return expected.empty() ? CleanupReadback::Ready : ReadGroupCleanup(expected, actual);
}
}

std::string LeavePvpAckReason(Queued const& entry)
{
    auto* field = sBattlefieldMgr->GetBattlefieldToZoneId(4197);
    std::vector<LeavePvpMemberFacts> members;
    for (auto guid : entry.guids)
    {
        LeavePvpMemberFacts member{guid, {}, {guid}};
        if (auto* player = ObjectAccessor::FindConnectedPlayer(PlayerGuid(guid)); player && player->GetSession())
        {
            member.wintergrasp = WintergraspFacts(player, field);
            auto add = [&](auto const& closure)
            { member.cleanupMembers.insert(member.cleanupMembers.end(), closure.begin(), closure.end()); };
            for (uint32 slot = 0; slot < PLAYER_MAX_BATTLEGROUND_QUEUES; ++slot)
            {
                auto const type = player->GetBattlegroundQueueTypeId(slot);
                if (type == BATTLEGROUND_QUEUE_NONE) continue;
                if (auto* bg = player->GetBattleground(); player->InBattleground() && bg
                    && type == BattlegroundMgr::BGQueueTypeId(bg->GetBgTypeID(), bg->GetArenaType())) continue;
                GroupQueueInfo info;
                if (sBattlegroundMgr->GetBattlegroundQueue(type).GetPlayerGroupInfoData(player->GetGUID(), &info))
                    add(QueueMembers(guid, info));
            }
            auto const lfgGroup = sLFGMgr->GetGroup(player->GetGUID());
            auto const phase = lfgGroup ? sLFGMgr->GetState(lfgGroup) : lfg::LFG_STATE_NONE;
            if (phase == lfg::LFG_STATE_ROLECHECK || phase == lfg::LFG_STATE_QUEUED)
                if (auto receipt = ReadLfgApplicants(lfgGroup.GetRawValue(), phase == lfg::LFG_STATE_QUEUED))
                    add(LfgReceiptGuids(*receipt));
            if (auto* group = player->GetGroup(); group && group->isLFGGroup()
                && DungeonState(sLFGMgr->GetState(group->GetGUID()))) add(Roster(group));
            // Ordinary/BG group departures reject shared side effects in GroupDepartureReason;
            // their permitted mutation touches only this member. Unreadable receipts still
            // fail conservatively at the step; an absent roster is never an invented closure.
        }
        members.push_back(std::move(member));
    }
    return LeavePvpAckError(members);
}

std::string OrdinaryCleanupReason(Queued const& entry)
{
    auto& module = RealmModule::Instance();
    auto* field = sBattlefieldMgr->GetBattlefieldToZoneId(4197);
    for (auto guid : entry.guids)
    {
        if (auto why = module.MutationReason(guid, entry.id); !why.empty()) return why;
        auto* player = ObjectAccessor::FindConnectedPlayer(PlayerGuid(guid));
        if (!player || !player->GetSession()) return "cleanup_member_offline";
        auto const phase = sLFGMgr->GetState(player->GetGUID());
        auto const lfgGroup = sLFGMgr->GetGroup(player->GetGUID());
        auto const groupPhase = lfgGroup ? sLFGMgr->GetState(lfgGroup) : lfg::LFG_STATE_NONE;
        if (phase == lfg::LFG_STATE_PROPOSAL || groupPhase == lfg::LFG_STATE_PROPOSAL) return "shared_lfg_proposal";
        if (phase == lfg::LFG_STATE_BOOT || groupPhase == lfg::LFG_STATE_BOOT)
            return "shared_lfg_participants_unavailable";
        if (phase == lfg::LFG_STATE_ROLECHECK || groupPhase == lfg::LFG_STATE_ROLECHECK
            || (lfgGroup && (phase == lfg::LFG_STATE_QUEUED || groupPhase == lfg::LFG_STATE_QUEUED)))
        {
            auto const receipt = lfgGroup ? ReadLfgApplicants(lfgGroup.GetRawValue(), groupPhase == lfg::LFG_STATE_QUEUED) : std::nullopt;
            if (!receipt) return "shared_lfg_participants_unavailable";
            if (auto why = ValidateMembers(entry, LfgReceiptGuids(*receipt)); !why.empty()) return why;
        }
        if (auto why = WintergraspPrivateReason(WintergraspFacts(player, field)); !why.empty()) return why;
    }
    return "";
}

std::string PrepareOrdinaryCleanupMutation(Queued& entry, std::uint32_t guid, std::vector<BodyScope> scopes)
{
    if (auto why = ChargeMembers(entry, {guid}); !why.empty()) return why;
    if (auto why = RealmModule::Instance().DeclareAffectedBodyScopes(entry.id, guid, std::move(scopes)); !why.empty()) return why;
    MarkChanged(entry, {guid});
    return "";
}

RealmStep LeaveNativeQueue(Queued& entry, std::uint32_t guid, std::uint32_t slot)
{
    auto& module = RealmModule::Instance();
    auto* bot = FindOnline(guid);
    if (!bot || slot >= PLAYER_MAX_BATTLEGROUND_QUEUES) return Failure(entry, "queue_identity_unavailable");
    auto const type = bot->GetBattlegroundQueueTypeId(slot);
    if (type == BATTLEGROUND_QUEUE_NONE) return Failure(entry, "queue_identity_changed");
    auto& queue = sBattlegroundMgr->GetBattlegroundQueue(type);
    GroupQueueInfo info;
    if (!queue.GetPlayerGroupInfoData(bot->GetGUID(), &info)) return Failure(entry, "queue_receipt_unavailable");
    if (BattlegroundMgr::BGQueueTypeId(info.BgTypeId, info.ArenaType) != type)
        return Failure(entry, "queue_type_changed");
    auto const affected = QueueMembers(guid, info);
    if (std::find(affected.begin(), affected.end(), guid) == affected.end()) return Failure(entry, "queue_manifest_unavailable");
    if (auto why = ValidateMembers(entry, affected); !why.empty()) return Failure(entry, why);
    auto* templateBg = sBattlegroundMgr->GetBattlegroundTemplate(info.BgTypeId);
    if (!templateBg) return Failure(entry, "queue_template_unavailable");
    std::map<std::uint32_t, std::vector<BattlegroundQueueTypeId>> slots;
    auto const native = queue.m_QueuedPlayers.find(bot->GetGUID());
    if (native == queue.m_QueuedPlayers.end()) return Failure(entry, "queue_lineage_unavailable");
    for (auto member : affected)
    {
        auto* player = FindOnline(member);
        GroupQueueInfo copied;
        auto const row = queue.m_QueuedPlayers.find(player->GetGUID());
        if (!queue.GetPlayerGroupInfoData(player->GetGUID(), &copied) || !SameQueue(info, copied)
            || row == queue.m_QueuedPlayers.end() || row->second != native->second
            || player->GetBattlegroundQueueIndex(type) >= PLAYER_MAX_BATTLEGROUND_QUEUES
            || player->IsInvitedForBattlegroundQueueType(type) != bool(info.IsInvitedToBGInstanceGUID)
            || (info.IsInvitedToBGInstanceGUID && !queue.IsPlayerInvited(player->GetGUID(), info.IsInvitedToBGInstanceGUID, info.RemoveInviteTime)))
            return Failure(entry, "queue_lineage_changed");
        for (uint32 index = 0; index < PLAYER_MAX_BATTLEGROUND_QUEUES; ++index)
            slots[member].push_back(player->GetBattlegroundQueueTypeId(index));
    }
    auto const budget = ChargeMembers(entry, affected);
    if (budget == "busy:tick_budget") return Result(entry, StepState::Pending);
    if (!budget.empty()) return Failure(entry, budget);
    for (auto member : affected)
    {
        auto const body = Identity(member);
        if (!body) return Failure(entry, "queue_member_identity_unavailable");
        if (auto why = module.DeclareAffectedBodyScopes(entry.id, member, {Scope(*body)}); !why.empty()) return Failure(entry, why);
        if (member == guid && !entry.payload["step"].contains("scopes"))
            entry.payload["step"]["scopes"] = Json::array({Json::array({body->map, body->instance, body->group})});
    }
    // No yield separates full-roster validation, atomic charging and recursion.
    entry.payload["step"]["changed"] = true;
    MarkChanged(entry, affected);
    if (!entry.payload.contains("queue_cleanup_receipts")) entry.payload["queue_cleanup_receipts"] = Json::array();
    entry.payload["queue_cleanup_receipts"].push_back({{"type", static_cast<std::uint32_t>(type)}, {"members", affected}});
    queue.RemovePlayer(bot->GetGUID(), true);
    bot->RemoveBattlegroundQueueId(type); // Native recursion clears only the later members' slots.
    std::vector<QueueCleanupReadback> readback;
    for (auto member : affected)
    {
        auto* player = FindOnline(member);
        GroupQueueInfo after;
        bool slotsPreserved = player != nullptr;
        for (uint32 index = 0; index < PLAYER_MAX_BATTLEGROUND_QUEUES; ++index)
        {
            auto const expected = slots.at(member).at(index) == type ? BATTLEGROUND_QUEUE_NONE : slots.at(member).at(index);
            slotsPreserved = slotsPreserved && player->GetBattlegroundQueueTypeId(index) == expected;
        }
        readback.push_back({member, player && module.ValidateOwnedBody(member, entry.id).empty(),
            !queue.GetPlayerGroupInfoData(PlayerGuid(member), &after),
            player && player->GetBattlegroundQueueIndex(type) >= PLAYER_MAX_BATTLEGROUND_QUEUES,
            player && !player->IsInvitedForBattlegroundQueueType(type) && !queue.IsPlayerInvitedToRatedArena(PlayerGuid(member)), slotsPreserved});
    }
    if (!ReadQueueCleanup(affected, readback)) return Result(entry, StepState::Unknown, "queue_leave_unproved");
    WorldPacket status;
    sBattlegroundMgr->BuildBattlegroundStatusPacket(&status, templateBg, static_cast<uint8>(slot), STATUS_NONE, 0, 0, 0, TEAM_NEUTRAL);
    bot->SendDirectMessage(&status);
    sBattlegroundMgr->ScheduleQueueUpdate(info.ArenaMatchmakerRating, info.ArenaType, type, info.BgTypeId,
        static_cast<BattlegroundBracketId>(info.BracketId));
    return Result(entry, StepState::Pending);
}

RealmStep LeaveObservedLfgApplicants(Queued& entry, std::uint32_t guid)
{
    auto& module = RealmModule::Instance();
    auto* player = FindOnline(guid);
    auto const group = player ? sLFGMgr->GetGroup(player->GetGUID()) : ObjectGuid::Empty;
    auto const phase = group ? sLFGMgr->GetState(group) : lfg::LFG_STATE_NONE;
    if (phase != lfg::LFG_STATE_ROLECHECK && phase != lfg::LFG_STATE_QUEUED)
        return Failure(entry, "lfg_applicant_phase_changed");
    auto const receipt = ReadLfgApplicants(group.GetRawValue(), phase == lfg::LFG_STATE_QUEUED);
    if (!receipt) return Failure(entry, "shared_lfg_participants_unavailable");
    auto const members = LfgReceiptGuids(*receipt);
    if (std::find(members.begin(), members.end(), guid) == members.end()) return Failure(entry, "lfg_applicant_member_changed");
    if (auto why = ValidateMembers(entry, members); !why.empty()) return Failure(entry, why);
    auto const budget = ChargeMembers(entry, members);
    if (budget == "busy:tick_budget") return Result(entry, StepState::Pending);
    if (!budget.empty()) return Failure(entry, budget);
    for (auto member : members)
    {
        auto const body = Identity(member);
        if (!body) return Failure(entry, "lfg_applicant_identity_unavailable");
        if (auto why = module.DeclareAffectedBodyScopes(entry.id, member, {Scope(*body)}); !why.empty()) return Failure(entry, why);
        if (member == guid)
            entry.payload["step"]["scopes"] = Json::array({Json::array({body->map, body->instance, body->group})});
    }
    if (auto why = BeginLfgApplicantCleanup(entry.id, *receipt); !why.empty()) return Failure(entry, why);
    // Normal LeaveLfg(group) affects exactly the witnessed private roles or
    // group-player producer set. It preserves this ordinary native Group.
    entry.payload["step"]["changed"] = true;
    MarkChanged(entry, members);
    if (!entry.payload.contains("lfg_applicant_cleanup_receipts")) entry.payload["lfg_applicant_cleanup_receipts"] = Json::array();
    entry.payload["lfg_applicant_cleanup_receipts"].push_back({{"group", receipt->group}, {"join", receipt->join}});
    CancelLfgApplicants(*receipt);
    if (!VerifyLfgApplicantCleanup(entry.id, receipt->group, receipt->join))
        return Result(entry, StepState::Unknown, "lfg_applicant_leave_unproved");
    return Result(entry, StepState::Pending);
}

RealmStep LeaveOnlineLfgGroup(Queued& entry, std::uint32_t guid)
{
    auto& module = RealmModule::Instance();
    auto& state = entry.payload["step"];
    if (state.at("stage") != "shared_lfg")
    {
        auto* bot = FindOnline(guid);
        auto* group = bot ? bot->GetGroup() : nullptr;
        if (!group || !group->isLFGGroup()) return Failure(entry, "lfg_group_unavailable");
        if (!sLFGMgr->isOptionEnabled(lfg::LFG_OPTION_ENABLE_DUNGEON_FINDER | lfg::LFG_OPTION_ENABLE_RAID_BROWSER
            | lfg::LFG_OPTION_ENABLE_SEASONAL_BOSSES)) return Failure(entry, "lfg_cleanup_disabled");
        auto const groupGuid = group->GetGUID();
        auto const phase = sLFGMgr->GetState(groupGuid);
        if (!DungeonState(phase)) return Failure(entry, "shared_lfg_participants_unavailable");
        auto const members = Roster(group);
        if (auto why = ValidateMembers(entry, members); !why.empty()) return Failure(entry, why);
        auto plan = PlanOnlineLfgRemoval(members, Linked(group), group->GetLeaderGUID().GetCounter(), guid, true);
        if (!plan.error.empty()) return Failure(entry, plan.error);
        if (group->GetInviteeCount() || sLFGMgr->GetPlayerCount(groupGuid) != members.size())
            return Failure(entry, "lfg_group_manifest_unavailable");
        Json receipt{{"group", groupGuid.GetRawValue()}, {"phase", static_cast<std::uint32_t>(phase)},
            {"members", Json::array()}, {"complete", false}};
        auto const dungeonMap = sLFGMgr->GetDungeonMapId(groupGuid);
        for (auto member : members)
        {
            auto* player = FindOnline(member);
            if (player->GetGroup() != group || player->GetOriginalGroup() || player->InBattleground()
                || sLFGMgr->GetGroup(player->GetGUID()) != groupGuid || sLFGMgr->GetState(player->GetGUID()) != phase)
                return Failure(entry, "lfg_member_lineage_changed");
            bool const teleports = player->GetMap()->IsDungeon() && player->GetMapId() == dungeonMap;
            auto const destination = teleports ? Entry(player) : Position(player);
            if (teleports && !ValidEntry(destination)) return Failure(entry, "entry_point_unavailable");
            receipt["members"].push_back({{"guid", member}, {"group", groupGuid.GetRawValue()},
                {"origin", Save(Position(player))}, {"destination", Save(destination)},
                {"entry_map", player->GetEntryPoint().GetMapId()}, {"teleports", teleports}, {"left", false}});
        }
        if (!entry.payload.contains("lfg_cleanup_receipts")) entry.payload["lfg_cleanup_receipts"] = Json::array();
        state["receipt"] = entry.payload["lfg_cleanup_receipts"].size();
        entry.payload["lfg_cleanup_receipts"].push_back(std::move(receipt));
        state["stage"] = "shared_lfg";
    }
    auto& receipt = entry.payload["lfg_cleanup_receipts"].at(state.at("receipt").get<std::size_t>());
    CurrentScopes(state, receipt, guid);
    auto const left = ReadLeftMembers(entry, receipt);
    if (left == CleanupReadback::Unknown) return Failure(entry, "lfg_member_cleanup_unproved");
    if (left == CleanupReadback::Pending) return Result(entry, StepState::Pending);
    auto const remaining = ReceiptGuids(receipt, true);
    auto* group = Lookup(receipt.at("group").get<std::uint64_t>());
    if (remaining.empty())
    {
        if (group) return Failure(entry, "lfg_group_disband_unproved");
        receipt["complete"] = true;
        state["stage"] = "queues";
        return Result(entry, StepState::Pending);
    }
    if (!group || !group->isLFGGroup() || Roster(group) != remaining || group->GetInviteeCount()
        || sLFGMgr->GetPlayerCount(group->GetGUID()) != remaining.size()
        || static_cast<std::uint32_t>(sLFGMgr->GetState(group->GetGUID())) != receipt.at("phase").get<std::uint32_t>())
        return Failure(entry, "lfg_group_lineage_changed");
    if (auto why = ValidateMembers(entry, remaining); !why.empty()) return Failure(entry, why);
    for (auto const& member : receipt.at("members"))
    {
        if (member.at("left").get<bool>()) continue;
        auto* player = FindOnline(member.at("guid").get<std::uint32_t>());
        if (player->GetGroup() != group || player->GetOriginalGroup()
            || sLFGMgr->GetGroup(player->GetGUID()) != group->GetGUID()
            || static_cast<std::uint32_t>(sLFGMgr->GetState(player->GetGUID())) != receipt.at("phase").get<std::uint32_t>()
            || (member.at("teleports").get<bool>() && !EntryMatches(player, member)))
            return Failure(entry, "lfg_member_lineage_changed");
    }
    auto const leaving = std::find(remaining.begin(), remaining.end(), guid) != remaining.end() ? guid : remaining.front();
    auto const leader = group->GetLeaderGUID().GetCounter();
    std::uint32_t nextLeader = leader;
    if (leader == leaving)
        for (auto member : remaining) if (member != leaving) { nextLeader = member; break; }
    auto* solo = FindOnline(nextLeader);
    bool const keepsSolo = solo && sLFGMgr->GetDungeonMapId(group->GetGUID())
        && (!solo->IsAlive() || solo->GetMapId() == sLFGMgr->GetDungeonMapId(group->GetGUID()));
    auto const plan = PlanOnlineLfgRemoval(remaining, Linked(group), leader, leaving, keepsSolo);
    if (!plan.error.empty()) return Failure(entry, plan.error);
    auto const budget = ChargeMembers(entry, plan.affected);
    if (budget == "busy:tick_budget") return Result(entry, StepState::Pending);
    if (!budget.empty()) return Failure(entry, budget);
    for (auto const& member : receipt.at("members"))
        if (!member.at("left").get<bool>())
            if (auto why = module.DeclareAffectedBodyScopes(entry.id, member.at("guid").get<std::uint32_t>(), MemberScopes(member)); !why.empty())
                return Failure(entry, why);
    // The group may delete itself. Only its immutable full GUID survives this call.
    auto const rawGroup = group->GetGUID().GetRawValue();
    state["changed"] = true;
    MarkChanged(entry, plan.affected);
    group->RemoveMember(PlayerGuid(leaving), GROUP_REMOVEMETHOD_LEAVE);
    group = nullptr;
    auto* after = Lookup(rawGroup);
    if ((plan.disbands && after) || (!plan.disbands && (!after || Roster(after) != plan.remaining
        || after->GetLeaderGUID() != PlayerGuid(plan.leader))))
        return Result(entry, StepState::Unknown, "lfg_group_removal_unproved");
    for (auto& member : receipt["members"])
    {
        auto const memberGuid = member.at("guid").get<std::uint32_t>();
        if (member.at("left").get<bool>() || (!plan.disbands && memberGuid != leaving)) continue;
        member["left"] = true;
        if (memberGuid == leaving || !member.at("teleports").get<bool>()) continue;
        // Native disband clears LFG state/group for its survivor but skips that
        // member's OnRemoveMember entry teleport. Complete only that exact exit.
        auto* player = FindOnline(memberGuid);
        if (!player || !module.ValidateOwnedBody(memberGuid, entry.id).empty() || player->GetGroup() || player->GetOriginalGroup()
            || sLFGMgr->GetState(player->GetGUID()) != lfg::LFG_STATE_NONE || sLFGMgr->GetGroup(player->GetGUID())
            || !EntryMatches(player, member) || !At(player, Load(member.at("origin")), false))
            return Result(entry, StepState::Unknown, "lfg_survivor_exit_unproved");
        if (!player->TeleportToEntryPoint()) return Result(entry, StepState::Unknown, "lfg_survivor_worldport_refused");
    }
    if (ReadLeftMembers(entry, receipt) == CleanupReadback::Unknown)
        return Result(entry, StepState::Unknown, "lfg_member_cleanup_unproved");
    return Result(entry, StepState::Pending);
}

std::string VerifyCleanupReceipts(Queued const& entry)
{
    auto& module = RealmModule::Instance();
    if (auto why = OrdinaryCleanupReason(entry); !why.empty()) return why;
    if (entry.payload.contains("lfg_applicant_cleanup_receipts"))
        for (auto const& receipt : entry.payload.at("lfg_applicant_cleanup_receipts"))
            if (!VerifyLfgApplicantCleanup(entry.id, receipt.at("group").get<std::uint64_t>(), receipt.at("join").get<std::uint64_t>()))
                return "lfg_applicant_cleanup_readback_changed";
    if (entry.payload.contains("queue_cleanup_receipts"))
        for (auto const& receipt : entry.payload.at("queue_cleanup_receipts"))
        {
            auto const type = static_cast<BattlegroundQueueTypeId>(receipt.at("type").get<std::uint32_t>());
            auto& queue = sBattlegroundMgr->GetBattlegroundQueue(type);
            auto const members = receipt.at("members").get<std::vector<std::uint32_t>>();
            std::vector<QueueCleanupReadback> readback;
            for (auto guid : members)
            {
                auto* player = FindOnline(guid);
                GroupQueueInfo info;
                readback.push_back({guid, player && module.ValidateOwnedBody(guid, entry.id).empty(),
                    !queue.GetPlayerGroupInfoData(PlayerGuid(guid), &info),
                    player && player->GetBattlegroundQueueIndex(type) >= PLAYER_MAX_BATTLEGROUND_QUEUES,
                    player && !player->IsInvitedForBattlegroundQueueType(type) && !queue.IsPlayerInvitedToRatedArena(PlayerGuid(guid)), true});
            }
            if (!ReadQueueCleanup(members, readback)) return "queue_cleanup_readback_changed";
        }
    if (entry.payload.contains("lfg_cleanup_receipts"))
        for (auto const& receipt : entry.payload.at("lfg_cleanup_receipts"))
            if (!receipt.at("complete").get<bool>() || Lookup(receipt.at("group").get<std::uint64_t>())
                || ReadLeftMembers(entry, receipt) != CleanupReadback::Ready)
                return "lfg_cleanup_readback_changed";
    if (entry.cursor + 1 == entry.guids.size())
        for (auto guid : entry.guids)
        {
            auto* player = FindOnline(guid);
            if (!player || !module.ValidateOwnedBody(guid, entry.id).empty() || player->IsBeingTeleported()
                || (player->GetDelayedOperations() & ExitRestoreMask) || player->InBattleground() || player->GetOriginalGroup()
                || sLFGMgr->GetState(PlayerGuid(guid)) != lfg::LFG_STATE_NONE
                || (player->GetGroup() && (player->GetGroup()->isLFGGroup() || player->GetGroup()->isBGGroup() || player->GetGroup()->isBFGroup())))
                return "cleanup_manifest_readback_changed";
            for (uint32 slot = 0; slot < PLAYER_MAX_BATTLEGROUND_QUEUES; ++slot)
                if (player->GetBattlegroundQueueTypeId(slot) != BATTLEGROUND_QUEUE_NONE)
                    return "cleanup_manifest_queue_changed";
        }
    return "";
}
}
