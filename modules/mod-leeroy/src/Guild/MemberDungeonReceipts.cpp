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
#include "Log.h"
#include "Realm/RealmPorts.h"
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
std::map<std::string,DungeonWriteProof> unverified;
struct Unreadable { bool slot; std::vector<uint32> roster; };
std::map<std::string,Unreadable> unreadable;
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
            auto rows = CharacterDatabase.Query("SELECT start_id,revision,payload,CAST(COALESCE(active_slot,0) AS UNSIGNED) FROM leeroy_member_dungeon_receipt "
                "WHERE realm={} AND settled=0 LIMIT 129", ::realm.Id.Realm);
            if (!rows) return false;
            do
            {
                auto* fields = rows->Fetch();
                auto id = fields[0].Get<std::string>();
                auto raw = fields[2].Get<std::string>();
                auto data = raw.size()<=32768 ? Json::parse(raw,nullptr,false) : Json{};
                auto const slot = fields[3].Get<uint64>();
                if (!DungeonReceiptIdValid(id) || !DungeonStoredEvidence(id,fields[1].Get<uint64>(),slot,0,data))
                {
                    if (unreadable.emplace(id,Unreadable{slot!=0,DungeonKnownRoster(data)}).second)
                        LOG_ERROR("module.leeroy","leeroy: member_dungeon recovery unreadable receipt; ownership retained");
                    continue;
                }
                restored.emplace(id,DungeonReceipt{id, fields[1].Get<uint64>(), std::move(data)});
            } while (rows->NextRow());
        }
        if (restored.size()+unreadable.size() != expected) return false;
        outstanding = std::move(restored);
    }
    return loaded = true;
}

std::optional<DungeonReceipt> Lookup(std::string const& id)
{
    if (!exists || !DungeonReceiptIdValid(id) || unreadable.contains(id)) return std::nullopt;
    auto row = CharacterDatabase.Query("SELECT revision,payload,CAST(COALESCE(active_slot,0) AS UNSIGNED),settled "
        "FROM leeroy_member_dungeon_receipt WHERE realm={} AND start_id=UNHEX('{}')",
        ::realm.Id.Realm, Hex(id));
    if (!row) return std::nullopt;
    auto raw = row->Fetch()[1].Get<std::string>();
    auto data = raw.size()<=32768 ? Json::parse(raw,nullptr,false) : Json{};
    auto revision = row->Fetch()[0].Get<uint64>();
    auto slot = row->Fetch()[2].Get<uint64>();
    if (!DungeonStoredEvidence(id,revision,slot,row->Fetch()[3].Get<uint8>(),data))
    {
        if (unreadable.emplace(id,Unreadable{slot!=0,DungeonKnownRoster(data)}).second)
            LOG_ERROR("module.leeroy","leeroy: member_dungeon lookup contradictory receipt; ownership retained");
        return std::nullopt;
    }
    return DungeonReceipt{id,revision,std::move(data)};
}
}

namespace
{
void Remember(DungeonReceipt const& row)
{
    if (row.data.at("settled").get<bool>()) outstanding.erase(row.id);
    else outstanding[row.id] = row;
}

DungeonWriteRead Verify(DungeonReceipt& receipt)
{
    auto intent = unverified.find(receipt.id);
    auto row = Lookup(receipt.id);
    auto result = intent->second.Observe(row ? row->revision : 0,row ? &row->data : nullptr);
    if (result == DungeonWriteRead::Committed || result == DungeonWriteRead::Previous)
    {
        receipt = std::move(*row);
        Remember(receipt);
        unverified.erase(intent);
    }
    return result;
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
        {"terminal",nullptr},{"instance",0},{"group",0},{"handbacks",Json::object()},{"issued_ms",NowUnixMs()}};
    if (!DungeonStoredEvidence(id,1,1,0,data) || Lookup(id) || unreadable.contains(id) || unverified.contains(id)) return false;
    receipt.id = id;
    unverified.emplace(id,DungeonWriteProof{0,Json{},data});
    CharacterDatabase.DirectExecute("INSERT INTO leeroy_member_dungeon_receipt(realm,start_id,revision,active_slot,settled,payload) "
        "VALUES({},UNHEX('{}'),1,1,0,UNHEX('{}'))", ::realm.Id.Realm, Hex(id), Hex(data.dump()));
    return Verify(receipt)==DungeonWriteRead::Committed;
}

namespace
{
template<class Execute>
bool WriteReceipt(DungeonReceipt& receipt, Json const& next, std::int64_t now, uint32 handbackGuid, Execute execute)
{
    if (unverified.contains(receipt.id))
    {
        // Never rebase a new transition built from stale memory. Its caller rebuilds it from the verified row.
        auto state = Verify(receipt);
        if (state != DungeonWriteRead::Previous) return state==DungeonWriteRead::Committed && receipt.data==next;
    }
    if (receipt.data == next) return true;
    auto payload = next.dump();
    if (payload.size()>32768 || !DungeonWriteAllowed(receipt.id,receipt.revision,receipt.data,next,now,handbackGuid)) return false;
    unverified.emplace(receipt.id,DungeonWriteProof{receipt.revision,receipt.data,next});
    execute(Acore::StringFormat("UPDATE leeroy_member_dungeon_receipt SET revision=revision+1,active_slot={},settled={},payload=UNHEX('{}') "
        "WHERE realm={} AND start_id=UNHEX('{}') AND revision={}", next.at("slot").get<bool>() ? "1" : "NULL",
        unsigned(next.at("settled").get<bool>()), Hex(payload), ::realm.Id.Realm, Hex(receipt.id), receipt.revision));
    return Verify(receipt)==DungeonWriteRead::Committed;
}
}

bool SaveDungeonReceipt(DungeonReceipt& receipt, Json const& next, std::int64_t now)
{
    return WriteReceipt(receipt,next,now,0,[](std::string const& sql) { CharacterDatabase.DirectExecute(sql); });
}

bool DungeonRecoveryConflicts(std::vector<uint32> const& guids, bool requiresSlot)
{
    if (!loaded) return true;
    for (auto const& [id,bad] : unreadable)
    {
        if (requiresSlot && bad.slot) return true;
        for (auto g : guids)
            if (bad.roster.empty() || std::find(bad.roster.begin(),bad.roster.end(),g)!=bad.roster.end()) return true;
    }
    for (auto const& [id,intent] : unverified)
        if (!intent.revision)
        {
            if (requiresSlot) return true;
            auto const& roster = intent.after.at("roster");
            for (auto guid : guids) if (std::find(roster.begin(),roster.end(),guid)!=roster.end()) return true;
        }
    for (auto const& [id, row] : outstanding)
    {
        if (requiresSlot && row.data.at("slot").get<bool>()) return true;
        for (auto guid : guids)
            if (std::find(row.data.at("roster").begin(), row.data.at("roster").end(), guid) != row.data.at("roster").end())
                return true;
    }
    return false;
}

namespace
{
// Verify the character row inside the receipt CAS. A grouped body, mismatched save or missing character
// cannot mint a handback. The live save or guarded offline return can commit in this same transaction.
std::string ReturnedCharacter(Json const& body, uint32 leader)
{
    auto guid = body.at("guid").get<uint32>();
    auto mask = body.at("dungeon").get<uint32>() | (body.at("raid").get<uint32>() << 4);
    auto match = Acore::StringFormat("SELECT 1 FROM characters c WHERE c.guid={} AND c.map={} AND c.instance_id=0 "
        "AND POW(c.position_x-({}),2)+POW(c.position_y-({}),2)<=25 AND ABS(c.position_z-({}))<=5 "
        "AND c.instance_mode_mask={} AND NOT EXISTS (SELECT 1 FROM group_member gm WHERE gm.memberGuid=c.guid)",
        guid,body.at("map").get<uint32>(),body.at("x").get<float>(),body.at("y").get<float>(),body.at("z").get<float>(),mask);
    if (guid==leader) match += Acore::StringFormat(" AND (c.playerFlags & {})={}",uint32(PLAYER_FLAGS_GROUP_LEADER),
        body.at("leader")==true ? uint32(PLAYER_FLAGS_GROUP_LEADER) : 0u);
    return match;
}

bool RecordHandback(DungeonReceipt& receipt, uint32 guid, Player* player = nullptr, std::string const& offlineReturn = {},
    bool observedAtOrigin = false)
{
    if (DungeonHandbackRecorded(receipt.data,receipt.id,guid)) return true;
    auto next = receipt.data;
    auto& bodies = next.at("bodies");
    auto body = std::find_if(bodies.begin(),bodies.end(),[&](auto const& b) { return b.at("guid")==guid; });
    if (body==bodies.end() || body->at("abandoned")==true) return false;
    auto match = ReturnedCharacter(*body,next.at("leader"));
    if (!player && offlineReturn.empty())
    {
        // The live observation may have arrived before its group removal reached the database.
        if ((!observedAtOrigin && body->at("returned")!=true) || !CharacterDatabase.Query(match)) return false;
    }
    (*body)["changed"] = true;
    (*body)["returned"] = true;
    next["handbacks"][std::to_string(guid)] = receipt.id;
    return WriteReceipt(receipt,next,NowUnixMs(),guid,[&](std::string cas)
    {
        cas += " AND EXISTS (" + match + ")";
        if (!player && offlineReturn.empty()) { CharacterDatabase.DirectExecute(cas); return; }
        auto trans = CharacterDatabase.BeginTransaction();
        if (player) player->SaveToDB(trans,false,false);
        else
        {
            trans->Append(offlineReturn);
            cas += " AND ROW_COUNT()=1"; // An offline return must have changed this exact, groupless character.
        }
        trans->Append(cas);
        CharacterDatabase.DirectCommitTransaction(trans);
    });
}
}

bool ObserveDungeonHandback(DungeonReceipt& receipt, Player& player)
{
    auto guid = player.GetGUID().GetCounter();
    auto const& bodies = receipt.data.at("bodies");
    auto body = std::find_if(bodies.begin(),bodies.end(),[&](auto const& b) { return b.at("guid")==guid; });
    auto* ai = GET_PLAYERBOT_AI(&player);
    if (body==bodies.end() || body->at("abandoned")==true || !player.IsInWorld() || player.IsBeingTeleported() ||
        !ai || ai->GetMaster() || player.GetGroup() || player.GetGuildId()!=receipt.data.at("guild") ||
        DungeonLocationOf(*body,0,player.GetMapId(),player.GetInstanceId(),player.GetPositionX(),
            player.GetPositionY(),player.GetPositionZ())!=DungeonLocation::Origin ||
        player.GetDungeonDifficulty()!=Difficulty(body->at("dungeon").get<uint32>()) ||
        player.GetRaidDifficulty()!=Difficulty(body->at("raid").get<uint32>()) ||
        (guid==receipt.data.at("leader") && player.HasPlayerFlag(PLAYER_FLAGS_GROUP_LEADER)!=body->at("leader"))) return false;
    RecordHandback(receipt,guid,&player);
    return true; // World observation only. The independent proof is the exact receipt readback.
}

bool DungeonReturnsPersisted(DungeonReceipt& receipt)
{
    // A verified write replaces receipt.data. Keep this pass's manifest stable across individual commits.
    auto bodies = receipt.data.at("bodies");
    bool complete = true;
    for (auto const& body : bodies)
    {
        if (body.at("changed")==false) continue;
        auto guid = body.at("guid").get<uint32>();
        if (!DungeonHandbackRecorded(receipt.data,receipt.id,guid) &&
            (body.at("returned")!=true || !RecordHandback(receipt,guid))) complete = false;
    }
    return complete;
}

bool DungeonReturned(std::string const& id, std::vector<uint32> const& guids)
{
    if (!loaded || outstanding.contains(id) || unverified.contains(id) || unreadable.contains(id)) return false;
    auto row = Lookup(id);
    if (!row || !row->data.value("settled",false) || row->data.value("guild",0u) != LeeroyModule::Instance().GuildId() ||
        row->data.value("human",0u) != HumanGuid()) return false;
    auto const& roster = row->data.at("roster");
    return std::all_of(guids.begin(), guids.end(), [&](auto g){ return std::find(roster.begin(),roster.end(),g) != roster.end(); });
}

LeeroyCommandResult PublishDungeonReceipt(std::string const& id)
{
    if (!loaded) return {false,"dungeon_store_unavailable"};
    if (unreadable.contains(id)) return {false,"dungeon_receipt_unproven"};
    auto row = Lookup(id);
    if (!row)
    {
        if (unreadable.contains(id)) return {false,"dungeon_receipt_unproven"};
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
    event["slot_held"] = data.at("slot");
    event["map_id"] = data.at("map");
    event["revision"] = row->revision;
    LeeroyModule::Instance().Publish(EncodeEvent(data.value("terminal_ms",NowUnixMs()),event),false);
    return {true,{}};
}

namespace
{
bool MatchesParty(Json const& data, Group* group)
{
    if (!group || group->isRaidGroup() || group->isLFGGroup() || group->isBGGroup() || group->isBFGroup() ||
        group->GetDungeonDifficulty()!=DUNGEON_DIFFICULTY_HEROIC) return false;
    std::vector<uint32> members;
    for (auto const& slot : group->GetMemberSlots()) members.push_back(slot.guid.GetCounter());
    return DungeonMatchesGroup(data,group->GetGUID().GetCounter(),group->GetLeaderGUID().GetCounter(),members);
}

// With a group present this is a read-only check for disband. Once it is gone each body has
// independent authority. No return below can prevent the caller from checking its neighbors.
bool RecoverBody(DungeonReceipt& receipt, Json const& body, Group* group, bool& groupProof)
{
    auto const& data = receipt.data;
    auto const groupId = data.value("group",0u);
    auto const instance = data.value("instance",0u);
    auto const leader = data.at("leader").get<uint32>();
    uint32 guid = body.at("guid");
    if (DungeonHandbackRecorded(data,receipt.id,guid)) return !group;
    if (body.at("abandoned")==true) return false;
    uint32 map = body.at("map");
    float x = body.at("x"), y = body.at("y"), z = body.at("z"), orientation = body.at("o");
    if (!IsCast(guid) || guid == HumanGuid() || !Realm::IsOnRandomBotAccount(guid) ||
        !Realm::IsConfiguredGuildMember(guid) || !MapMgr::IsValidMapCoord(map,x,y,z,orientation) ||
        Realm::RealmOwns(guid) || Training::DummyTrialOwns(guid) || Training::HasTrainingJournal(guid) ||
        Training::TrainingDcOwns(guid) || Training::TrainingCleanupPending() || HasPendingBotCommands({guid})) return false;
    auto snapshot = CharacterDatabase.Query("SELECT c.map,c.instance_id,c.position_x,c.position_y,c.position_z,"
        "c.instance_mode_mask,c.playerFlags,COALESCE(gm.guid,0),c.online FROM characters c "
        "LEFT JOIN group_member gm ON gm.memberGuid=c.guid WHERE c.guid={}", guid);
    if (!snapshot) return false;
    auto* fields = snapshot->Fetch();
    auto durableGroup = fields[7].Get<uint32>();
    if (group ? durableGroup!=groupId : durableGroup!=0) return false;
    auto const storedPlace = DungeonLocationOf(body,instance,fields[0].Get<uint32>(),fields[1].Get<uint32>(),
        fields[2].Get<float>(),fields[3].Get<float>(),fields[4].Get<float>());
    if (storedPlace==DungeonLocation::Foreign) return false;
    bool const atOrigin = storedPlace==DungeonLocation::Origin;
    bool const inCopy = storedPlace==DungeonLocation::Copy;
    if (inCopy && group) groupProof = true;
    uint32 mask = body.at("dungeon").get<uint32>() | (body.at("raid").get<uint32>() << 4);
    bool const leaderFlag = body.at("leader").get<bool>();
    bool const flagMatches = guid != leader || bool(fields[6].Get<uint32>() & PLAYER_FLAGS_GROUP_LEADER) == leaderFlag;
    bool const storedOrigin = atOrigin && mask==fields[5].Get<uint8>() && flagMatches;
    auto const effect = data.value("effect",std::string{});
    bool const changed = body.at("changed")==true || (data.value("actor",0u)==guid &&
        (effect=="create_group" || effect=="add_group"));
    bool const authorized = DungeonReturnAuthorized(data);
    auto* player = ObjectAccessor::FindConnectedPlayer(ObjectGuid::Create<HighGuid::Player>(guid));
    if (player)
    {
        auto* ai = GET_PLAYERBOT_AI(player);
        auto queue = sLFGMgr->GetState(player->GetGUID());
        if (!ai || ai->GetMaster() || IsSelfBot(player) || !player->IsInWorld() || player->IsBeingTeleported() ||
            player->GetGuildId()!=data.at("guild") || player->IsGameMaster() || player->HasPendingBind() ||
            player->IsInFlight() || player->GetVehicle() || player->GetTransport() || player->duel ||
            player->InBattleground() || player->InBattlegroundQueue() || StringStayHeld(guid) ||
            (queue!=lfg::LFG_STATE_NONE && queue!=lfg::LFG_STATE_RAIDBROWSER) ||
            !player->GetSession() || player->GetSession()->IsLoggingOut() || player->IsInCombat() ||
            player->GetGroupInvite() || player->GetOriginalGroup() || player->GetTradeData() ||
            player->GetGroup() != group) return false;
        auto const place = DungeonLocationOf(body,instance,player->GetMapId(),player->GetInstanceId(),
            player->GetPositionX(),player->GetPositionY(),player->GetPositionZ());
        if (place==DungeonLocation::Foreign) return false;
        if (group) return true;
        bool const firstIntent = !authorized && DungeonFirstGroupActor(data,guid);
        if (changed && !authorized && body.at("returned")!=true && !firstIntent) return false;
        if (changed && authorized)
        {
            if (place==DungeonLocation::Copy)
            {
                if (player->IsAlive()) player->TeleportTo(map,x,y,z,orientation);
                return false;
            }
            player->SetDungeonDifficulty(Difficulty(body.at("dungeon").get<uint32>()));
            player->SetRaidDifficulty(Difficulty(body.at("raid").get<uint32>()));
            if (guid == leader && leaderFlag) player->SetPlayerFlag(PLAYER_FLAGS_GROUP_LEADER);
            else if (guid == leader) player->RemovePlayerFlag(PLAYER_FLAGS_GROUP_LEADER);
        }
        if (place!=DungeonLocation::Origin || player->GetDungeonDifficulty() != Difficulty(body.at("dungeon").get<uint32>()) ||
            player->GetRaidDifficulty() != Difficulty(body.at("raid").get<uint32>()) ||
            (guid == leader && player->HasPlayerFlag(PLAYER_FLAGS_GROUP_LEADER) != leaderFlag)) return false;
        if (firstIntent)
        {
            // The live postconditions above and the independent stored row must already agree.
            // Compare again inside the receipt CAS without saving or changing the character.
            return player->GetGUID().GetCounter()==guid && storedOrigin && RecordHandback(receipt,guid,nullptr,{},true);
        }
        return changed ? RecordHandback(receipt,guid,player) : storedOrigin;
    }
    if (fields[8].Get<uint8>()) return false; // A session exists but cannot be inspected.
    if (group) return true;
    if (!changed) return storedOrigin;
    if (storedOrigin) return body.at("returned")==true && RecordHandback(receipt,guid);
    if (!authorized) return false;
    // The character and proof commit together only for a scoped, observed state that needs a real change.
    auto restore = Acore::StringFormat("UPDATE characters SET map={},instance_id=0,position_x={},position_y={},"
            "position_z={},orientation={},instance_mode_mask={},playerFlags=(playerFlags & ~{}) | {} "
            "WHERE guid={} AND online=0 AND map={} AND instance_id={} AND position_x={} AND position_y={} "
            "AND position_z={} AND NOT EXISTS (SELECT 1 FROM group_member WHERE memberGuid={})",
            map,x,y,z,orientation,mask,guid==leader?uint32(PLAYER_FLAGS_GROUP_LEADER):0u,
            guid==leader && leaderFlag?uint32(PLAYER_FLAGS_GROUP_LEADER):0u,guid,fields[0].Get<uint32>(),
            fields[1].Get<uint32>(),fields[2].Get<float>(),fields[3].Get<float>(),fields[4].Get<float>(),guid);
    restore += Acore::StringFormat(" AND instance_mode_mask={} AND playerFlags={} AND EXISTS "
        "(SELECT 1 FROM leeroy_member_dungeon_receipt WHERE realm={} AND start_id=UNHEX('{}') AND revision={} AND settled=0)",
        unsigned(fields[5].Get<uint8>()),fields[6].Get<uint32>(),::realm.Id.Realm,Hex(receipt.id),receipt.revision);
    return RecordHandback(receipt,guid,nullptr,restore);
}

bool RecoverBodies(DungeonReceipt& receipt)
{
    auto const& data = receipt.data;
    if (DungeonBeforeMutation(data)) return true;
    if (data.at("guild") != LeeroyModule::Instance().GuildId() || data.at("human") != HumanGuid()) return false;
    auto bodies = data.at("bodies"); // Per-member commits replace the verified payload.
    if (bodies.size()!=5) return false;
    auto groupId = data.value("group",0u);
    auto* group = groupId ? sGroupMgr->GetGroupByGUID(groupId) : nullptr;
    if (group && (!MatchesParty(data,group) || group->GetMembersCount()!=bodies.size())) return false;
    bool released = true, groupProof = false;
    for (auto const& body : bodies)
    {
        if (!RecoverBody(receipt,body,group,groupProof)) released = false;
    }
    if (group)
    {
        if (!released || !groupProof) return false;
        auto next = data;
        AuthorizeDungeonReturn(next);
        if (!SaveDungeonReceipt(receipt,next)) return false;
        group->Disband();
        return false;
    }
    return released;
}

bool HeldAfterRestart(Json const& data)
{
    auto* group = sGroupMgr->GetGroupByGUID(data.at("group").get<uint32>());
    if (!MatchesParty(data,group)) return false;
    auto const instance = data.at("instance").get<uint32>();
    auto* driver = FindOnline(data.value("driver",0u));
    if (!driver || driver->GetGUID().GetCounter()==HumanGuid() || IsCast(driver->GetGUID().GetCounter())) return false;
    for (auto const& body : data.at("bodies"))
    {
        auto guid = body.at("guid").get<uint32>();
        auto* p = FindOnline(guid);
        if (body.at("changed")!=true || body.at("returned")==true || body.at("abandoned")==true ||
            !p || p->IsBeingTeleported() || !IsCast(guid) || !Realm::IsOnRandomBotAccount(guid) ||
            p->GetGuildId()!=data.at("guild") || p->GetGroup()!=group || IsSelfBot(p) ||
            !GET_PLAYERBOT_AI(p) || GET_PLAYERBOT_AI(p)->GetMaster()!=driver) continue;
        if (DungeonLocationOf(body,instance,p->GetMapId(),p->GetInstanceId(),p->GetPositionX(),
                p->GetPositionY(),p->GetPositionZ())!=DungeonLocation::Foreign) return true;
    }
    return false;
}

void RecoverDungeonReceipt(DungeonReceipt& receipt)
{
    if (!receipt.data.contains("recovery_ms"))
    {
        auto data = receipt.data;
        data["recovery_ms"] = NowUnixMs();
        if (!SaveDungeonReceipt(receipt,data)) return;
    }
    bool const complete = RecoverBodies(receipt);
    auto const& verified = receipt.data;
    auto const now = NowUnixMs();
    if (!complete && now - verified.at("recovery_ms").get<int64>() < OfflineCleanupFailMs) return;
    Json terminal;
    if (verified.at("terminal").is_null())
    {
        JobEvent event;
        event.id = receipt.id; event.kind = "member_dungeon"; event.state = "failed";
        event.reason = complete ? "world_restart" : "cleanup_failed:world_restart";
        event.guids = verified.at("roster").get<std::vector<uint32>>();
        event.kills = verified.value("kills",0u); event.wipes = verified.value("wipes",0u);
        event.instanceId = verified.value("instance",0u);
        terminal = MakeJobEvent(event);
    }
    auto data = DungeonTerminalUpdate(verified,terminal,complete,now);
    if (!complete && verified.at("slot")==true)
        if (auto age=DungeonFailureAge(verified,now); age && DungeonCanReleaseSlot(true,*age,false) && !HeldAfterRestart(verified))
            data["slot"] = false;
    if (data != receipt.data && SaveDungeonReceipt(receipt,data,now)) PublishDungeonReceipt(receipt.id);
}
}

void TickDungeonRecovery(uint32 diff)
{
    if (retry > diff) { retry -= diff; return; }
    retry = 5000;
    if (!Load()) return;
    // A failed first readback never started the runner. Discover its exact committed insertion or prove absence.
    std::vector<std::string> starts;
    for (auto const& [id,write] : unverified) if (!write.revision) starts.push_back(id);
    for (auto const& id : starts)
    {
        DungeonReceipt receipt{id,0,Json{}};
        auto state = Verify(receipt);
        if (state==DungeonWriteRead::Committed) continue;
        if (state==DungeonWriteRead::Conflict)
        {
            if (auto existing=Lookup(id)) { Remember(*existing); unverified.erase(id); }
            continue;
        }
        auto count = CharacterDatabase.Query("SELECT COUNT(*) FROM leeroy_member_dungeon_receipt WHERE realm={} AND start_id=UNHEX('{}')",
            ::realm.Id.Realm,Hex(id));
        if (count && !count->Fetch()[0].Get<uint64>()) unverified.erase(id);
    }
    // Reconcile copies of the receipts: a successful save may erase the entry.
    auto pending = outstanding;
    for (auto& [id, row] : pending)
    {
        if (unreadable.contains(id)) continue;
        try
        {
            if (MemberDungeonFor(row.data.at("leader").get<uint32>())==id) continue;
            if (unverified.contains(id))
            {
                auto read = Verify(row);
                if ((read!=DungeonWriteRead::Committed && read!=DungeonWriteRead::Previous) || row.data.at("settled")==true) continue;
            }
            RecoverDungeonReceipt(row);
        }
        catch (Json::exception const&)
        {
            if (unreadable.emplace(id,Unreadable{true,DungeonKnownRoster(row.data)}).second)
                LOG_ERROR("module.leeroy","leeroy: member_dungeon recovery invalid field; ownership retained");
        }
    }
}
}
