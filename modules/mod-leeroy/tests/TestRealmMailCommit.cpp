// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmMailPersistence.h"
#include "TestHarness.h"
#include <map>
#include <memory>
#include <optional>
#include <nlohmann/json.hpp>

namespace RealmMailCommitBoundary
{
using namespace Leeroy::Realm;
using Json = nlohmann::json;
using QueryResult = std::shared_ptr<std::vector<MailPersistedRow>>;
struct Transaction
{
    std::vector<std::string> statements;
    void Append(std::string const& sql) { statements.push_back(sql); }
};
using CharacterDatabaseTransaction = std::shared_ptr<Transaction>;
struct WriteJob { CharacterDatabaseTransaction transaction; bool ready = false, success = false; };
struct ReadJob { std::string sql; bool ready = false; QueryResult result; };
struct TransactionCallback
{
    std::shared_ptr<WriteJob> job;
    std::function<void(bool)> callback;
    void AfterComplete(std::function<void(bool)> fn) { callback = std::move(fn); }
    bool InvokeIfReady()
    {
        if (!job->ready || !callback) return false;
        auto fn = std::move(callback); fn(job->success); return true;
    }
};
struct QueryCallback
{
    std::shared_ptr<ReadJob> job;
    std::function<void(QueryResult)> callback;
    void WithCallback(std::function<void(QueryResult)> fn) { callback = std::move(fn); }
    bool InvokeIfReady()
    {
        if (!job->ready || !callback) return false;
        auto fn = std::move(callback); fn(job->result); return true;
    }
};
// No synchronous SQL API: the production lane must poll only completed futures.
struct Database
{
    std::vector<std::shared_ptr<WriteJob>> writes;
    std::vector<std::shared_ptr<ReadJob>> reads;
    TransactionCallback AsyncCommitTransaction(CharacterDatabaseTransaction transaction)
    {
        auto job = std::make_shared<WriteJob>(); job->transaction = std::move(transaction);
        writes.push_back(job); return {job,{}};
    }
    QueryCallback AsyncQuery(std::string const& sql)
    {
        auto job = std::make_shared<ReadJob>(); job->sql = sql; reads.push_back(job); return {job,{}};
    }
} CharacterDatabase;
enum class StepState { Pending, Unknown, Complete };
struct RealmStep { StepState state; std::string reason; std::uint32_t value = 0; };
struct Queued { std::string id; Json payload; };
struct RealmModule
{
    bool on = true;
    std::uint64_t epoch = 22;
    std::string refusal;
    static auto& Instance() { static RealmModule module; return module; }
    auto Epoch() const { return epoch; }
    bool On() const { return on; }
    std::string ValidateOwnedBody(std::uint32_t, std::string const&) const { return refusal; }
};
std::uint32_t HumanGuid() { return 1; }
struct Attempt
{
    Json admitted;
    std::uint32_t guid = 101, human = 1, first = 901;
    std::uint64_t epoch = 22;
    std::optional<bool> committed;
    std::unique_ptr<TransactionCallback> commit;
    MailStagedHeader header;
    std::uint32_t expiry = 26000;
    std::vector<MailSavedItem> attachments;
    std::string readbackSql;
    std::unique_ptr<QueryCallback> readback;
    std::optional<std::vector<MailPersistedRow>> persisted;
};
std::map<std::string,Attempt> attempts;
std::vector<MailPersistedRow> ReadPersistedRows(QueryResult const& rows)
{
    return rows ? *rows : std::vector<MailPersistedRow>{};
}
#include "RealmMailCommit.inc"

Queued Entry() { return {"mail-command",{{"from_guid",101},{"to_guid",1}}}; }
MailStagedHeader Header()
{
    return {7001,101,1,90,2000,"Delivery","A gift.",true,true,true,true,true,true};
}
std::vector<MailPersistedRow> Rows()
{
    return {{Header(),26000,1,MailSavedItem{901,41001,1,2}}};
}
Attempt& Fresh()
{
    attempts.clear(); CharacterDatabase = {}; RealmModule::Instance() = {};
    auto& attempt = attempts[Entry().id];
    attempt.admitted = Entry().payload; attempt.header = Header();
    attempt.attachments = {*Rows().front().item}; attempt.readbackSql = "SELECT exact mail 7001";
    auto transaction = std::make_shared<Transaction>();
    transaction->Append("SAVE_SOURCE_AND_MAIL");
    CommitMail(attempt,transaction);
    return attempt;
}
void Commit(bool success)
{
    auto& job = *CharacterDatabase.writes.back(); job.ready = true; job.success = success;
}
void Readback(QueryResult result)
{
    auto& job = *CharacterDatabase.reads.back(); job.ready = true; job.result = std::move(result);
}

LEEROY_TEST(RealmMailCommitCoreOwnsCommitAndReadbackWaitsForCompletion)
{
    auto& attempt = Fresh();
    CHECK_EQ(CharacterDatabase.writes.front()->transaction->statements.size(),1u);
    CHECK_EQ(CharacterDatabase.writes.front()->transaction->statements.front(),"SAVE_SOURCE_AND_MAIL");
    for (int i = 0; i < 100; ++i) CHECK(ReadResult(Entry(),101,attempt).state == StepState::Pending);
    CHECK(CharacterDatabase.reads.empty());
    Commit(true);
    CHECK(ReadResult(Entry(),101,attempt).state == StepState::Pending);
    CHECK_EQ(CharacterDatabase.reads.size(),1u);
    CHECK_EQ(CharacterDatabase.reads.front()->sql,attempt.readbackSql);
    CHECK(ReadResult(Entry(),101,attempt).state == StepState::Pending);
    Readback(std::make_shared<std::vector<MailPersistedRow>>(Rows()));
    auto result = ReadResult(Entry(),101,attempt);
    CHECK(result.state == StepState::Complete); CHECK_EQ(result.value,901u);
    CHECK(attempts.empty()); CHECK_EQ(CharacterDatabase.writes.size(),1u);
}

LEEROY_TEST(RealmMailCommitLostAcknowledgementStillAcceptsExactPersistedTransfer)
{
    auto& attempt = Fresh(); Commit(false);
    CHECK(ReadResult(Entry(),101,attempt).state == StepState::Pending);
    CHECK_EQ(CharacterDatabase.reads.size(),1u);
    for (int i = 0; i < 100; ++i) CHECK(ReadResult(Entry(),101,attempt).state == StepState::Pending);
    CHECK_EQ(CharacterDatabase.writes.size(),1u);
    Readback(std::make_shared<std::vector<MailPersistedRow>>(Rows()));
    CHECK(ReadResult(Entry(),101,attempt).state == StepState::Complete);
    CHECK(attempts.empty()); CHECK_EQ(CharacterDatabase.writes.size(),1u);
}

LEEROY_TEST(RealmMailCommitLostAcknowledgementWithoutProofRemainsUnknown)
{
    for (bool queryFailed : {false,true})
    {
        auto& attempt = Fresh(); Commit(false);
        CHECK(ReadResult(Entry(),101,attempt).state == StepState::Pending);
        Readback(queryFailed ? QueryResult{} : std::make_shared<std::vector<MailPersistedRow>>());
        CHECK(ReadResult(Entry(),101,attempt).state == StepState::Unknown);
        CHECK(attempts.contains(Entry().id));
        CHECK(ReadResult(Entry(),101,attempt).state == StepState::Unknown);
        CHECK_EQ(CharacterDatabase.writes.size(),1u); CHECK_EQ(CharacterDatabase.reads.size(),1u);
    }
}

LEEROY_TEST(RealmMailCommitMismatchedReadbackNeverProvesFailedCommit)
{
    auto& attempt = Fresh(); Commit(false);
    CHECK(ReadResult(Entry(),101,attempt).state == StepState::Pending);
    auto rows = Rows(); rows.front().item->owner = 102;
    Readback(std::make_shared<std::vector<MailPersistedRow>>(rows));
    CHECK(ReadResult(Entry(),101,attempt).state == StepState::Unknown);
    CHECK_EQ(CharacterDatabase.writes.size(),1u); CHECK(attempts.contains(Entry().id));
}

LEEROY_TEST(RealmMailCommitIdentityFenceStillPrecedesReadback)
{
    auto& attempt = Fresh(); Commit(false); RealmModule::Instance().epoch++;
    CHECK(ReadResult(Entry(),101,attempt).state == StepState::Unknown);
    CHECK(CharacterDatabase.reads.empty()); CHECK_EQ(CharacterDatabase.writes.size(),1u);
}
}
