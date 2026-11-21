// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "MemberDungeonReceipts.h"
#include "GuildPorts.h"
#include "MemberDungeonJob.h"
#include "DatabaseEnv.h"
#include "QueryResult.h"
#include "GroupMgr.h"
#include "Realm.h"
#include "World.h"
#include "PlayerbotAI.h"
#include "Playerbots.h"
#include "TrainingRivals.h"
#include "TrainingCommands.h"
#include "MapMgr.h"
#include "Realm/RealmModule.h"
#include "LeeroyOfflineRaidPolicy.h"
#include "LeeroyMemberDungeon.h"
#include <cmath>
#include <algorithm>
#include <map>
#include <set>

namespace Leeroy::Guild
{
namespace
{
using Json = nlohmann::json;
constexpr char const* CreateStore =
    "CREATE TABLE IF NOT EXISTS leeroy_member_dungeon_receipt ("
    "realm INT UNSIGNED NOT NULL, start_id VARBINARY(128) NOT NULL, revision BIGINT UNSIGNED NOT NULL, "
    "active_slot TINYINT UNSIGNED NULL, settled TINYINT UNSIGNED NOT NULL, payload MEDIUMBLOB NOT NULL, "
    "PRIMARY KEY(realm,start_id), UNIQUE KEY one_active(realm,active_slot)) ENGINE=InnoDB";
std::map<std::string,DungeonReceipt> outstanding;
bool loaded = false, exists = false;
uint32 retry = 0;

std::string Hex(std::string const& value)
{
    constexpr char digits[] = "0123456789abcdef";
    std::string hex;
    hex.reserve(value.size()*2);
    for (unsigned char c : value) { hex.push_back(digits[c>>4]); hex.push_back(digits[c&15]); }
    return hex;
}

bool Load()
{
    if (loaded) return true;
    auto probe = CharacterDatabase.Query("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() "
        "AND table_name='leeroy_member_dungeon_receipt'");
    if (!probe) return false;
    exists = probe->Fetch()[0].Get<uint64>() != 0;
    if (exists)
    {
        auto count = CharacterDatabase.Query("SELECT COUNT(*) FROM leeroy_member_dungeon_receipt WHERE realm={} AND settled=0", ::realm.Id.Realm);
        if (!count) return false;
        auto expected = count->Fetch()[0].Get<uint64>();
        if (expected > 128) return false;
        std::map<std::string,DungeonReceipt> restored;
        if (expected)
        {
            auto rows = CharacterDatabase.Query("SELECT start_id,revision,payload FROM leeroy_member_dungeon_receipt "
                "WHERE realm={} AND settled=0 LIMIT 129", ::realm.Id.Realm);
            if (!rows) return false;
            do
            {
                auto* fields = rows->Fetch();
                auto id = fields[0].Get<std::string>();
                auto data = Json::parse(fields[2].Get<std::string>(), nullptr, false);
                if (!DungeonReceiptIdValid(id) || !ValidDungeonReceipt(data)) return false;
                restored.emplace(id,DungeonReceipt{id, fields[1].Get<uint64>(), std::move(data)});
            } while (rows->NextRow());
        }
        if (restored.size() != expected) return false;
        outstanding = std::move(restored);
    }
    return loaded = true;
}

std::optional<DungeonReceipt> Lookup(std::string const& id)
{
    if (!exists || !DungeonReceiptIdValid(id)) return std::nullopt;
    auto row = CharacterDatabase.Query("SELECT revision,payload FROM leeroy_member_dungeon_receipt WHERE realm={} AND start_id=UNHEX('{}')",
        ::realm.Id.Realm, Hex(id));
    if (!row) return std::nullopt;
    auto data = Json::parse(row->Fetch()[1].Get<std::string>(), nullptr, false);
    if (!ValidDungeonReceipt(data)) return std::nullopt;
    return DungeonReceipt{id, row->Fetch()[0].Get<uint64>(), std::move(data)};
}
}

bool DungeonReceiptIdValid(std::string const& id)
{
    return !id.empty() && id.size() <= 128 && std::none_of(id.begin(),id.end(),[](unsigned char c) { return c < 32 || c == 127; });
}

bool BeginDungeonReceipt(DungeonReceipt& receipt, std::string const& id, uint32 guild, uint32 human, uint32 leader,
    std::vector<uint32> const& roster)
{
    if (!MemberDungeonEnabled() || !DungeonReceiptIdValid(id) || !Load() || outstanding.size() >= 128) return false;
    if (!exists)
    {
        CharacterDatabase.DirectExecute(CreateStore);
        if (!CharacterDatabase.Query("SELECT COUNT(*) FROM leeroy_member_dungeon_receipt")) return false;
        exists = true;
    }
    Json data = {{"version",1},{"guild",guild},{"human",human},{"leader",leader},{"roster",roster},
        {"map",602},{"difficulty",1},{"stage","accepted"},{"slot",true},{"settled",false},{"bodies",Json::array()},
        {"terminal",nullptr},{"instance",0},{"group",0},{"issued_ms",NowUnixMs()}};
    if (Lookup(id)) return false;
    CharacterDatabase.DirectExecute("INSERT INTO leeroy_member_dungeon_receipt(realm,start_id,revision,active_slot,settled,payload) "
        "VALUES({},UNHEX('{}'),1,1,0,UNHEX('{}'))", ::realm.Id.Realm, Hex(id), Hex(data.dump()));
    auto row = Lookup(id);
    if (!row || row->revision != 1 || row->data != data) return false;
    receipt = *row;
    outstanding[id] = receipt;
    return true;
}

bool SaveDungeonReceipt(DungeonReceipt& receipt, Json const& next)
{
    if (receipt.data == next) return true;
    auto payload = next.dump();
    if (payload.size() > 32768 || !receipt.revision || !ValidDungeonReceipt(next)) return false;
    CharacterDatabase.DirectExecute("UPDATE leeroy_member_dungeon_receipt SET revision=revision+1,active_slot={},settled={},payload=UNHEX('{}') "
        "WHERE realm={} AND start_id=UNHEX('{}') AND revision={}", next.at("slot").get<bool>() ? "1" : "NULL",
        unsigned(next.at("settled").get<bool>()), Hex(payload), ::realm.Id.Realm, Hex(receipt.id), receipt.revision);
    auto row = Lookup(receipt.id);
    if (!row || row->revision != receipt.revision+1 || row->data != next) return false;
    receipt = *row;
    if (next.at("settled").get<bool>()) outstanding.erase(receipt.id);
    else outstanding[receipt.id] = receipt;
    return true;
}

bool DungeonRecoveryConflicts(std::vector<uint32> const& guids, bool requiresSlot)
{
    if (!loaded) return true;
    for (auto const& [id, row] : outstanding)
    {
        if (requiresSlot && row.data.at("slot").get<bool>()) return true;
        for (auto guid : guids)
            if (std::find(row.data.at("roster").begin(), row.data.at("roster").end(), guid) != row.data.at("roster").end())
                return true;
    }
    return false;
}

bool DungeonReturnsPersisted(DungeonReceipt const& receipt)
{
    for (auto const& b : receipt.data.at("bodies"))
    {
        if (!b.value("changed",false)) continue;
        auto guid = b.at("guid").get<uint32>();
        auto row = CharacterDatabase.Query("SELECT c.map,c.instance_id,c.position_x,c.position_y,c.position_z,"
            "c.instance_mode_mask,c.playerFlags,COALESCE(gm.guid,0) FROM characters c "
            "LEFT JOIN group_member gm ON gm.memberGuid=c.guid WHERE c.guid={}",guid);
        if (!row) return false;
        auto* fields = row->Fetch();
        auto mask = b.at("dungeon").get<uint32>() | (b.at("raid").get<uint32>() << 4);
        if (fields[0].Get<uint32>() != b.at("map") || fields[1].Get<uint32>() || fields[7].Get<uint32>() ||
            fields[5].Get<uint8>() != mask || std::hypot(fields[2].Get<float>()-b.at("x").get<float>(),
            fields[3].Get<float>()-b.at("y").get<float>()) > 5 ||
            std::abs(fields[4].Get<float>()-b.at("z").get<float>()) > 5 ||
            (guid == receipt.data.at("leader") && bool(fields[6].Get<uint32>() & PLAYER_FLAGS_GROUP_LEADER) != b.at("leader"))) return false;
    }
    return true;
}

bool DungeonReturned(std::string const& id, std::vector<uint32> const& guids)
{
    if (!loaded || outstanding.contains(id)) return false;
    auto row = Lookup(id);
    if (!row || !row->data.value("settled",false) || row->data.value("guild",0u) != LeeroyModule::Instance().GuildId() ||
        row->data.value("human",0u) != HumanGuid()) return false;
    auto const& roster = row->data.at("roster");
    return std::all_of(guids.begin(), guids.end(), [&](auto g){ return std::find(roster.begin(),roster.end(),g) != roster.end(); });
}

LeeroyCommandResult PublishDungeonReceipt(std::string const& id)
{
    if (!loaded) return {false,"dungeon_store_unavailable"};
    auto row = Lookup(id);
    if (!row)
    {
        if (!exists) return {false,"unknown_dungeon"};
        auto count = CharacterDatabase.Query("SELECT COUNT(*) FROM leeroy_member_dungeon_receipt WHERE realm={} AND start_id=UNHEX('{}')",
            ::realm.Id.Realm, Hex(id));
        return {false,count && !count->Fetch()[0].Get<uint64>() ? "unknown_dungeon" : "dungeon_receipt_unproven"};
    }
    auto const& data = row->data;
    if (data.at("guild") != LeeroyModule::Instance().GuildId() || data.at("human") != HumanGuid()) return {false,"wrong_owner"};
    JobEvent progress;
    progress.id = id;
    progress.kind = "member_dungeon";
    progress.state = "progress";
    progress.stage = data.at("stage").get<std::string>();
    progress.guids = data.at("roster").get<std::vector<uint32>>();
    progress.instanceId = data.value("instance",0u);
    auto event = data.at("terminal").is_object() ? data.at("terminal") : MakeJobEvent(progress);
    event["returned"] = data.at("settled");
    event["map_id"] = data.at("map");
    event["revision"] = row->revision;
    LeeroyModule::Instance().Publish(EncodeEvent(NowUnixMs(),event),false);
    return {true,{}};
}

namespace
{
// Recover on the world thread from the exact persisted party and locations. On a new boot there
// is no authority over an AI master, training journal or replacement party from the old session.
bool RecoverBodies(DungeonReceipt& receipt)
{
    auto const& data = receipt.data;
    if (data.at("stage") == "accepted" && data.at("bodies").empty()) return true;
    if (data.at("guild") != LeeroyModule::Instance().GuildId() || data.at("human") != HumanGuid()) return false;
    auto const groupId = data.value("group",0u);
    auto const instance = data.value("instance",0u);
    auto const leader = data.at("leader").get<uint32>();
    auto const& bodies = data.at("bodies");
    if (bodies.size() != 5) return false;
    auto* group = groupId ? sGroupMgr->GetGroupByGUID(groupId) : nullptr;
    if (group && (group->GetLeaderGUID().GetCounter() != leader || group->isRaidGroup() ||
        group->isLFGGroup() || group->isBGGroup() || group->isBFGroup() ||
        group->GetDungeonDifficulty() != DUNGEON_DIFFICULTY_HEROIC)) return false;
    std::set<uint32> members;
    if (group)
        for (auto const& slot : group->GetMemberSlots()) members.insert(slot.guid.GetCounter());
    auto expected = data.at("roster").get<std::vector<uint32>>();
    if (!std::all_of(members.begin(),members.end(),[&](auto g) {
        return std::find(expected.begin(),expected.end(),g) != expected.end(); })) return false;
    bool groupProof = false;
    bool released = true;
    for (auto const& body : bodies)
    {
        uint32 guid = body.at("guid");
        uint32 map = body.at("map");
        float x = body.at("x"), y = body.at("y"), z = body.at("z"), orientation = body.at("o");
        if (!IsCast(guid) || !MapMgr::IsValidMapCoord(map,x,y,z,orientation) ||
            Realm::RealmOwns(guid) || Training::DummyTrialOwns(guid) || Training::HasTrainingJournal(guid)) return false;
        auto snapshot = CharacterDatabase.Query("SELECT c.map,c.instance_id,c.position_x,c.position_y,c.position_z,"
            "c.instance_mode_mask,c.playerFlags,COALESCE(gm.guid,0),c.online FROM characters c "
            "LEFT JOIN group_member gm ON gm.memberGuid=c.guid WHERE c.guid={}", guid);
        if (!snapshot) return false;
        auto* fields = snapshot->Fetch();
        auto durableGroup = fields[7].Get<uint32>();
        if (durableGroup && (durableGroup != groupId || (group && !members.contains(guid)))) return false;
        bool const inCopy = instance && fields[0].Get<uint32>() == 602 && fields[1].Get<uint32>() == instance;
        bool const atOrigin = fields[0].Get<uint32>() == map && !fields[1].Get<uint32>() &&
            std::hypot(fields[2].Get<float>()-x, fields[3].Get<float>()-y) <= 5 && std::abs(fields[4].Get<float>()-z) <= 5;
        if (!inCopy && !atOrigin) return false;
        if (inCopy && group && durableGroup == groupId) groupProof = true;
        uint32 mask = body.at("dungeon").get<uint32>() | (body.at("raid").get<uint32>() << 4);
        bool const leaderFlag = body.at("leader").get<bool>();
        bool const flagMatches = guid != leader || bool(fields[6].Get<uint32>() & PLAYER_FLAGS_GROUP_LEADER) == leaderFlag;
        auto* player = ObjectAccessor::FindConnectedPlayer(ObjectGuid::Create<HighGuid::Player>(guid));
        if (player)
        {
            auto* ai = GET_PLAYERBOT_AI(player);
            if (!ai || ai->GetMaster() || IsSelfBot(player) || !player->IsInWorld() || player->IsBeingTeleported() ||
                !player->GetSession() || player->GetSession()->IsLoggingOut() || player->IsInCombat() ||
                player->GetGroupInvite() || player->GetOriginalGroup() || player->GetTradeData() ||
                (player->GetGroup() && player->GetGroup() != group)) return false;
            bool origin = !player->GetInstanceId() && player->GetMapId() == map &&
                player->GetDistance2d(x,y) <= 5 && std::abs(player->GetPositionZ()-z) <= 5;
            bool copy = instance && player->GetMapId() == 602 && player->GetInstanceId() == instance;
            if (!origin && !copy) return false;
            if (!group && data.value("recovery_authorized",false))
            {
                if (copy)
                {
                    if (player->IsAlive()) player->TeleportTo(map,x,y,z,orientation);
                    return false;
                }
                if (player->GetDungeonDifficulty() != Difficulty(body.at("dungeon").get<uint32>()) ||
                    player->GetRaidDifficulty() != Difficulty(body.at("raid").get<uint32>()) ||
                    (guid == leader && player->HasPlayerFlag(PLAYER_FLAGS_GROUP_LEADER) != leaderFlag))
                {
                    player->SetDungeonDifficulty(Difficulty(body.at("dungeon").get<uint32>()));
                    player->SetRaidDifficulty(Difficulty(body.at("raid").get<uint32>()));
                    if (guid == leader && leaderFlag) player->SetPlayerFlag(PLAYER_FLAGS_GROUP_LEADER);
                    else if (guid == leader) player->RemovePlayerFlag(PLAYER_FLAGS_GROUP_LEADER);
                    player->SaveToDB(false,false);
                    return false;
                }
            }
            if (!origin || player->GetGroup()) released = false;
        }
        else if (!group && !durableGroup && !fields[8].Get<uint8>() && data.value("recovery_authorized",false)
            && (!atOrigin || mask != fields[5].Get<uint8>() || !flagMatches))
        {
            // Only a disconnected, groupless character still at its proved location qualifies.
            CharacterDatabase.DirectExecute("UPDATE characters SET map={},instance_id=0,position_x={},position_y={},"
                "position_z={},orientation={},instance_mode_mask={},playerFlags=(playerFlags & ~{}) | {} "
                "WHERE guid={} AND online=0 AND map={} AND instance_id={} AND position_x={} AND position_y={} "
                "AND position_z={} AND NOT EXISTS (SELECT 1 FROM group_member WHERE memberGuid={})",
                map,x,y,z,orientation,mask,guid==leader?uint32(PLAYER_FLAGS_GROUP_LEADER):0u,
                guid==leader && leaderFlag?uint32(PLAYER_FLAGS_GROUP_LEADER):0u,guid,fields[0].Get<uint32>(),
                fields[1].Get<uint32>(),fields[2].Get<float>(),fields[3].Get<float>(),fields[4].Get<float>(),guid);
            return false;
        }
        if (!atOrigin || durableGroup || mask != fields[5].Get<uint8>() || !flagMatches) released = false;
    }
    if (group)
    {
        if (!groupProof || members.size() != expected.size()) return false;
        auto next = data;
        next["recovery_authorized"] = true;
        if (!SaveDungeonReceipt(receipt,next)) return false;
        group->Disband();
        return false;
    }
    return released;
}

void RecoverDungeonReceipt(DungeonReceipt& receipt)
{
    auto now = NowUnixMs();
    if (!receipt.data.contains("recovery_ms"))
    {
        auto data = receipt.data;
        data["recovery_ms"] = now;
        if (!SaveDungeonReceipt(receipt,data)) return;
    }
    bool const complete = RecoverBodies(receipt);
    auto data = receipt.data;
    if (!complete && now - data.at("recovery_ms").get<int64>() < OfflineCleanupFailMs) return;
    if (data.at("terminal").is_null())
    {
        JobEvent event;
        event.id = receipt.id; event.kind = "member_dungeon"; event.state = "failed";
        event.reason = complete ? "world_restart" : "cleanup_failed:world_restart";
        event.guids = data.at("roster").get<std::vector<uint32>>();
        event.kills = data.value("kills",0u); event.wipes = data.value("wipes",0u);
        event.instanceId = data.value("instance",0u);
        data["terminal"] = MakeJobEvent(event);
        if (!complete) data["failed_ms"] = now;
    }
    data["settled"] = complete;
    if (complete) data["slot"] = false;
    if (!complete && data.contains("failed_ms") && now-data.at("failed_ms").get<int64>() >= OfflineSlotReleaseMs)
    {
        auto group = sGroupMgr->GetGroupByGUID(data.value("group",0u));
        if (!group) data["slot"] = false;
    }
    if (data != receipt.data && SaveDungeonReceipt(receipt,data)) PublishDungeonReceipt(receipt.id);
}
}

void TickDungeonRecovery(uint32 diff)
{
    if (retry > diff) { retry -= diff; return; }
    retry = 5000;
    if (!Load()) return;
    // Reconcile copies of the receipts: a successful save may erase the entry.
    auto pending = outstanding;
    for (auto& [id, row] : pending)
    {
        if (!MemberDungeonFor(row.data.at("leader").get<uint32>()).empty()) continue;
        RecoverDungeonReceipt(row);
    }
}
}
