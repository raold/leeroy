// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

// bridge_protocol.jsonl is shared with the mind's suite (mind/tests/test_wire_contract.py), which runs every row
// through its Python copy of DecodeCommand (the fake bridge's decoder) and checks that the mind encodes each valid
// row exactly. Here the same rows go through the real decoder and, for an extension command, its handler's parser.
//
// LEEROY_WIRE_CAPTURE names a file of command lines the mind's test doubles saw, each with the verdict of their
// decoder (mind/leeroy_mind/testing/wire.py writes it when the variable is set for a mind test run).
// Bridge_CapturedLinesParse replays it: the real decoder must agree with the fake's on every line, and every
// extension line the fake passed on must pass its handler's parser. Unset, as in CI, it has nothing to read.

#include "LeeroyGuildAddPolicy.h"
#include "LeeroyGuildCommands.h"
#include "LeeroyLead.h"
#include "LeeroyMechanicPolicy.h"
#include "LeeroyOwnerSay.h"
#include "LeeroyProtocol.h"
#include "LeeroyRaidLeadPolicy.h"
#include "LeeroyRealmCommands.h"
#include "LeeroyStrings.h"
#include "LeeroyTrainingProtocol.h"
#include "LeeroyWorldProtocol.h"
#include "TestHarness.h"
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>

using namespace Leeroy::Guild;
using json = nlohmann::json;

namespace
{
std::vector<json> Rows()
{
    std::ifstream file(LEEROY_TEST_FIXTURES "/bridge_protocol.jsonl");
    if (!file)
        throw std::runtime_error("cannot open bridge protocol fixture");
    std::vector<json> rows;
    for (std::string line; std::getline(file, line);)
        if (!line.empty())
            rows.push_back(json::parse(line));
    return rows;
}

std::string Line(json const& row)
{
    return row.contains("line") ? row.at("line").get<std::string>() : row.at("msg").dump();
}

constexpr std::string_view kDayCheck = "day_check_";

// DayPlans.cpp: a day check carries its check object and is never also a prepared start; the driver preparations
// ride an original check.
bool DayCheckShape(json const& msg, std::string_view original)
{
    if (!msg.contains("day_check") || msg.contains("day_prepared"))
        return false;
    auto const& check = msg.at("day_check");
    return check.is_object() && check.contains("request_id") && check["request_id"].is_string()
        && check.contains("origins") && check["origins"].is_array() && check["origins"].size() <= 40
        && (!check["origins"].empty() || original == "start_member_dungeon" || check.contains("heroic_start"));
}

template <typename T>
bool Has(std::optional<T> const& value) { return value.has_value(); }

// The handler's own parser for an extension command; nullopt for a type whose handler parses inline in game code.
std::optional<bool> Parse(std::string_view type, json const& m, std::string& e)
{
    namespace R = Leeroy::Realm;
    namespace T = Leeroy::Training;
    if (type.starts_with(kDayCheck))
    {
        auto original = type.substr(kDayCheck.size());
        auto inner = Parse(original, m, e);
        return inner ? std::optional<bool>(DayCheckShape(m, original) && *inner) : std::nullopt;
    }
    if (type == "day_prepare_offline_driver")
        return DayCheckShape(m, "start_offline_raid") && m.contains("day_driver_ticket") && Has(ParseOfflineRaid(m, e));
    if (type == "member_dungeon_inspect" || type == "member_dungeon_prepare_driver")
        return DayCheckShape(m, "start_member_dungeon") && Has(ParseMemberDungeon(m, e));
    if (type == "form_raid") return Has(ParseFormRaid(m, e));
    if (type == "summon") return Has(ParseSummon(m, e));
    if (type == "clear_lockout") return Has(ParseClearLockout(m, e));
    if (type == "set_loot_master") return Has(ParseSetLootMaster(m, e));
    if (type == "give_loot") return Has(ParseGiveLoot(m, e));
    if (type == "get_loot_receipt") return Has(ParseLootReceipt(m, e));
    if (type == "start_offline_raid") return Has(ParseOfflineRaid(m, e));
    if (type == "start_member_dungeon") return Has(ParseMemberDungeon(m, e));
    if (type == "member_dungeon_stop" || type == "member_dungeon_status") return Has(ParseDungeonTarget(m, e));
    if (type == "offline_raid_stop") return Has(ParseOfflineRaidStop(m, e));
    if (type == "gear_up") return Has(ParseGearUp(m, e));
    if (type == "raid_control") return Has(ParseRaidControl(m, e));
    if (type == "remove_friend") return Has(ParseRemoveFriend(m, e));
    if (type == "set_spec") return Has(ParseSetSpec(m, e));
    if (type == "bot_do") return Has(ParseBotDo(m, e));
    if (type == "stock_items") return Has(ParseStockItems(m, e));
    if (type == "string_do") return Has(ParseStringDo(m, e));
    if (type == "guild_add_roster") return Has(ParseGuildAddRoster(m, e));
    if (type == "lead_do") return Has(ParseLeadDo(m, e));
    if (type == "raid_lead") return Has(ParseRaidLead(m, e));
    if (type == "raid_mechanics") return Has(ParseRaidMechanics(m, e));
    if (type == "owner_say") return Has(ParseOwnerSay(m, e));
    if (type == "telemetry") return Has(T::ParseTelemetry(m, e));
    if (type == "get_stats") return Has(T::ParseGetStats(m, e));
    if (type == "get_training_cleanup") return Has(T::ParseGetTrainingCleanup(m, e));
    if (type == "get_vocab") return Has(T::ParseGetVocab(m, e));
    if (type == "custom_strategy") return Has(T::ParseCustomStrategy(m, e));
    if (type == "dc_setting") return Has(T::ParseDcSetting(m, e));
    if (type == "dummy_trial") return Has(T::ParseDummyTrial(m, e));
    if (type == "get_rivals") return Has(T::ParseGetRivals(m, e));
    if (type == "start_benchmark") return Has(T::ParseStartBenchmark(m, e));
    if (type == "get_world_roster") return Has(Leeroy::World::RosterRequest(m, e));
    if (type == "realm_actors") return Has(R::ParseActors(m, e));
    if (type == "realm_say") return Has(R::ParseSay(m, e));
    if (type == "world_say") return Has(R::ParseWorldSay(m, e));
    if (type == "realm_emote") return Has(R::ParseEmote(m, e));
    if (type == "realm_move") return Has(R::ParseMove(m, e));
    if (type == "realm_visit") return Has(R::ParseVisit(m, e));
    if (type == "rpg_status") return Has(R::ParseRpgStatus(m, e));
    if (type == "leave_pvp") return Has(R::ParseLeavePvp(m, e));
    if (type == "get_realm_pvp") return Has(R::ParseGetRealmPvp(m, e));
    if (type == "realm_forget") return Has(R::ParseRealmForget(m, e));
    if (type == "rearm") return Has(R::ParseRearm(m, e));
    if (type == "wg_join") return Has(R::ParseWgJoin(m, e));
    if (type == "arena_team") return Has(R::ParseArenaTeam(m, e));
    if (type == "arena_queue") return Has(R::ParseArenaQueue(m, e));
    if (type == "duel") return Has(R::ParseDuel(m, e));
    if (type == "craft") return Has(R::ParseCraft(m, e));
    if (type == "item_count") return Has(R::ParseItemCount(m, e));
    if (type == "equip") return Has(R::ParseEquip(m, e));
    if (type == "set_profession") return Has(R::ParseSetProfession(m, e));
    if (type == "transmog_set") return Has(R::ParseTransmogSet(m, e));
    if (type == "calendar_post") return Has(R::ParseCalendarPost(m, e));
    if (type == "calendar_signup") return Has(R::ParseCalendarSignup(m, e));
    if (type == "calendar_update") return Has(R::ParseCalendarUpdate(m, e));
    if (type == "mail_send") return Has(R::ParseMailSend(m, e));
    if (type == "bank_tab_buy") return Has(R::ParseBankTabBuy(m, e));
    if (type == "bank_money") return Has(R::ParseBankMoney(m, e));
    if (type == "bank_deposit") return Has(R::ParseBankDeposit(m, e));
    if (type == "bank_withdraw") return Has(R::ParseBankWithdraw(m, e));
    if (type == "bank_stock") return Has(R::ParseBankStock(m, e));
    if (type == "grant_achievement") return Has(R::ParseGrantAchievement(m, e));
    if (type == "guild_rename") return Has(R::ParseGuildRename(m, e));
    if (type == "guild_grow") return Has(R::ParseGuildGrow(m, e));
    if (type == "guild_admit") return Has(R::ParseGuildAdmit(m, e));
    if (type == "guild_note") return Has(R::ParseGuildNote(m, e));
    if (type == "guild_readmit") return Has(R::ParseGuildReadmit(m, e));
    if (type == "announce") return Has(R::ParseAnnounce(m, e));
    return std::nullopt;   // day_begin, day_status, day_revoke: Realm/DayPlans.cpp parses inline
}

// The server registers each extension type its handlers serve; a row naming an unknown type stays unknown.
void Register(std::vector<json> const& rows)
{
    RegisterGuildCommandTypes();
    Leeroy::Realm::RegisterRealmCommandTypes();
    Leeroy::Training::RegisterTrainingCommandTypes();
    for (auto const& row : rows)
        if (row.contains("msg") && row["msg"].is_object() && row["msg"].contains("type") && row["msg"]["type"].is_string()
            && row.at("error").get<std::string>().rfind(std::string(Leeroy::Err::UnknownType), 0) != 0)
            Leeroy::RegisterExtensionCommand(row["msg"]["type"].get<std::string>());   // false for a core type
}
}

LEEROY_TEST(Bridge_FixtureLinesDecodeAsRecorded)
{
    auto const rows = Rows();
    CHECK(rows.size() > 40);
    Register(rows);
    for (auto const& row : rows)
    {
        std::string const name = row.at("name").get<std::string>();
        std::string const expected = row.at("error").get<std::string>();
        auto const decoded = Leeroy::DecodeCommand(Line(row));
        if (decoded.command.has_value() != row.at("valid").get<bool>() || decoded.error != expected)
            LeeroyTest::Fail(__FILE__, __LINE__, name + ": decoded as '" + decoded.error + "'");
        if (row.contains("ackable"))
            CHECK_EQ(decoded.ackable, row.at("ackable").get<bool>());
        std::string const id = row.contains("msg") && row["msg"].is_object() && row["msg"].contains("id")
            && row["msg"]["id"].is_string() ? row["msg"]["id"].get<std::string>() : std::string();
        CHECK_EQ(decoded.id, id);
    }
}

LEEROY_TEST(Bridge_FixtureExtensionsParseInTheirHandlers)
{
    auto const rows = Rows();
    Register(rows);
    int parsed = 0;
    for (auto const& row : rows)
    {
        if (!row.at("valid").get<bool>() || !row.contains("msg") || row.at("name") == "extension_fields_unchecked")
            continue;
        auto const decoded = Leeroy::DecodeCommand(Line(row));
        if (!decoded.command || decoded.command->type != Leeroy::CommandType::Extension)
            continue;
        std::string error = "previous error";
        auto const ok = Parse(decoded.command->extType, decoded.command->raw, error);
        if (!ok)
            throw std::runtime_error("no parser for bridge protocol fixture type " + decoded.command->extType);
        if (!*ok)
            LeeroyTest::Fail(__FILE__, __LINE__, row.at("name").get<std::string>() + ": refused '" + error + "'");
        ++parsed;
    }
    CHECK(parsed >= 13);
}

LEEROY_TEST(Bridge_CapturedLinesParse)
{
    char const* path = std::getenv("LEEROY_WIRE_CAPTURE");
    if (!path || !*path)
        return;
    std::ifstream file(path);
    CHECK(static_cast<bool>(file));
    Register({});
    std::size_t lines = 0, differ = 0, refused = 0, unchecked = 0;
    for (std::string text; std::getline(file, text);)
    {
        if (text.empty())
            continue;
        ++lines;
        // {"line", "error": what the fake's decoder said ("" accepted), "test"}
        auto const row = json::parse(text);
        std::string const line = row.at("line").get<std::string>(), fake = row.at("error").get<std::string>();
        std::string const test = row.value("test", std::string("?"));
        // Which extension types a fake bridge knows is its own setting (an older server): not the decoder's rule.
        if (fake.rfind(std::string(Leeroy::Err::UnknownType), 0) == 0)
            continue;
        auto const msg = json::parse(line, nullptr, false);
        if (msg.is_object() && msg.contains("type") && msg["type"].is_string())
            Leeroy::RegisterExtensionCommand(msg["type"].get<std::string>());
        auto const decoded = Leeroy::DecodeCommand(line);
        if (decoded.error != fake)
        {
            ++differ;
            std::cout << "  decoded '" << decoded.error << "', the fake '" << fake << "' (" << test << "): "
                      << line.substr(0, 300) << "\n";
            continue;
        }
        if (!decoded.command || decoded.command->type != Leeroy::CommandType::Extension)
            continue;   // a refusal both decoders gave was the test's to expect
        std::string error;
        auto const parsed = Parse(decoded.command->extType, decoded.command->raw, error);
        if (!parsed)
            ++unchecked;
        else if (!*parsed)
        {
            ++refused;
            std::cout << "  refused (" << error << ") after the fake passed it on (" << test << "): "
                      << line.substr(0, 300) << "\n";
        }
    }
    std::cout << "  captured lines: " << lines << ", decoded otherwise: " << differ << ", refused by a parser: "
              << refused << ", without a parser: " << unchecked << "\n";
    CHECK_EQ(differ, std::size_t(0));
    CHECK_EQ(refused, std::size_t(0));
}
