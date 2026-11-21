// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyGuildCommands.h"
#include "LeeroyLootLedger.h"
#include "LeeroyProtocol.h"
#include "TestHarness.h"
#include <algorithm>
#include <deque>
#include <map>
#include <memory>

static struct { struct { unsigned Realm = 1; } Id; } lootReceiptTestRealm;
namespace LootReceiptBoundary
{
using uint32 = std::uint32_t;
using uint64 = std::uint64_t;
using Json = nlohmann::json;
struct LeeroyCommandResult { bool ok = false; std::string error; bool deferred = false; };
struct Field
{
    std::string value;
    template<class T> T Get() const
    {
        if constexpr (std::is_same_v<T, std::string>) return value;
        else return static_cast<T>(std::stoull(value));
    }
};
struct Rows { std::vector<Field> fields; Field* Fetch() { return fields.data(); } };
using QueryResult = std::shared_ptr<Rows>;
struct ReadJob { std::string sql; bool ready = false; QueryResult result; };
struct Transaction
{
    std::vector<std::string> statements;
    void Append(std::string const& sql) { statements.push_back(sql); }
};
using CharacterDatabaseTransaction = std::shared_ptr<Transaction>;
struct WriteJob { CharacterDatabaseTransaction tx; bool ready = false, success = false; };
struct QueryCallback
{
    std::shared_ptr<ReadJob> job;
    std::function<void(QueryResult)> fn;
    void WithCallback(std::function<void(QueryResult)> cb) { fn = std::move(cb); }
    bool InvokeIfReady()
    {
        if (!job->ready || !fn) return false;
        auto callback = std::move(fn); callback(job->result); return true;
    }
};
struct TransactionCallback
{
    std::shared_ptr<WriteJob> job;
    std::function<void(bool)> fn;
    void AfterComplete(std::function<void(bool)> cb) { fn = std::move(cb); }
    bool InvokeIfReady()
    {
        if (!job->ready || !fn) return false;
        auto callback = std::move(fn); callback(job->success); return true;
    }
};
// No synchronous API: the real adapter cannot accidentally block the world thread.
struct Database
{
    std::deque<std::shared_ptr<ReadJob>> reads;
    std::deque<std::shared_ptr<WriteJob>> writes;
    QueryCallback AsyncQuery(std::string const& sql)
    {
        auto job = std::make_shared<ReadJob>(); job->sql = sql; reads.push_back(job); return {job, {}};
    }
    CharacterDatabaseTransaction BeginTransaction() { return std::make_shared<Transaction>(); }
    TransactionCallback AsyncCommitTransaction(CharacterDatabaseTransaction tx)
    {
        auto job = std::make_shared<WriteJob>(); job->tx = std::move(tx); writes.push_back(job); return {job, {}};
    }
} CharacterDatabase;
struct TestBridge
{
    uint64 serial = 1;
    bool connected = true;
    std::vector<Json> replies;
    uint64 ConnectionSerial() const { return serial; }
    bool Connected() const { return connected; }
    void Reply(uint64 expected, std::string const& line)
    {
        if (expected == serial && connected) replies.push_back(Json::parse(line));
    }
};
struct LeeroyModule
{
    bool enabled = true;
    uint32 owner = 1, guild = 21;
    struct Settings { bool lootReceipts = true; } cfg;
    TestBridge bridge;
    static LeeroyModule& Instance() { static LeeroyModule module; return module; }
    bool Enabled() const { return enabled; }
    auto const& Config() const { return cfg; }
    uint32 HumanGuid() const { return owner; }
    uint32 GuildId() const { return guild; }
    auto& Bridge() { return bridge; }
};
uint64 LeeroyCommandSerial() { return LeeroyModule::Instance().bridge.serial; }
std::int64_t NowUnixMs() { return 1000000; }
namespace Leeroy::Guild
{
using ::Leeroy::Guild::ParseGiveLoot;
using ::Leeroy::Guild::ParseLootReceipt;
using ::Leeroy::Guild::LootSourceObservation;
using ::Leeroy::EncodeAck;
struct LootSourceLedger
{
    std::optional<LootSourceObservation> source = LootSourceObservation{80, 90, 100};
    static auto& Instance() { static LootSourceLedger ledger; return ledger; }
    auto Observe(uint32, uint32, uint32) { return source; }
};
unsigned transfers = 0;
bool eligible = true;
LeeroyCommandResult TransferLoot(Json const&, CharacterDatabaseTransaction tx,
                                std::optional<LootSourceObservation> expected)
{
    if (!eligible || !expected || expected->generation != LootSourceLedger::Instance().source->generation)
        return {false, "not_eligible"};
    ++transfers;
    tx->Append("SAVE_INVENTORY");
    return {true, {}};
}
}
#define realm lootReceiptTestRealm
#include "LootReceipts.inc"
#undef realm
using namespace Leeroy::Guild;

Json Command(std::string id = "give-1")
{
    return Json::parse(Json{{"id", id}, {"map", 631}, {"instance_id", 77}, {"source_entry", 36597},
        {"index", 0}, {"item_id", 50001}, {"to_guid", 105}, {"receipt", true}}.dump());
}
void Fresh()
{
    pending.clear(); schemaReady = false; CharacterDatabase = {};
    LeeroyModule::Instance() = {}; LootSourceLedger::Instance() = {}; transfers = 0; eligible = true;
}
void Commit(bool success = true)
{
    CHECK(!CharacterDatabase.writes.empty());
    if (CharacterDatabase.writes.empty()) return;
    auto job = CharacterDatabase.writes.front(); CharacterDatabase.writes.pop_front();
    CHECK(std::find(job->tx->statements.begin(), job->tx->statements.end(), "COMMIT")
        == job->tx->statements.end());
    job->ready = true; job->success = success; TickLootReceipts();
}
void Row(unsigned count, unsigned state = 0, std::string payload = "")
{
    CHECK(!CharacterDatabase.reads.empty());
    if (CharacterDatabase.reads.empty()) return;
    if (payload.empty() && count && !pending.empty()) payload = pending.begin()->second.payload;
    auto job = CharacterDatabase.reads.front(); CharacterDatabase.reads.pop_front();
    CHECK(job->sql.find("SELECT COUNT(*)") == 0);
    job->result = std::make_shared<Rows>(Rows{{{std::to_string(count)}, {payload}, {std::to_string(state)}}});
    job->ready = true; TickLootReceipts();
}
void Reserved()
{
    CHECK(BeginLootReceipt(Command()).deferred);
    Commit(); Row(1); Row(0); Commit(); Row(1, 0);
}
Json Reply() { return LeeroyModule::Instance().bridge.replies.back(); }

LEEROY_TEST(LootReceiptWaitsForReservationCommitAndInventoryReadback)
{
    Fresh(); CHECK(BeginLootReceipt(Command()).deferred);
    for (int i = 0; i < 100; ++i) TickLootReceipts();
    CHECK_EQ(transfers, 0u); CHECK(LeeroyModule::Instance().bridge.replies.empty());
    Commit(); Row(1); Row(0);
    for (int i = 0; i < 100; ++i) TickLootReceipts();
    CHECK_EQ(transfers, 0u);
    Commit(); CHECK_EQ(transfers, 0u); Row(1, 0); CHECK_EQ(transfers, 1u);
    auto sql = CharacterDatabase.writes.front()->tx->statements;
    CHECK_EQ(sql.front(), "SAVE_INVENTORY"); CHECK(sql[1].find("SET state=1") != std::string::npos);
    CHECK(LeeroyModule::Instance().bridge.replies.empty());
    Commit(); CHECK(LeeroyModule::Instance().bridge.replies.empty());
    Row(1, 1); CHECK(Reply()["ok"] == true); CHECK(pending.empty());
}

LEEROY_TEST(LootReceiptRestartRecoversLostAckWithoutAnotherGive)
{
    Fresh(); Reserved(); auto payload = pending.begin()->second.payload;
    Commit(); Row(1, 1);
    pending.clear(); schemaReady = false; LeeroyModule::Instance().bridge.replies.clear();
    auto status = Command("proof-1"); status["give_id"] = "give-1";
    CHECK(GetLootReceipt(status).deferred); CHECK(CharacterDatabase.writes.empty());
    Row(1); Row(1, 1, payload); CHECK(Reply()["ok"] == true); CHECK_EQ(transfers, 1u);
    CHECK_EQ(Reply()["id"].get<std::string>(), "proof-1");
}

LEEROY_TEST(LootReceiptReservedCrashNeverReplaysAndAbsentIsNotSuccess)
{
    Fresh(); schemaReady = true; CHECK(BeginLootReceipt(Command()).deferred);
    Row(1, 0); CHECK_EQ(transfers, 0u); CHECK(Reply()["ok"] == false);
    auto status = Command("proof-1"); status["give_id"] = "give-1";
    CHECK(GetLootReceipt(status).deferred); Row(0);
    CHECK_EQ(Reply()["error"].get<std::string>(), "loot_receipt_absent");
}

LEEROY_TEST(LootReceiptFailedReservationCannotOwnAnotherWritersReservation)
{
    Fresh(); schemaReady = true; CHECK(BeginLootReceipt(Command()).deferred);
    Row(0); Commit(false); Row(1, 0); CHECK_EQ(transfers, 0u); CHECK(Reply()["ok"] == false);
}

LEEROY_TEST(LootReceiptFailedInventoryCommitCannotChargeOrReplay)
{
    Fresh(); Reserved(); Commit(false); Row(1, 0);
    CHECK_EQ(transfers, 1u); CHECK(Reply()["ok"] == false);
    CHECK(BeginLootReceipt(Command()).deferred); Row(1, 0); CHECK_EQ(transfers, 1u);
}

LEEROY_TEST(LootReceiptExactPayloadPreventsWinnerOrSlotMismatch)
{
    Fresh(); schemaReady = true; CHECK(BeginLootReceipt(Command()).deferred);
    auto stored = Json::parse(pending.begin()->second.payload); stored["to_guid"] = 104;
    Row(1, 1, stored.dump()); CHECK(Reply()["ok"] == false); CHECK_EQ(transfers, 0u);
    CHECK_EQ(Reply()["error"].get<std::string>(), "loot_receipt_conflict");
}

LEEROY_TEST(LootReceiptConcurrentDuplicateAndStatusDoNotGiveTwice)
{
    Fresh(); CHECK(BeginLootReceipt(Command()).deferred);
    CHECK(!BeginLootReceipt(Command()).ok);
    auto status = Command("proof-1"); status["give_id"] = "give-1";
    CHECK_EQ(GetLootReceipt(status).error, "loot_receipt_pending");
    status["to_guid"] = 104u; CHECK_EQ(GetLootReceipt(status).error, "loot_receipt_conflict");
    CHECK_EQ(pending.size(), 1u); CHECK_EQ(transfers, 0u);
}

LEEROY_TEST(LootReceiptDisconnectFencesDeferredTransferAndOldReply)
{
    Fresh(); CHECK(BeginLootReceipt(Command()).deferred); Commit(); Row(1); Row(0);
    LeeroyModule::Instance().bridge.serial = 2;
    Commit(); Row(1, 0); CHECK_EQ(transfers, 0u);
    CHECK(CharacterDatabase.writes.front()->tx->statements.front().find("SET state=2") != std::string::npos);
    Commit(); Row(1, 2); CHECK(LeeroyModule::Instance().bridge.replies.empty());
}

LEEROY_TEST(LootReceiptOwnerAndSourceAreCheckedAfterDatabaseWait)
{
    Fresh(); CHECK(BeginLootReceipt(Command()).deferred); Commit(); Row(1); Row(0);
    LeeroyModule::Instance().owner = 2;
    Commit(); Row(1, 0); CHECK_EQ(transfers, 0u); Commit(); Row(1, 2);
    CHECK_EQ(Reply()["error"].get<std::string>(), "loot_receipt_rejected");
    Fresh(); CHECK(BeginLootReceipt(Command()).deferred); Commit(); Row(1); Row(0);
    LootSourceLedger::Instance().source->generation = 101;
    Commit(); Row(1, 0); CHECK_EQ(transfers, 0u);
}

LEEROY_TEST(LootReceiptDisabledStillAllowsReadOnlyProof)
{
    Fresh(); LeeroyModule::Instance().cfg.lootReceipts = false;
    CHECK_EQ(BeginLootReceipt(Command()).error, "loot_receipt_disabled");
    CHECK(CharacterDatabase.writes.empty());
    auto status = Command("proof-1"); status["give_id"] = "give-1";
    CHECK(GetLootReceipt(status).deferred); Row(1); Row(1, 1);
    CHECK(Reply()["ok"] == true); CHECK_EQ(transfers, 0u); CHECK(CharacterDatabase.writes.empty());
}

LEEROY_TEST(LootReceiptDatabaseErrorIsNotAnEmptyRow)
{
    Fresh(); schemaReady = true; CHECK(BeginLootReceipt(Command()).deferred);
    CharacterDatabase.reads.front()->ready = true; TickLootReceipts();
    CHECK_EQ(Reply()["error"].get<std::string>(), "loot_receipt_unavailable");
    CHECK_EQ(transfers, 0u); CHECK(CharacterDatabase.writes.empty());
}

LEEROY_TEST(LootReceiptSerializesInventoryCommitsForOneRecipient)
{
    Fresh(); schemaReady = true;
    CHECK(BeginLootReceipt(Command()).deferred);
    auto second = Command("give-2"); second["index"] = 1u;
    CHECK(BeginLootReceipt(second).deferred);
    auto payload2 = pending.rbegin()->second.payload;
    Row(0); Row(0); Commit(); Commit(); Row(1, 0); Row(1, 0, payload2);
    CHECK_EQ(transfers, 1u);
    for (int i = 0; i < 100; ++i) TickLootReceipts();
    CHECK_EQ(transfers, 1u); CHECK_EQ(CharacterDatabase.writes.size(), 1u);
    Commit(); Row(1, 1);
    CHECK_EQ(transfers, 2u); CHECK_EQ(CharacterDatabase.writes.size(), 1u);
    Commit(); Row(1, 1); CHECK_EQ(LeeroyModule::Instance().bridge.replies.size(), 2u);
}

LEEROY_TEST(LootReceiptNeverQueriesAnAbsentTableOrCreatesItForStatus)
{
    Fresh(); LeeroyModule::Instance().cfg.lootReceipts = false;
    auto status = Command("proof-1"); status["give_id"] = "give-1";
    CHECK(GetLootReceipt(status).deferred);
    CHECK(CharacterDatabase.reads.front()->sql.find("information_schema.tables") != std::string::npos);
    Row(0); CHECK_EQ(Reply()["error"].get<std::string>(), "loot_receipt_absent");
    CHECK(CharacterDatabase.writes.empty()); CHECK(CharacterDatabase.reads.empty());
    CHECK_EQ(transfers, 0u);
}

LEEROY_TEST(LootReceiptFailedSchemaCreationCannotQueryTheAbsentTable)
{
    Fresh(); CHECK(BeginLootReceipt(Command()).deferred); Commit(false);
    CHECK(CharacterDatabase.reads.front()->sql.find("information_schema.tables") != std::string::npos);
    Row(0); CHECK_EQ(Reply()["error"].get<std::string>(), "loot_receipt_absent");
    CHECK(CharacterDatabase.reads.empty()); CHECK(CharacterDatabase.writes.empty());
    CHECK_EQ(transfers, 0u);
}
}
