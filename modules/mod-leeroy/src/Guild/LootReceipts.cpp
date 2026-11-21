// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LootReceipts.h"
#include "LootMaster.h"
#include "GuildPorts.h"
#include "DatabaseEnv.h"
#include "QueryCallback.h"
#include "QueryResult.h"
#include "Transaction.h"
#include "Realm.h"
#include <algorithm>
#include <map>
#include <memory>

namespace Leeroy::Guild
{
namespace
{
using Json = nlohmann::json;
constexpr char const* Schema =
    "CREATE TABLE IF NOT EXISTS leeroy_loot_give_receipt ("
    "realm INT UNSIGNED NOT NULL, guild_id INT UNSIGNED NOT NULL, owner INT UNSIGNED NOT NULL, "
    "command_id VARBINARY(128) NOT NULL, payload BLOB NOT NULL, state TINYINT UNSIGNED NOT NULL, "
    "PRIMARY KEY(realm,guild_id,owner,command_id)) ENGINE=InnoDB";
// state: 0 = reserved/unknown, 1 = inventory committed, 2 = rejected before transfer.
// Reservations survive a process crash and are NEVER automatically executed again.
enum class Phase { Schema, Probe, Lookup, Reserve, Reserved, Commit, Verify };
struct Io
{
    bool readReady = false;
    QueryResult rows;
    std::optional<bool> committed;
    std::unique_ptr<QueryCallback> read;
    std::unique_ptr<TransactionCallback> write;
};
struct Pending
{
    Json message;
    std::string id, replyId, payload, where;
    uint64 serial = 0;
    uint32 owner = 0, guild = 0;
    bool status = false;
    bool reservedHere = false;
    Phase phase = Phase::Lookup;
    std::optional<LootSourceObservation> source;
    std::shared_ptr<Io> io = std::make_shared<Io>();
};
std::map<std::string, Pending> pending;
bool schemaReady = false;

std::string Hex(std::string const& value)
{
    constexpr char digits[] = "0123456789abcdef";
    std::string out;
    for (unsigned char c : value) { out += digits[c >> 4]; out += digits[c & 15]; }
    return out;
}

bool ValidId(std::string const& id)
{
    return !id.empty() && id.size() <= 128 && std::all_of(id.begin(), id.end(), [](unsigned char c)
    {
        return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')
            || c == '-' || c == '_';
    });
}

void Write(Pending& p, CharacterDatabaseTransaction transaction)
{
    auto io = p.io;
    io->committed.reset();
    // Let the core own COMMIT: its lost-COMMIT path deliberately does not replay
    // the transaction. A raw COMMIT here would be misclassified as a lost query
    // and could replay an inventory snapshot. Readback, not this callback, is proof.
    io->write = std::make_unique<TransactionCallback>(CharacterDatabase.AsyncCommitTransaction(transaction));
    io->write->AfterComplete([weak = std::weak_ptr<Io>(io)](bool committed)
    {
        if (auto state = weak.lock()) state->committed = committed;
    });
}

void Read(Pending& p, bool probe = false)
{
    auto io = p.io;
    io->readReady = false;
    io->rows = {};
    // Aggregate always returns one row. A null result is a database failure, never absence.
    std::string const sql = probe
        ? "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() "
          "AND table_name='leeroy_loot_give_receipt'"
        : "SELECT COUNT(*),COALESCE(MAX(payload),''),CAST(COALESCE(MAX(state),0) AS UNSIGNED) "
          "FROM leeroy_loot_give_receipt WHERE " + p.where;
    io->read = std::make_unique<QueryCallback>(CharacterDatabase.AsyncQuery(sql));
    io->read->WithCallback([weak = std::weak_ptr<Io>(io)](QueryResult rows)
    {
        if (auto state = weak.lock())
        {
            state->rows = std::move(rows);
            state->readReady = true;
        }
    });
}

LeeroyCommandResult Begin(Json const& message, bool status)
{
    std::string error;
    auto cmd = ParseGiveLoot(message, error);
    if (!cmd) return {false, error};
    if (status && !ParseLootReceipt(message, error)) return {false, error};
    char const* idKey = status ? "give_id" : "id";
    if (!message.contains(idKey) || !message[idKey].is_string()
        || !ValidId(message[idKey].get<std::string>()))
        return {false, "bad field: give_id"};
    auto& module = LeeroyModule::Instance();
    if (!status && (!module.Enabled() || !module.Config().lootReceipts))
        return {false, "loot_receipt_disabled"};
    Pending p;
    p.message = message;
    p.id = message[idKey].get<std::string>();
    p.replyId = message.at("id").get<std::string>();
    p.serial = LeeroyCommandSerial();
    p.owner = module.HumanGuid();
    p.guild = module.GuildId();
    p.status = status;
    p.payload = Json{{"map", cmd->map}, {"instance_id", cmd->instanceId},
        {"source_entry", cmd->sourceEntry}, {"index", cmd->index},
        {"item_id", cmd->itemId}, {"to_guid", cmd->toGuid}}.dump();
    p.where = "realm=" + std::to_string(::realm.Id.Realm) + " AND guild_id=" + std::to_string(p.guild)
        + " AND owner=" + std::to_string(p.owner) + " AND command_id=X'" + Hex(p.id) + "'";
    auto found = pending.find(p.where);
    if (found != pending.end())
        return {false, found->second.payload == p.payload ? "loot_receipt_pending" : "loot_receipt_conflict"};
    if (pending.size() >= 128) return {false, "loot_receipt_busy"};
    p.source = LootSourceLedger::Instance().Observe(cmd->map, cmd->instanceId, cmd->sourceEntry);
    if (!status && !schemaReady)
    {
        auto transaction = CharacterDatabase.BeginTransaction();
        transaction->Append(Schema);
        Write(p, transaction);
        p.phase = Phase::Schema;
    }
    else
    {
        // ER_NO_SUCH_TABLE is fatal in the core. An installation that has never
        // opted in still accepts read-only status without creating a table.
        p.phase = schemaReady ? Phase::Lookup : Phase::Probe;
        Read(p, p.phase == Phase::Probe);
    }
    auto key = p.where;
    pending.emplace(std::move(key), std::move(p));
    return {true, {}, true};
}

std::optional<LeeroyCommandResult> Advance(Pending& p)
{
    auto& io = *p.io;
    if (io.write && !io.committed.has_value()) io.write->InvokeIfReady();
    if (io.read && !io.readReady) io.read->InvokeIfReady();
    if (p.phase == Phase::Schema || p.phase == Phase::Reserve || p.phase == Phase::Commit)
    {
        if (!io.committed.has_value()) return {};
        if (p.phase == Phase::Schema)
        {
            p.phase = Phase::Probe;
        }
        else if (p.phase == Phase::Reserve)
        {
            p.reservedHere = *io.committed;
            p.phase = Phase::Reserved;
        }
        else p.phase = Phase::Verify;
        // No pre-commit snapshot may prove this write. Always issue a new read.
        Read(p, p.phase == Phase::Probe);
        return {};
    }
    if (!io.readReady) return {};
    if (!io.rows) return LeeroyCommandResult{false, "loot_receipt_unavailable"};
    auto* fields = io.rows->Fetch();
    uint64 const count = fields[0].Get<uint64>();
    if (p.phase == Phase::Probe)
    {
        if (count != 1) return LeeroyCommandResult{false, "loot_receipt_absent"};
        schemaReady = true;
        p.phase = Phase::Lookup;
        Read(p);
        return {};
    }
    uint64 const state = fields[2].Get<uint64>();
    if (count && (count != 1 || fields[1].Get<std::string>() != p.payload || state > 2))
        return LeeroyCommandResult{false, "loot_receipt_conflict"};
    if (count && state == 1) return LeeroyCommandResult{true, {}};
    if (count && state == 2) return LeeroyCommandResult{false, "loot_receipt_rejected"};
    if (p.phase == Phase::Lookup)
    {
        if (count || p.status)
            return LeeroyCommandResult{false, count ? "loot_receipt_pending" : "loot_receipt_absent"};
        auto transaction = CharacterDatabase.BeginTransaction();
        transaction->Append("INSERT INTO leeroy_loot_give_receipt(realm,guild_id,owner,command_id,payload,state) VALUES ("
            + std::to_string(::realm.Id.Realm) + "," + std::to_string(p.guild) + "," + std::to_string(p.owner)
            + ",X'" + Hex(p.id) + "',X'" + Hex(p.payload) + "',0)");
        Write(p, transaction);
        p.phase = Phase::Reserve;
        return {};
    }
    if (p.phase != Phase::Reserved || !p.reservedHere || !count)
        return LeeroyCommandResult{false, "loot_receipt_pending"};

    // Inventory persistence for two awards to one player must not race across DB workers.
    for (auto const& [key, other] : pending)
        if (&other != &p && (other.phase == Phase::Commit || other.phase == Phase::Verify)
            && other.message.at("to_guid") == p.message.at("to_guid"))
            return {};

    auto& module = LeeroyModule::Instance();
    auto transaction = CharacterDatabase.BeginTransaction();
    LeeroyCommandResult result{false, "not_eligible"};
    // Capture identity/source at admission, then revalidate after the database wait.
    // Keep no Player/Map pointer across that wait. A disconnected caller cannot start a late give.
    if (p.source && module.Enabled() && module.Config().lootReceipts && p.owner == module.HumanGuid()
        && p.guild == module.GuildId() && module.Bridge().Connected()
        && p.serial == module.Bridge().ConnectionSerial())
        result = TransferLoot(p.message, transaction, p.source);
    transaction->Append("UPDATE leeroy_loot_give_receipt SET state=" + std::string(result.ok ? "1" : "2")
        + " WHERE " + p.where + " AND state=0 AND payload=X'" + Hex(p.payload) + "'");
    Write(p, transaction);
    p.phase = Phase::Commit;
    return {};
}
}

LeeroyCommandResult BeginLootReceipt(Json const& message) { return Begin(message, false); }
LeeroyCommandResult GetLootReceipt(Json const& message) { return Begin(message, true); }

void TickLootReceipts()
{
    for (auto it = pending.begin(); it != pending.end();)
    {
        std::optional<LeeroyCommandResult> result;
        try { result = Advance(it->second); }
        catch (...) { result = LeeroyCommandResult{false, "loot_receipt_unavailable"}; }
        if (!result) { ++it; continue; }
        auto const& p = it->second;
        LeeroyModule::Instance().Bridge().Reply(p.serial, EncodeAck(NowUnixMs(), p.replyId, result->ok, result->error));
        it = pending.erase(it);
    }
}
}
