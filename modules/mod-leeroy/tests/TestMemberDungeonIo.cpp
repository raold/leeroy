// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyMemberDungeon.h"
#include "LeeroyGuildCommands.h"
#include "LeeroyOfflineRaidPolicy.h"
#include "TestHarness.h"
#include <algorithm>
#include <cmath>
#include <deque>
#include <iomanip>
#include <map>
#include <memory>
#include <set>
#include <stdexcept>

using uint8 = std::uint8_t;
using uint32 = std::uint32_t;
using uint64 = std::uint64_t;
using int64 = std::int64_t;
using Json = nlohmann::json;
#define LOG_ERROR(...) ((void)0)
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
struct LeeroyCommandResult { bool ok; std::string error; };
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
struct PlayerbotAI { Player* master=nullptr; Player* GetMaster(){return master;} };
struct ReceiptSession {bool IsLoggingOut(){return false;}};
class Player
{
public:
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
    static auto& Instance(){static LeeroyModule value;return value;}
    uint32 GuildId(){return 21;} void Publish(Json const&,bool){}
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
    // An independent status request saw no row before the transaction committed.
    CHECK(!PublishDungeonReceipt(receipt.id).ok);Answer(Result({"0"}));
    CHECK(!PublishDungeonReceipt(receipt.id).ok);
    Commit();CHECK(PollDungeonReceipt(receipt)==DungeonPersistence::Pending);
    CHECK_EQ(CharacterDatabase.queries.size(),2u);
    Row(receipt,receipt.data,1);CHECK(PollDungeonReceipt(receipt)==DungeonPersistence::Ready);
    CHECK_EQ(receipt.revision,1u);CHECK(!DungeonReceiptPending(receipt.id));
    CHECK_EQ(CharacterDatabase.transactions.size(),1u);
    CHECK_EQ(CharacterDatabase.transactions.front()->transaction->statements.back(),"COMMIT");
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
    CHECK_EQ(statements.size(),3u);
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
};
struct Job
{
    DungeonReceipt receipt;int stage=0;ObjectGuid group,driver;JobEvent event;
    std::vector<Member> members;unsigned stops=0;
};
bool Dungeon(Job const&){return true;}
char const* Name(int){return "grouping";}
void Stop(Job& job,std::string const&,bool){++job.stops;}
#include "MemberDungeonCheckpoint.inc"
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
