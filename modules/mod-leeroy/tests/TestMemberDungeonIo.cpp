// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyMemberDungeon.h"
#include "LeeroyGuildCommands.h"
#include "LeeroyOfflineRaidPolicy.h"
#include "LeeroyProtocol.h"
#include "TestHarness.h"
#include <algorithm>
#include <cmath>
#include <deque>
#include <iomanip>
#include <map>
#include <memory>
#include <set>
#include <stdexcept>
#include <unordered_map>
#include <unordered_set>

using uint8 = std::uint8_t;
using uint32 = std::uint32_t;
using uint64 = std::uint64_t;
using int64 = std::int64_t;
using Json = nlohmann::json;
#define NowUnixMs ReceiptTestNowUnixMs
template<class... Args> void ReceiptLog(Args const&...) {}
#define LOG_ERROR(...) ReceiptLog(__VA_ARGS__)
#define LOG_INFO(...) ReceiptLog(__VA_ARGS__)
namespace Acore
{
template<class T> std::string SqlValue(T const& value)
{
    std::ostringstream out;
    out << std::setprecision(9) << value;
    return out.str();
}
template<class... Args> std::string StringFormat(std::string_view format, Args const&... args)
{
    std::string text(format);
    std::size_t offset = 0;
    auto add = [&](auto const& arg)
    {
        auto at = text.find("{}",offset);
        auto value = SqlValue(arg);
        text.replace(at,2,value);
        offset = at+value.size();
    };
    (add(args),...);
    return text;
}
}
struct ReceiptField
{
    std::string value;
    template<class T> T Get() const
    {
        if constexpr (std::is_same_v<T,std::string>) return value;
        else if constexpr (std::is_floating_point_v<T>) return static_cast<T>(std::stod(value));
        else return static_cast<T>(std::stoull(value));
    }
};
struct ReceiptRows
{
    std::vector<std::vector<ReceiptField>> rows;
    std::size_t at = 0;
    ReceiptField* Fetch() { return rows.at(at).data(); }
    bool NextRow() { return ++at < rows.size(); }
};
using QueryResult = std::shared_ptr<ReceiptRows>;
struct ReceiptRead { std::string sql; bool ready=false; QueryResult result; };
struct ReceiptTransaction
{
    std::vector<std::string> statements;
    void Append(std::string const& text) { statements.push_back(text); }
    template<class... Args> void Append(std::string_view text, Args const&... args)
        { statements.push_back(Acore::StringFormat(text,args...)); }
};
using CharacterDatabaseTransaction = std::shared_ptr<ReceiptTransaction>;
struct ReceiptWrite { CharacterDatabaseTransaction transaction; bool ready=false, success=false; };
struct QueryCallback
{
    std::shared_ptr<ReceiptRead> read;
    std::function<void(QueryResult)> callback;
    void WithCallback(std::function<void(QueryResult)> fn) { callback=std::move(fn); }
    bool InvokeIfReady()
    {
        if (!read->ready || !callback) return false;
        auto fn=std::move(callback); fn(read->result); return true;
    }
};
struct TransactionCallback
{
    std::shared_ptr<ReceiptWrite> write;
    std::function<void(bool)> callback;
    void AfterComplete(std::function<void(bool)> fn) { callback=std::move(fn); }
    bool InvokeIfReady()
    {
        if (!write->ready || !callback) return false;
        auto fn=std::move(callback); fn(write->success); return true;
    }
};
// Deliberately no Query, DirectExecute or DirectCommitTransaction methods.
struct ReceiptDatabase
{
    std::deque<std::shared_ptr<ReceiptRead>> queries;
    std::deque<std::shared_ptr<ReceiptWrite>> transactions;
    QueryCallback AsyncQuery(std::string const& sql)
    {
        auto read=std::make_shared<ReceiptRead>(); read->sql=sql; queries.push_back(read);
        return {read,{}};
    }
    CharacterDatabaseTransaction BeginTransaction() { return std::make_shared<ReceiptTransaction>(); }
    TransactionCallback AsyncCommitTransaction(CharacterDatabaseTransaction transaction)
    {
        auto write=std::make_shared<ReceiptWrite>(); write->transaction=std::move(transaction);
        transactions.push_back(write); return {write,{}};
    }
} CharacterDatabase;
int64 receiptClock=1000000;
int64 NowUnixMs() { return receiptClock; }
struct { struct { uint32 Realm=1; } Id; } realm;
struct LeeroyCommandResult { bool ok=false; std::string error; bool deferred=false; };
enum class HighGuid { Player };
struct ObjectGuid
{
    uint32 low=0;
    template<HighGuid> static ObjectGuid Create(uint32 g) { return {g}; }
    uint32 GetCounter() const { return low; }
};
enum Difficulty { DUNGEON_DIFFICULTY_NORMAL, DUNGEON_DIFFICULTY_HEROIC };
constexpr uint32 PLAYER_FLAGS_GROUP_LEADER=2;
struct Group
{
    uint32 id=90,leader=101;
    struct Slot { ObjectGuid guid; };
    std::vector<Slot> slots;
    bool disbanded=false;
    bool isRaidGroup()const{return false;} bool isLFGGroup()const{return false;}
    bool isBGGroup()const{return false;} bool isBFGroup()const{return false;}
    auto GetDungeonDifficulty()const{return DUNGEON_DIFFICULTY_HEROIC;}
    auto const& GetMemberSlots()const{return slots;}
    ObjectGuid GetGUID()const{return {id};} ObjectGuid GetLeaderGUID()const{return {leader};}
    auto GetMembersCount()const{return slots.size();} void Disband(){disbanded=true;}
};
class Player;
struct PlayerbotAI
{
    Player* master=nullptr; Player* GetMaster(){return master;}
    struct Run {bool enabled=true;} run;
    unsigned disabled=0;
    template<class E> void DoSpecificAction(char const*,E const&,bool){}
};
struct Event {Event(char const*,char const*,Player*){}};
struct DcRun {static auto& Of(PlayerbotAI* ai){return ai->run;}};
struct DcActionShared
{
    static void DisableDungeonClear(PlayerbotAI* ai,char const*){ai->run.enabled=false;++ai->disabled;}
};
struct ReceiptSession {bool IsLoggingOut(){return false;}};
class Player
{
public:
    struct Instance { uint32 GetEncounterCount(){return 4;} uint8 GetBossState(uint32){return 0;} } script;
    auto* GetInstanceScript(){return &script;}
    uint32 guid=101,map=1,instance=0,flags=0,teleports=0,saves=0;
    float x=101,y=8,z=9; bool duel=false;
    Group* group=nullptr; PlayerbotAI ai; ReceiptSession session;
    Difficulty dungeon=DUNGEON_DIFFICULTY_NORMAL,raid=DUNGEON_DIFFICULTY_NORMAL;
    ObjectGuid GetGUID(){return {guid};} bool IsInWorld(){return true;} bool IsBeingTeleported(){return false;}
    bool IsAlive(){return true;} bool IsInCombat(){return false;} bool IsGameMaster(){return false;}
    bool HasPendingBind(){return false;} bool IsInFlight(){return false;} bool GetVehicle(){return false;}
    bool GetTransport(){return false;} bool InBattleground(){return false;} bool InBattlegroundQueue(){return false;}
    bool GetGroupInvite(){return false;} bool GetOriginalGroup(){return false;} bool GetTradeData(){return false;}
    uint32 GetGuildId(){return 21;} uint32 GetMapId(){return map;} uint32 GetInstanceId(){return instance;}
    float GetPositionX(){return x;} float GetPositionY(){return y;} float GetPositionZ(){return z;}
    Group* GetGroup(){return group;} ReceiptSession* GetSession(){return &session;}
    auto GetDungeonDifficulty(){return dungeon;} auto GetRaidDifficulty(){return raid;}
    void SetDungeonDifficulty(Difficulty d){dungeon=d;} void SetRaidDifficulty(Difficulty d){raid=d;}
    bool HasPlayerFlag(uint32 flag){return (flags&flag)!=0;}
    void SetPlayerFlag(uint32 flag){flags|=flag;} void RemovePlayerFlag(uint32 flag){flags&=~flag;}
    void TeleportTo(uint32 m,float a,float b,float c,float){++teleports;map=m;instance=0;x=a;y=b;z=c;}
    void SaveToDB(CharacterDatabaseTransaction trans,bool,bool){++saves;trans->Append("SAVE_CHARACTER");}
};
#define GET_PLAYERBOT_AI(player) (&(player)->ai)
std::map<uint32,Player*> receiptConnected;
Player* FindOnline(uint32 guid)
{
    auto found=receiptConnected.find(guid);return found==receiptConnected.end()?nullptr:found->second;
}
struct ObjectAccessor { static Player* FindConnectedPlayer(ObjectGuid g){return FindOnline(g.low);} };
bool IsSelfBot(Player*){return false;} bool IsCast(uint32 g){return g>=101&&g<=105;} uint32 HumanGuid(){return 1;}
struct MapMgr
{
    static bool IsValidMapCoord(uint32 map,float x,float y,float z,float o){return map&&std::isfinite(x+y+z+o);}
};
struct ReceiptGroups
{
    Group* active=nullptr;
    Group* GetGroupByGUID(uint32 id){return active&&!active->disbanded&&active->id==id?active:nullptr;}
} receiptGroups;
auto sGroupMgr=&receiptGroups;
namespace lfg {enum State {LFG_STATE_NONE,LFG_STATE_RAIDBROWSER};}
struct ReceiptLfg {auto GetState(ObjectGuid){return lfg::LFG_STATE_NONE;}} receiptLfg;
auto sLFGMgr=&receiptLfg;
struct LeeroyModule
{
    struct Replies
    {
        uint64 serial=7;bool connected=true;std::vector<std::string> lines;
        bool Reply(uint64 source,std::string line)
        {
            if(!connected || source!=serial)return false;
            lines.push_back(std::move(line));return true;
        }
    } replies;
    std::vector<Json> events;
    static auto& Instance(){static LeeroyModule value;return value;}
    auto& Bridge(){return replies;}
    uint32 GuildId(){return 21;}
    void Publish(Json const& event,bool)
        { events.push_back(event.is_string()?Json::parse(event.get<std::string>()):event); }
};
Json EncodeEvent(int64,Json const& event){return event;}
namespace Leeroy::Training
{
bool DummyTrialOwns(uint32){return false;} bool HasTrainingJournal(uint32){return false;}
bool TrainingDcOwns(uint32){return false;} bool TrainingCleanupPending(){return false;}
}
namespace Leeroy::Realm
{
bool IsOnRandomBotAccount(uint32 g){return IsCast(g);}
bool IsConfiguredGuildMember(uint32 g){return IsCast(g);} bool RealmOwns(uint32){return false;}
}
namespace Leeroy::Guild
{
bool receiptEnabled=true;
std::string receiptLive;
bool MemberDungeonEnabled(){return receiptEnabled;}
bool HasPendingBotCommands(std::vector<uint32>const&){return false;}
bool StringStayHeld(uint32){return false;}
std::string MemberDungeonFor(uint32){return receiptLive;}
}
#include "MemberDungeonReceipts.h.inc"
#include "MemberDungeonReceipts.cpp.inc"

namespace
{
using namespace Leeroy::Guild;
QueryResult Result(std::initializer_list<std::string> fields)
{
    auto result=std::make_shared<ReceiptRows>();
    std::vector<ReceiptField> row;
    for(auto const& value:fields)row.push_back({value});
    result->rows.push_back(std::move(row));return result;
}
void Fresh(bool ready=true)
{
    reads.clear();writes.clear();outstanding.clear();unverified.clear();unreadable.clear();
    loaded=exists=ready;retry=0;receiptClock=1000000;
    CharacterDatabase.queries.clear();CharacterDatabase.transactions.clear();
    receiptConnected.clear();receiptGroups.active=nullptr;receiptLive.clear();receiptEnabled=true;
    LeeroyModule::Instance().replies={};
    LeeroyModule::Instance().events.clear();
}
void Answer(QueryResult result)
{
    auto query=CharacterDatabase.queries.back();query->result=std::move(result);query->ready=true;
}
void Commit(bool success=true)
{
    auto write=CharacterDatabase.transactions.back();write->success=success;write->ready=true;
}
void Row(DungeonReceipt const& receipt,Json const& data,uint64 revision)
{
    (void)receipt;
    Answer(Result({"1",std::to_string(revision),data.dump(),data.at("slot")==true?"1":"0",data.at("settled")==true?"1":"0"}));
}
DungeonReceipt Accepted()
{
    DungeonReceipt receipt;
    if(!BeginDungeonReceipt(receipt,"test-start",21,1,101,{101,102,103,104,105}))
        throw std::runtime_error("accept failed");
    CHECK(PollDungeonReceipt(receipt)==DungeonPersistence::Pending);
    Commit();
    CHECK(PollDungeonReceipt(receipt)==DungeonPersistence::Pending);
    Row(receipt,receipt.data,1);
    CHECK(PollDungeonReceipt(receipt)==DungeonPersistence::Ready);
    return receipt;
}
Json Manifest()
{
    auto data=Accepted().data;
    data["stage"]="tearing_down";data["group"]=90;data["instance"]=47;
    for(uint32 guid=101;guid<=105;++guid)
        data["bodies"].push_back({{"guid",guid},{"map",1},{"x",guid},{"y",8},{"z",9},{"o",0},
            {"dungeon",0},{"raid",0},{"leader",false},{"changed",true},{"moved",true},{"returned",false},{"abandoned",false}});
    AuthorizeDungeonReturn(data);
    return data;
}
}

LEEROY_TEST(MemberDungeonIoFailedInitialLoadRetriesWithoutClaimingEveryBody)
{
    Fresh(false);
    CHECK(!Load());Answer({});CHECK(!Load());
    CHECK(!DungeonRecoveryConflicts({101},false));
    CHECK(DungeonRecoveryConflicts({101},true));
    unreadable.emplace("known",Unreadable{false,{102}});
    CHECK(DungeonRecoveryConflicts({102},false));
    CHECK(!DungeonRecoveryConflicts({101},false));
    receiptClock+=1001;
    CHECK(!Load());Answer(Result({"0"}));CHECK(Load());
    CHECK(!exists);
    CHECK_EQ(CharacterDatabase.queries.size(),2u);
}

LEEROY_TEST(MemberDungeonIoInsertWaitsForCommitAndFreshIndependentReadback)
{
    Fresh();DungeonReceipt receipt;
    CHECK(BeginDungeonReceipt(receipt,"test-start",21,1,101,{101,102,103,104,105}));
    CHECK_EQ(receipt.revision,0u);
    CHECK(DungeonRecoveryConflicts({101},false));
    CHECK(!DungeonRecoveryConflicts({999},false));
    for(int i=0;i<1000;++i)CHECK(PollDungeonReceipt(receipt)==DungeonPersistence::Pending);
    CHECK(CharacterDatabase.queries.empty());
    // An older independent read must not become evidence for a later write.
    CHECK(!Lookup(receipt.id));Answer(Result({"0"}));CHECK(!Lookup(receipt.id));
    Commit();CHECK(PollDungeonReceipt(receipt)==DungeonPersistence::Pending);
    CHECK_EQ(CharacterDatabase.queries.size(),2u);
    Row(receipt,receipt.data,1);CHECK(PollDungeonReceipt(receipt)==DungeonPersistence::Ready);
    CHECK_EQ(receipt.revision,1u);CHECK(!DungeonReceiptPending(receipt.id));
    CHECK_EQ(CharacterDatabase.transactions.size(),1u);
    auto const& statements=CharacterDatabase.transactions.front()->transaction->statements;
    CHECK(std::find(statements.begin(),statements.end(),"COMMIT")==statements.end());
}

LEEROY_TEST(MemberDungeonIoLostReadbackRetainsOwnershipAndRetries)
{
    Fresh();auto receipt=Accepted();auto next=receipt.data;next["recovery_ms"]=receiptClock;
    CHECK(!SaveDungeonReceipt(receipt,next));Commit();
    CHECK(PollDungeonReceipt(receipt)==DungeonPersistence::Pending);Answer({});
    CHECK(PollDungeonReceipt(receipt)==DungeonPersistence::Pending);
    CHECK_EQ(receipt.revision,1u);CHECK(DungeonRecoveryConflicts({101},false));
    receiptClock+=1001;CHECK(PollDungeonReceipt(receipt)==DungeonPersistence::Pending);
    Row(receipt,next,2);CHECK(PollDungeonReceipt(receipt)==DungeonPersistence::Ready);
    CHECK_EQ(receipt.revision,2u);CHECK(receipt.data==next);
    CHECK(SaveDungeonReceipt(receipt,next));CHECK_EQ(CharacterDatabase.transactions.size(),2u);
}

LEEROY_TEST(MemberDungeonIoFailedInitialInsertNeedsSuccessfulAbsenceProof)
{
    Fresh();DungeonReceipt receipt;
    CHECK(BeginDungeonReceipt(receipt,"test-start",21,1,101,{101,102,103,104,105}));
    Commit(false);CHECK(PollDungeonReceipt(receipt)==DungeonPersistence::Pending);Answer({});
    CHECK(PollDungeonReceipt(receipt)==DungeonPersistence::Pending);
    CHECK(DungeonRecoveryConflicts({101},false));
    receiptClock+=1001;CHECK(PollDungeonReceipt(receipt)==DungeonPersistence::Pending);
    Answer(Result({"0"}));CHECK(PollDungeonReceipt(receipt)==DungeonPersistence::Failed);
    CHECK(!DungeonRecoveryConflicts({101},false));CHECK(!DungeonReceiptPending(receipt.id));
}

LEEROY_TEST(MemberDungeonIoUnchangedFailedCheckpointCanRetryWithoutAdvancing)
{
    Fresh();auto receipt=Accepted();auto before=receipt.data;auto next=before;next["recovery_ms"]=receiptClock;
    CHECK(!SaveDungeonReceipt(receipt,next));Commit(false);
    CHECK(PollDungeonReceipt(receipt)==DungeonPersistence::Pending);Row(receipt,before,1);
    CHECK(PollDungeonReceipt(receipt)==DungeonPersistence::Ready);
    CHECK(receipt.data==before);CHECK_EQ(receipt.revision,1u);
    CHECK(!SaveDungeonReceipt(receipt,next));CHECK_EQ(CharacterDatabase.transactions.size(),3u);
    for(int i=0;i<10;++i)CHECK(!SaveDungeonReceipt(receipt,next));
    CHECK_EQ(CharacterDatabase.transactions.size(),3u);
}

LEEROY_TEST(MemberDungeonIoContradictoryReceiptKeepsKnownRosterFenced)
{
    Fresh();auto receipt=Accepted();auto next=receipt.data;next["recovery_ms"]=receiptClock;
    CHECK(!SaveDungeonReceipt(receipt,next));Commit();
    CHECK(PollDungeonReceipt(receipt)==DungeonPersistence::Pending);
    Row(receipt,receipt.data,99);CHECK(PollDungeonReceipt(receipt)==DungeonPersistence::Failed);
    CHECK_EQ(receipt.revision,1u);CHECK(DungeonRecoveryConflicts({101},false));
    CHECK(!DungeonRecoveryConflicts({999},false));
}

LEEROY_TEST(MemberDungeonIoOfflineReturnQueuesGuardedFloatTolerantTransaction)
{
    Fresh();auto data=Manifest();DungeonReceipt receipt{"test-start",15,data};Remember(receipt);
    bool proof=false;
    CHECK(!RecoverBody(receipt,data["bodies"][0],nullptr,proof));
    CHECK_EQ(CharacterDatabase.transactions.size(),1u);
    Answer(Result({"602","47","123.456789","456.789012","12.345678","1","0","0","0"}));
    CHECK(!RecoverBody(receipt,data["bodies"][0],nullptr,proof));
    CHECK_EQ(CharacterDatabase.transactions.size(),2u);
    auto const& statements=CharacterDatabase.transactions.back()->transaction->statements;
    CHECK_EQ(statements.size(),2u);
    CHECK(statements[0].find("ABS(position_x-(")!=std::string::npos);
    CHECK(statements[0].find("ABS(position_y-(")!=std::string::npos);
    CHECK(statements[0].find("ABS(position_z-(")!=std::string::npos);
    CHECK(statements[0].find("<=0.01")!=std::string::npos);
    CHECK(statements[0].find("guid=101 AND online=0 AND map=602 AND instance_id=47")!=std::string::npos);
    CHECK(statements[0].find("NOT EXISTS (SELECT 1 FROM group_member")!=std::string::npos);
    CHECK(statements[0].find("revision=15 AND settled=0")!=std::string::npos);
    CHECK(statements[1].find("AND EXISTS (SELECT 1 FROM characters")!=std::string::npos);
    CHECK(statements[1].find("AND ROW_COUNT()=1")!=std::string::npos);
    CHECK(!DungeonHandbackRecorded(receipt.data,receipt.id,101));
    auto committed=unverified.at(receipt.id).after;Commit();
    CHECK(PollDungeonReceipt(receipt)==DungeonPersistence::Pending);Row(receipt,committed,16);
    CHECK(PollDungeonReceipt(receipt)==DungeonPersistence::Ready);
    CHECK(DungeonHandbackRecorded(receipt.data,receipt.id,101));
    CHECK(!DungeonHandbackRecorded(receipt.data,receipt.id,102));
}

LEEROY_TEST(MemberDungeonIoDelayedSnapshotNeverMovesLiveBody)
{
    Fresh();auto data=Manifest();DungeonReceipt receipt{"test-start",15,data};Remember(receipt);
    Player player;player.map=602;player.instance=47;player.x=12;receiptConnected[101]=&player;
    bool proof=false;
    for(int i=0;i<1000;++i)CHECK(!RecoverBody(receipt,data["bodies"][0],nullptr,proof));
    CHECK_EQ(player.teleports,0u);CHECK_EQ(player.saves,0u);
    CHECK_EQ(CharacterDatabase.transactions.size(),1u);
    Answer(Result({"602","47","12","8","9","1","0","0","1"}));
    CHECK(!RecoverBody(receipt,data["bodies"][0],nullptr,proof));CHECK_EQ(player.teleports,1u);
}

LEEROY_TEST(MemberDungeonIoReadCompletedBetweenRecoveryPassesIsConsumedOnce)
{
    Fresh(false);CHECK(!Load());Answer(Result({"1"}));PollIo();
    receiptClock+=5000;CHECK(!Load());Answer(Result({"0"}));PollIo();
    receiptClock+=5000;CHECK(Load());CHECK(exists);
    CHECK_EQ(CharacterDatabase.queries.size(),2u);
}

LEEROY_TEST(MemberDungeonIoCompletedReadSurvivesABoundedFullRecoverySweep)
{
    Fresh();CHECK(!Read("SELECT delayed",5000));Answer(Result({"1"}));PollIo();
    // Four of 128 receipts per five seconds: the next turn can be 160 seconds away.
    receiptClock+=165000;PollIo();
    CHECK(Read("SELECT delayed",5000)!=nullptr);
    CHECK_EQ(CharacterDatabase.queries.size(),1u);
    receiptClock+=60000;PollIo();CHECK(reads.empty());
}

LEEROY_TEST(MemberDungeonIoPartialStoreSnapshotRetainsDiscoveredOwners)
{
    Fresh();auto data=Accepted().data;
    Fresh(false);CHECK(!Load());Answer(Result({"1"}));
    CHECK(!Load());Answer(Result({"2"}));
    CHECK(!Load());Answer(Result({"test-start","1",data.dump(),"1"}));
    CHECK(!Load());CHECK(!loaded);
    CHECK(DungeonRecoveryConflicts({101},false));
    CHECK(!DungeonRecoveryConflicts({999},false));
    CHECK(DungeonRecoveryConflicts({},true));
}

LEEROY_TEST(MemberDungeonIoCommittedTerminalIsImmutableWhileHandbacksWait)
{
    Fresh();auto data=Manifest();
    JobEvent event;event.id="test-start";event.kind="member_dungeon";event.state="failed";
    event.reason="cleanup_failed:world_restart";event.guids={101,102,103,104,105};event.instanceId=47;
    data["terminal"]=MakeJobEvent(event);data["terminal_ms"]=receiptClock-100;
    DungeonReceipt receipt{"test-start",15,data};Remember(receipt);
    auto wrong=data;wrong["terminal"]["reason"]="different";
    CHECK(!SaveDungeonReceipt(receipt,wrong));CHECK(!DungeonReceiptPending(receipt.id));
    CHECK_EQ(CharacterDatabase.transactions.size(),1u);
    auto settled=data;settled["settled"]=true;settled["slot"]=false;
    CHECK(!SaveDungeonReceipt(receipt,settled)); // No member's durable handback exists yet.
    Player player;
    CHECK(ObserveDungeonHandback(receipt,player));
    CHECK_EQ(player.saves,1u);CHECK(!DungeonReturnsPersisted(receipt));
    CHECK(!DungeonHandbackRecorded(receipt.data,receipt.id,101));
    auto committed=unverified.at(receipt.id).after;Commit();
    CHECK(PollDungeonReceipt(receipt)==DungeonPersistence::Pending);Row(receipt,committed,16);
    CHECK(PollDungeonReceipt(receipt)==DungeonPersistence::Ready);
    CHECK(DungeonHandbackRecorded(receipt.data,receipt.id,101));
    CHECK(receipt.data.at("terminal")==data.at("terminal"));
    CHECK(!DungeonReturnsPersisted(receipt));
    CHECK(DungeonRecoveryConflicts({101},false));
}

LEEROY_TEST(MemberDungeonIoLostCommitAcknowledgementUsesHandbackReadbackWithoutReplayingSave)
{
    Fresh();auto data=Manifest();DungeonReceipt receipt{"test-start",15,data};Remember(receipt);
    Player player;
    CHECK(ObserveDungeonHandback(receipt,player));
    CHECK_EQ(player.saves,1u);
    auto committed=unverified.at(receipt.id).after;
    auto const& statements=CharacterDatabase.transactions.back()->transaction->statements;
    CHECK_EQ(statements.front(),"SAVE_CHARACTER");
    CHECK_EQ(statements.size(),2u); // Character snapshot + receipt CAS; core owns COMMIT.
    Commit(false); // The updated core reports a lost COMMIT acknowledgement as unknown.
    CHECK(PollDungeonReceipt(receipt)==DungeonPersistence::Pending);
    CHECK(DungeonRecoveryConflicts({101},false));
    CHECK(ObserveDungeonHandback(receipt,player));
    CHECK_EQ(player.saves,1u);CHECK_EQ(CharacterDatabase.transactions.size(),2u);
    Row(receipt,committed,16);
    CHECK(PollDungeonReceipt(receipt)==DungeonPersistence::Ready);
    CHECK(DungeonHandbackRecorded(receipt.data,receipt.id,101));
    CHECK(ObserveDungeonHandback(receipt,player));
    CHECK_EQ(player.saves,1u);CHECK_EQ(CharacterDatabase.transactions.size(),2u);
}

LEEROY_TEST(MemberDungeonIoRestartDoesNotBorrowAPartialReusedGroupIdentity)
{
    Fresh();auto data=Manifest();data["stage"]="grouping";data["instance"]=0;
    data["effect"]="add_group";data["actor"]=103;
    data.erase("return_scope");data.erase("recovery_authorized");
    for(auto& body:data["bodies"])
    {
        body["changed"]=body["guid"].get<uint32>()<=102;
        body["moved"]=false;
    }
    DungeonReceipt receipt{"test-start",15,data};Remember(receipt);
    Group partial;partial.slots={{{101}},{{102}}};receiptGroups.active=&partial;
    // The same numeric group, leader and subset can belong to a replacement party.
    CHECK(!RecoverBodies(receipt));CHECK(!partial.disbanded);
    CHECK_EQ(CharacterDatabase.transactions.size(),1u);
    CHECK(DungeonRecoveryConflicts({101},false));
    CHECK(!DungeonRecoveryConflicts({999},false));
}

namespace ReceiptRunnerTest
{
using namespace Leeroy::Guild;
enum class Stage { Grouping, Teleporting, Starting, Running, Restarting, TearingDown };
enum class RaidJobMode { Offline, Benchmark, MemberDungeon };
struct Origin
{
    uint32 GetMapId()const{return 1;}
    float GetPositionX()const{return 10;} float GetPositionY()const{return 8;}
    float GetPositionZ()const{return 9;} float GetOrientation()const{return 0;}
};
struct Member
{
    uint32 guid;Origin origin;
    uint32 originalDifficulty=0,originalDungeonDifficulty=0;
    bool originalLeaderFlag=false,changed=false,moved=false,handbackProved=false,abandoned=false;
    bool heroicEntryPending=false;
};
struct Job
{
    DungeonReceipt receipt;Stage stage=Stage::Grouping;ObjectGuid group,driver;JobEvent event;
    std::vector<Member> members;unsigned stops=0;
    uint64 admissionSerial=7;bool admissionPending=false;
    uint32 guild=21,human=1;OfflineRaidCmd command;
    uint32 elapsed=0,stageElapsed=0,progressElapsed=0,mutations=0,teardownVisits=0;
    uint64 rewardToken=0,trainingToken=0;
    bool trainingSealed=false,announced=false,pinsInstalled=false;
    bool dcIssued=false,ownsGroup=true,present=true;
    std::string terminalReason,terminalState;
    std::vector<uint32> bosses;
    struct {uint32 timeoutMs=1800000;} limits;
    RaidJobMode mode=RaidJobMode::MemberDungeon;
};
std::unique_ptr<Job> active;
bool Dungeon(Job const&){return true;}
bool Benchmark(Job const&){return false;}
char const* Name(Stage){return "grouping";}
namespace Training
{
struct Rewards {void Returning(uint64){} bool Contaminated(uint64){return false;}} rewards;
auto& BenchmarkRewards(){return rewards;}
bool TrainingAllowed(){return true;} bool FrontierReached(std::vector<uint8>const&,std::vector<uint32>const&){return false;}
bool ClearTrainingRun(uint32,uint64){return true;}
}
struct Proofs {void Seal(std::string const&) {}} proofs;
auto& CleanupProofs(){return proofs;}
void Enter(Job& job,Stage stage){job.stage=stage;job.stageElapsed=0;job.announced=false;++job.stops;}
LeeroyCommandResult Fail(std::string const& error){return {false,error};}
using Leeroy::EncodeAck;
#include "MemberDungeonStop.inc"
#include "MemberDungeonAdmission.inc"
#define LEEROY_HAS_DUNGEON_CLEAR 1
#include "MemberDungeonReplay.inc"
#include "MemberDungeonCheckpoint.inc"
uint32 AdvanceElapsed(uint32 elapsed,uint32 diff){return elapsed+diff;}
ObjectGuid Guid(uint32 guid){return {guid};}
void ReassertMasters(Job&){}
bool humanOnline=false,guildEnabled=true,keepCast=true;
bool HumanConnected(uint32){return humanOnline;}bool GuildEnabled(){return guildEnabled;}bool KeepCast(){return keepCast;}
Player* Driver(Job&){static Player player;return &player;}
bool Present(Job const& job,Player* player){return job.present&&player;}
bool OwnsGroup(Job const& job,Group* group){return job.ownsGroup&&group;}
Group* FindGroup(Job const&){return receiptGroups.active;}
#include "MemberDungeonDisable.inc"
std::vector<uint32> Requested(OfflineRaidCmd const& command)
{
    auto members=command.members;members.insert(members.begin(),command.leaderGuid);return members;
}
constexpr auto kPinOwnerBenchmark="benchmark";
struct PinSet {std::unordered_set<uint32> Owned(char const*){return {};}} pinSet;
auto& Pins(){return pinSet;}
struct RandomBots {bool IsPinned(uint32){return true;}} sRandomPlayerbotMgr;
void Progress(Job& job){job.announced=true;}
void Grouping(Job& job){++job.mutations;}void Teleporting(Job& job){++job.mutations;}
void Starting(Job& job){++job.mutations;}void Running(Job& job,uint32){++job.mutations;}
void Restarting(Job& job){++job.mutations;}
bool RunTeardown(Job& job){++job.teardownVisits;return false;}
bool ReleaseStuckSlot(Job const&){return false;}
std::string lastFinishedOffline;
#define DC_PB_CONFIG(...) true
#include "MemberDungeonTick.inc"
#undef DC_PB_CONFIG
#undef LEEROY_HAS_DUNGEON_CLEAR
}

LEEROY_TEST(MemberDungeonIoRunnerCheckpointWaitsWithoutAbortingOrAdvancing)
{
    Fresh();ReceiptRunnerTest::Job job;job.receipt=Accepted();
    for(uint32 guid=101;guid<=105;++guid)job.members.push_back({guid,{}});
    CHECK(!ReceiptRunnerTest::Checkpoint(job,"create_group",101));
    CHECK_EQ(job.stops,0u);
    for(int tick=0;tick<100;++tick)CHECK(!ReceiptRunnerTest::Checkpoint(job,"create_group",101));
    CHECK_EQ(job.stops,0u);CHECK_EQ(CharacterDatabase.transactions.size(),2u);
    auto committed=unverified.at(job.receipt.id).after;Commit();
    CHECK(!ReceiptRunnerTest::Checkpoint(job,"create_group",101));Row(job.receipt,committed,2);
    CHECK(ReceiptRunnerTest::Checkpoint(job,"create_group",101));
    CHECK_EQ(job.receipt.revision,2u);CHECK_EQ(job.stops,0u);
    job.members[0].changed=true;job.group={90};
    CHECK(!ReceiptRunnerTest::Checkpoint(job,"group_created"));
    CHECK_EQ(job.stops,0u);CHECK_EQ(CharacterDatabase.transactions.size(),3u);
}

namespace ReceiptDispatchTest
{
uint64 commandSerial=0;
auto& Handlers()
{
    static std::unordered_map<std::string,std::function<LeeroyCommandResult(Json const&)>> handlers;
    return handlers;
}
#include "MemberDungeonDispatch.inc"

Leeroy::Command StartCommand()
{
    Fresh();ReceiptRunnerTest::active.reset();
    ReceiptRunnerTest::humanOnline=false;ReceiptRunnerTest::guildEnabled=true;ReceiptRunnerTest::keepCast=true;
    Handlers()["start_member_dungeon"]=[](Json const& message)->LeeroyCommandResult
    {
        using namespace ReceiptRunnerTest;
        std::string error;
        auto command=ParseMemberDungeon(message,error);
        if(!command)throw std::runtime_error(error);
        auto id=message.at("id").get<std::string>();
        if(auto replay=MemberDungeonStartReplay(id,*command))return *replay;
        active=std::make_unique<Job>();active->command=*command;active->event.id=id;
        active->admissionPending=true;active->admissionSerial=commandSerial;
        if(!BeginDungeonReceipt(active->receipt,id,21,1,101,{101,102,103,104,105}))
            throw std::runtime_error("failed to stage initial receipt");
        return {true,{},true};
    };
    Leeroy::Command command;
    command.id="test-start";command.extType="start_member_dungeon";command.connectionSerial=7;
    command.raw={{"id",command.id},{"type",command.extType},{"map_id",602},{"leader_guid",101},
        {"members",{102,103,104,105}}};
    command.raw=Json::parse(command.raw.dump()); // Match unsigned fields decoded from the bridge wire.
    return command;
}
}

LEEROY_TEST(MemberDungeonIoDeferredAdmissionWaitsForDurableProofAndDeduplicatesRetries)
{
    using namespace ReceiptDispatchTest;
    auto command=StartCommand();
    auto const firstReply=ExecuteExtension(command,receiptClock);
    if(!firstReply.empty())throw std::runtime_error(firstReply);
    CHECK(ExecuteExtension(command,receiptClock).empty());
    CHECK_EQ(CharacterDatabase.transactions.size(),1u);
    auto& job=*ReceiptRunnerTest::active;
    CHECK(!ReceiptRunnerTest::AwaitDungeonPersistence(job));
    CHECK(LeeroyModule::Instance().replies.lines.empty());
    auto status=PublishDungeonReceipt(command.id);
    CHECK(!status.ok);CHECK_EQ(status.error,"dungeon_receipt_unproven");
    CHECK(CharacterDatabase.queries.empty()); // No premature absent-row status probe.
    Commit();CHECK(!ReceiptRunnerTest::AwaitDungeonPersistence(job));
    CHECK(ExecuteExtension(command,receiptClock).empty());
    Row(job.receipt,job.receipt.data,1);
    CHECK(ReceiptRunnerTest::AwaitDungeonPersistence(job));
    CHECK_EQ(job.receipt.revision,1u);CHECK_EQ(job.stops,0u);
    auto& lines=LeeroyModule::Instance().replies.lines;CHECK_EQ(lines.size(),1u);
    CHECK(Json::parse(lines.front()).at("ok")==true);
    auto replay=Json::parse(ExecuteExtension(command,receiptClock));
    CHECK(replay.at("ok")==true);CHECK_EQ(CharacterDatabase.transactions.size(),1u);
    CHECK_EQ(commandSerial,0u);
    ReceiptRunnerTest::active.reset();
}

LEEROY_TEST(MemberDungeonIoFailedInitialInsertAnswersNegativeWithoutAnUnversionedTerminal)
{
    using namespace ReceiptDispatchTest;
    auto command=StartCommand();CHECK(ExecuteExtension(command,receiptClock).empty());
    Commit(false);CHECK(!ReceiptRunnerTest::AwaitDungeonPersistence(*ReceiptRunnerTest::active));
    Answer({});CHECK(!ReceiptRunnerTest::AwaitDungeonPersistence(*ReceiptRunnerTest::active));
    CHECK(LeeroyModule::Instance().replies.lines.empty());
    CHECK_EQ(PublishDungeonReceipt(command.id).error,"dungeon_receipt_unproven");
    receiptClock+=1001;CHECK(!ReceiptRunnerTest::AwaitDungeonPersistence(*ReceiptRunnerTest::active));
    Answer(Result({"0"}));CHECK(!ReceiptRunnerTest::AwaitDungeonPersistence(*ReceiptRunnerTest::active));
    CHECK(!ReceiptRunnerTest::active);
    auto const& lines=LeeroyModule::Instance().replies.lines;CHECK_EQ(lines.size(),1u);
    auto ack=Json::parse(lines.front());CHECK(ack.at("ok")==false);CHECK_EQ(ack.at("type"),"ack");
    CHECK_EQ(ack.at("error"),"dungeon_receipt_unavailable");
    CHECK_EQ(PublishDungeonReceipt(command.id).error,"unknown_dungeon");
    CHECK(!DungeonRecoveryConflicts({101},false));
}

LEEROY_TEST(MemberDungeonIoDisconnectedAdmissionRetainsDurableReceiptWithoutStarting)
{
    using namespace ReceiptDispatchTest;
    auto command=StartCommand();CHECK(ExecuteExtension(command,receiptClock).empty());
    auto& replies=LeeroyModule::Instance().replies;replies.serial=8; // A replacement connection.
    auto replayed=command;replayed.connectionSerial=8;
    CHECK(ExecuteExtension(replayed,receiptClock).empty());
    Commit();auto& job=*ReceiptRunnerTest::active;
    CHECK(!ReceiptRunnerTest::AwaitDungeonPersistence(job));Row(job.receipt,job.receipt.data,1);
    CHECK(ReceiptRunnerTest::AwaitDungeonPersistence(job));
    CHECK_EQ(job.stops,1u);CHECK(job.stage==ReceiptRunnerTest::Stage::TearingDown);CHECK(replies.lines.empty());
    CHECK_EQ(job.receipt.revision,1u);CHECK(DungeonBeforeMutation(job.receipt.data));
    CHECK(DungeonRecoveryConflicts({101},false)); // Normal teardown publishes the persisted terminal later.
    ReceiptRunnerTest::active.reset();
}

LEEROY_TEST(MemberDungeonIoCancellationBeforeReadbackNeverReentersGrouping)
{
    using namespace ReceiptDispatchTest;
    auto command=StartCommand();CHECK(ExecuteExtension(command,receiptClock).empty());
    auto& job=*ReceiptRunnerTest::active;
    ReceiptRunnerTest::Stop(job,"stopped");
    ReceiptRunnerTest::TickOfflineRaid(50000);
    CHECK_EQ(job.mutations,0u);CHECK_EQ(job.teardownVisits,0u);
    CHECK(LeeroyModule::Instance().replies.lines.empty());
    Commit();ReceiptRunnerTest::TickOfflineRaid(1);Row(job.receipt,job.receipt.data,1);
    ReceiptRunnerTest::TickOfflineRaid(1);
    CHECK_EQ(job.mutations,0u);CHECK_EQ(job.teardownVisits,1u);
    CHECK(job.stage==ReceiptRunnerTest::Stage::TearingDown);CHECK_EQ(job.terminalReason,"stopped");
    CHECK_EQ(job.receipt.revision,1u);
    ReceiptRunnerTest::active.reset();
}

LEEROY_TEST(MemberDungeonIoTimeoutBeforeReadbackNeverStartsWorldMutation)
{
    using namespace ReceiptDispatchTest;
    auto command=StartCommand();CHECK(ExecuteExtension(command,receiptClock).empty());
    auto& job=*ReceiptRunnerTest::active;job.limits.timeoutMs=1000;
    ReceiptRunnerTest::TickOfflineRaid(50000);
    CHECK_EQ(job.mutations,0u);CHECK_EQ(job.teardownVisits,0u);
    Commit();ReceiptRunnerTest::TickOfflineRaid(1);Row(job.receipt,job.receipt.data,1);
    ReceiptRunnerTest::TickOfflineRaid(1);
    CHECK_EQ(job.mutations,0u);CHECK_EQ(job.teardownVisits,1u);
    CHECK(job.stage==ReceiptRunnerTest::Stage::TearingDown);CHECK_EQ(job.terminalReason,"timeout");
    ReceiptRunnerTest::active.reset();
}

LEEROY_TEST(MemberDungeonIoStoppedRunningJobDisablesOwnedRunWhileReadbackKeepsFailing)
{
    Fresh();auto data=Manifest();data["stage"]="running";
    using namespace ReceiptRunnerTest;
    active=std::make_unique<Job>();auto& job=*active;
    job.receipt={"test-start",15,data};Remember(job.receipt);
    job.stage=Stage::Running;job.command.leaderGuid=101;job.event.id="test-start";
    job.dcIssued=true;humanOnline=false;guildEnabled=keepCast=true;
    Group group;receiptGroups.active=&group;Player leader;receiptConnected[101]=&leader;
    auto next=data;next["kills"]=1;
    CHECK(!SaveDungeonReceipt(job.receipt,next));
    Stop(job,"stopped");TickOfflineRaid(1);
    CHECK(!job.dcIssued);CHECK(!leader.ai.run.enabled);CHECK_EQ(leader.ai.disabled,1u);
    CHECK_EQ(job.mutations,0u);CHECK_EQ(job.teardownVisits,0u);
    Commit();TickOfflineRaid(1);Answer({});TickOfflineRaid(1);
    CHECK_EQ(job.teardownVisits,0u);CHECK_EQ(leader.ai.disabled,1u);
    receiptClock+=1001;TickOfflineRaid(1);Row(job.receipt,next,16);TickOfflineRaid(1);
    CHECK_EQ(job.terminalReason,"stopped");CHECK(job.stage==Stage::TearingDown);
    CHECK_EQ(job.mutations,0u);CHECK_EQ(job.teardownVisits,1u);
    CHECK_EQ(job.receipt.data.at("stage"),"running"); // Old persisted progress cannot overwrite the newer stop intent.
    active.reset();
}

LEEROY_TEST(MemberDungeonIoAutomaticStopsRemainLiveDuringRunningCheckpointWait)
{
    using namespace ReceiptRunnerTest;
    for(auto const* reason:{"timeout","disabled","guild_off","keep_cast_off"})
    {
        Fresh();auto data=Manifest();data["stage"]="running";
        active=std::make_unique<Job>();auto& job=*active;
        job.receipt={"test-start",15,data};Remember(job.receipt);
        job.stage=Stage::Running;job.command.leaderGuid=101;job.event.id="test-start";job.dcIssued=true;
        humanOnline=false;
        guildEnabled=std::string(reason)!="guild_off";keepCast=std::string(reason)!="keep_cast_off";
        receiptEnabled=std::string(reason)!="disabled";
        if(std::string(reason)=="timeout")job.limits.timeoutMs=1;
        Group group;receiptGroups.active=&group;Player leader;receiptConnected[101]=&leader;
        auto next=data;next["kills"]=1;CHECK(!SaveDungeonReceipt(job.receipt,next));
        TickOfflineRaid(2);
        CHECK(job.stage==Stage::TearingDown);CHECK(!job.dcIssued);CHECK_EQ(leader.ai.disabled,1u);
        CHECK_EQ(job.mutations,0u);CHECK_EQ(job.teardownVisits,0u);
        CHECK(DungeonReceiptPending(job.receipt.id));
        active.reset();
    }
    humanOnline=false;guildEnabled=keepCast=true;
}

LEEROY_TEST(MemberDungeonIoPendingStopDoesNotDisableAnUnownedRun)
{
    Fresh();auto data=Manifest();data["stage"]="running";
    using namespace ReceiptRunnerTest;
    active=std::make_unique<Job>();auto& job=*active;
    job.receipt={"test-start",15,data};Remember(job.receipt);job.stage=Stage::Running;
    job.command.leaderGuid=101;job.event.id="test-start";job.dcIssued=true;job.ownsGroup=false;
    humanOnline=false;guildEnabled=keepCast=true;
    Group group;receiptGroups.active=&group;Player leader;receiptConnected[101]=&leader;
    auto next=data;next["kills"]=1;CHECK(!SaveDungeonReceipt(job.receipt,next));
    Stop(job,"stopped");TickOfflineRaid(1);
    CHECK(job.dcIssued);CHECK(leader.ai.run.enabled);CHECK_EQ(leader.ai.disabled,0u);
    CHECK_EQ(job.mutations,0u);CHECK_EQ(job.teardownVisits,0u);
    active.reset();
}

LEEROY_TEST(MemberDungeonIoOwnerLoginAloneKeepsAcceptedHeroicButRequestedPreemptionStopsIt)
{
    Fresh();auto data=Manifest();data["stage"]="running";
    using namespace ReceiptRunnerTest;
    active=std::make_unique<Job>();auto& job=*active;
    job.receipt={"test-start",15,data};Remember(job.receipt);job.stage=Stage::Running;
    job.command.leaderGuid=101;job.event.id="test-start";job.dcIssued=true;
    humanOnline=true;guildEnabled=keepCast=true;
    Group group;receiptGroups.active=&group;Player leader;receiptConnected[101]=&leader;
    auto next=data;next["kills"]=1;CHECK(!SaveDungeonReceipt(job.receipt,next));
    TickOfflineRaid(1);
    CHECK(job.stage==Stage::Running);CHECK(job.dcIssued);CHECK_EQ(leader.ai.disabled,0u);
    CHECK_EQ(job.mutations,0u);CHECK_EQ(job.teardownVisits,0u);
    Stop(job,"preempted");TickOfflineRaid(1);
    CHECK(job.stage==Stage::TearingDown);CHECK(!job.dcIssued);CHECK_EQ(leader.ai.disabled,1u);
    CHECK_EQ(job.terminalReason,"preempted");CHECK(DungeonReceiptPending(job.receipt.id));
    CHECK_EQ(job.teardownVisits,0u);active.reset();humanOnline=false;
}

LEEROY_TEST(MemberDungeonIoRecoveryPublishesSettledTerminalAfterDelayedVerification)
{
    Fresh();auto data=Accepted().data;data["recovery_ms"]=receiptClock;
    DungeonReceipt receipt{"test-start",15,data};Remember(receipt);
    TickDungeonRecovery(5000);
    CHECK(LeeroyModule::Instance().events.empty());
    auto next=unverified.at(receipt.id).after;
    CHECK(next.at("settled")==true);CHECK(next.at("terminal").is_object());
    Commit();TickDungeonRecovery(5000);Row(receipt,next,16);TickDungeonRecovery(5000);
    CHECK(!outstanding.contains(receipt.id));
    auto const& events=LeeroyModule::Instance().events;
    CHECK_EQ(events.size(),1u);CHECK(events.at(0).at("returned")==true);
    CHECK_EQ(events.at(0).at("revision"),16);CHECK_EQ(events.at(0).at("id"),receipt.id);
    TickDungeonRecovery(5000);CHECK_EQ(events.size(),1u);
}

LEEROY_TEST(MemberDungeonIoRecoveryPublishesUnsettledFailureAfterDelayedVerification)
{
    Fresh();auto data=Manifest();data["recovery_ms"]=receiptClock-OfflineCleanupFailMs-1;
    DungeonReceipt receipt{"test-start",15,data};Remember(receipt);
    TickDungeonRecovery(5000);CHECK(LeeroyModule::Instance().events.empty());
    auto next=unverified.at(receipt.id).after;
    CHECK(next.at("settled")==false);CHECK(next.at("terminal").is_object());
    Commit();TickDungeonRecovery(5000);Row(receipt,next,16);TickDungeonRecovery(5000);
    CHECK(outstanding.contains(receipt.id));
    auto const& events=LeeroyModule::Instance().events;
    CHECK_EQ(events.size(),1u);CHECK(events.at(0).at("returned")==false);
    CHECK_EQ(events.at(0).at("reason"),"cleanup_failed:world_restart");
    CHECK_EQ(events.at(0).at("revision"),16);
}
