// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_LEEROY_LEEROY_GUILD_COMMANDS_H
#define MOD_LEEROY_LEEROY_GUILD_COMMANDS_H

#include <array>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>
#include <nlohmann/json.hpp>

namespace Leeroy::Guild
{
inline constexpr std::array<std::string_view, 12> kCommandTypes = {
    "form_raid", "summon", "clear_lockout", "set_loot_master", "give_loot",
    "start_offline_raid", "gear_up", "raid_control", "remove_friend", "set_spec",
    "bot_do", "stock_items"
};
inline constexpr std::array<std::string_view, 6> kBotStrategyAdd = {
    "behind", "tank face", "mark rti", "boost", "ss tank", "ss healer"
};
inline constexpr std::array<std::string_view, 4> kBotStrategyRemove = {
    "behind", "boost", "ss tank", "ss healer"
};
inline constexpr std::array<std::string_view, 8> kRtiIcons = {
    "skull", "cross", "square", "moon", "triangle", "diamond", "circle", "star"
};
inline constexpr std::array<std::string_view, 9> kFormations = {
    "arrow", "circle", "chaos", "line", "near", "far", "melee", "queue", "shield"
};
inline constexpr std::array<std::string_view, 3> kBotSpells = {"bloodlust", "heroism", "rebirth"};
inline constexpr std::array<std::string_view, 1> kBotActions = {"buff"};
inline constexpr std::array<std::uint32_t, 6> kStockItems = {43015, 34753, 46377, 46376, 46379, 46378};

struct FormRaidCmd
{
    std::uint32_t leaderGuid = 0;
    std::vector<std::uint32_t> members;
    std::uint32_t size = 0;
    bool heroic = false;
    std::uint32_t mapId = 0, mainTankGuid = 0;
    std::vector<std::uint32_t> assistants;
};
struct SummonCmd
{
    std::uint32_t mapId = 0;
    std::vector<std::uint32_t> guids;
    std::string where = "entrance";
    bool summonHuman = false;
    std::uint32_t summonerGuid = 0;
};
struct ClearLockoutCmd
{
    std::uint32_t mapId = 0;
    std::vector<std::uint32_t> guids;
    std::vector<std::uint8_t> difficulties = {0, 1, 2, 3};
};
struct SetLootMasterCmd { std::uint32_t memberGuid = 0, looterGuid = 0, threshold = 4; };
struct GiveLootCmd
{
    std::uint32_t map = 0, instanceId = 0, sourceEntry = 0, index = 0, itemId = 0, toGuid = 0;
};
struct OfflineRaidCmd
{
    std::uint32_t mapId = 0, size = 0;
    bool heroic = false;
    std::uint32_t leaderGuid = 0;
    std::vector<std::uint32_t> members, assistants;
    std::uint32_t lootMasterGuid = 0, maxWipes = 6, maxWipesPerBoss = 3, timeoutMin = 180, minMembers = 0;
};
struct GearUpCmd
{
    std::vector<std::uint32_t> guids;
    std::uint32_t ilvl = 264, levelTo = 80;
};
struct RaidControlCmd
{
    std::uint32_t memberGuid = 0;
    std::string action;
    std::uint32_t leaderGuid = 0;
    std::string param;
};
struct RemoveFriendCmd { std::uint32_t guid = 0; };
struct SetSpecCmd
{
    std::uint32_t guid = 0, specNo = 0;
    bool glyphs = true;
};
struct BotDoCmd
{
    std::vector<std::uint32_t> guids;
    std::string action, param;
    std::uint32_t targetGuid = 0;
};
struct StockItemsCmd
{
    std::uint32_t guid = 0;
    std::vector<std::uint32_t> items;
    std::uint32_t count = 5;
};

// Field parsers do not inspect the envelope; DecodeCommand owns v/type/id validation.
std::optional<FormRaidCmd> ParseFormRaid(nlohmann::json const&, std::string& error);
std::optional<SummonCmd> ParseSummon(nlohmann::json const&, std::string& error);
std::optional<ClearLockoutCmd> ParseClearLockout(nlohmann::json const&, std::string& error);
std::optional<SetLootMasterCmd> ParseSetLootMaster(nlohmann::json const&, std::string& error);
std::optional<GiveLootCmd> ParseGiveLoot(nlohmann::json const&, std::string& error);
std::optional<OfflineRaidCmd> ParseOfflineRaid(nlohmann::json const&, std::string& error);
std::optional<GearUpCmd> ParseGearUp(nlohmann::json const&, std::string& error);
std::optional<RaidControlCmd> ParseRaidControl(nlohmann::json const&, std::string& error);
std::optional<RemoveFriendCmd> ParseRemoveFriend(nlohmann::json const&, std::string& error);
std::optional<SetSpecCmd> ParseSetSpec(nlohmann::json const&, std::string& error);
std::optional<BotDoCmd> ParseBotDo(nlohmann::json const&, std::string& error);
std::optional<StockItemsCmd> ParseStockItems(nlohmann::json const&, std::string& error);
void RegisterGuildCommandTypes();
bool BotDoParamAllowed(std::string_view action, std::string_view param);
std::uint8_t RaidDifficulty(std::uint32_t size, bool heroic); // size has passed its field parser

struct JobEvent
{
    std::string id, kind, state, reason, stage;
    std::vector<std::uint32_t> guids;
    std::vector<std::pair<std::uint32_t, std::string>> skipped;
    std::uint32_t kills = 0, wipes = 0, instanceId = 0, guid = 0, level = 0, avgIlvl = 0;
};
nlohmann::json MakeJobEvent(JobEvent const&);
}

#endif
