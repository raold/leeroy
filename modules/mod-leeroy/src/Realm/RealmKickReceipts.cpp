// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "RealmModule.h"
#include "RealmPorts.h"
#include "LeeroyRealmReadmit.h"
#include "DatabaseEnv.h"
#include "Log.h"
#include "QueryResult.h"
#include <map>
#include <vector>

namespace Leeroy::Realm
{
namespace
{
constexpr std::uint64_t RetryMs = 60000;

// Created by the module on first use, not by the core's updater: with the cast
// guard and Realm off the characters DB stays exactly as it was.
constexpr char const* CreateTable =
    "CREATE TABLE IF NOT EXISTS `leeroy_kick_receipt` ("
    "`guild_id` INT UNSIGNED NOT NULL, "
    "`human_guid` INT UNSIGNED NOT NULL, "
    "`guid` INT UNSIGNED NOT NULL, "
    "`kick_ts` BIGINT UNSIGNED NOT NULL, "
    "`kick_rank` TINYINT UNSIGNED NOT NULL, "
    "`receipt_state` TINYINT UNSIGNED NOT NULL, "
    "PRIMARY KEY (`guild_id`, `human_guid`, `guid`)"
    ") ENGINE=InnoDB COMMENT='mod-leeroy: his published kicks, for a consented guild_readmit'";

struct Store { bool ready = false; std::uint64_t retryAt = 0; };
std::map<std::pair<std::uint32_t, std::uint32_t>, Store> stores; // World thread only.

bool Active()
{
    return Config().enable && (Config().castGuard || RealmModule::Instance().On());
}

StoredKickReceipt Row(Field const* fields)
{
    return {fields[0].Get<uint32>(), fields[1].Get<uint64>(), fields[2].Get<uint8>(), fields[3].Get<uint8>()};
}
}

bool EnsureKickReceiptStore(GuardState& guard)
{
    if (!Active())
        return false;
    auto& store = stores[{guard.Guild(), guard.Human()}];
    if (store.ready)
        return true;
    auto const steady = NowMs();
    if (steady < store.retryAt)
        return false;
    store.retryAt = steady + RetryMs;
    auto const unixNow = NowUnixMs();
    if (unixNow <= static_cast<std::int64_t>(kReadmitWindowMs))
    {
        LOG_ERROR("module.leeroy", "mod-leeroy: cast guard kick receipts not restored: no wall clock");
        return false;
    }
    auto const now = static_cast<std::uint64_t>(unixNow);
    CharacterDatabase.DirectExecute(CreateTable);
    // COUNT(*) always has a row: no result is a failed read, not an empty table.
    if (!CharacterDatabase.Query("SELECT COUNT(*) FROM `leeroy_kick_receipt`"))
    {
        LOG_ERROR("module.leeroy", "mod-leeroy: cast guard kick receipts unavailable: leeroy_kick_receipt unreadable");
        return false;
    }
    // Older than the consent window, a receipt authorizes nothing.
    CharacterDatabase.DirectExecute("DELETE FROM `leeroy_kick_receipt` WHERE `kick_ts` < {}", now - kReadmitWindowMs);
    std::vector<StoredKickReceipt> rows;
    if (auto result = CharacterDatabase.Query("SELECT `guid`, `kick_ts`, `kick_rank`, `receipt_state` "
            "FROM `leeroy_kick_receipt` WHERE `guild_id` = {} AND `human_guid` = {} "
            "ORDER BY `kick_ts` DESC LIMIT {}", guard.Guild(), guard.Human(), kMaxKickReceipts))
    {
        do rows.push_back(Row(result->Fetch()));
        while (result->NextRow());
    }
    auto const restored = guard.RestoreReceipts(rows, now, kReadmitWindowMs);
    store.ready = true;
    LOG_INFO("module.leeroy", "mod-leeroy: cast guard restored {} of {} stored kick receipts", restored, rows.size());
    return true;
}

bool StoreKickReceipt(GuardState& guard, std::uint32_t guid, KickReceipt const& receipt)
{
    if (!EnsureKickReceiptStore(guard))
        return false;
    CharacterDatabase.DirectExecute("REPLACE INTO `leeroy_kick_receipt` "
        "(`guild_id`, `human_guid`, `guid`, `kick_ts`, `kick_rank`, `receipt_state`) VALUES ({}, {}, {}, {}, {}, {})",
        guard.Guild(), guard.Human(), guid, receipt.kickTs, std::uint32_t{receipt.rank},
        std::uint32_t{KickReceiptCode(receipt.state)});
    auto result = CharacterDatabase.Query("SELECT `guid`, `kick_ts`, `kick_rank`, `receipt_state` "
        "FROM `leeroy_kick_receipt` WHERE `guild_id` = {} AND `human_guid` = {} AND `guid` = {}",
        guard.Guild(), guard.Human(), guid);
    return result && StoredReceiptIs(Row(result->Fetch()), guid, receipt);
}
}
