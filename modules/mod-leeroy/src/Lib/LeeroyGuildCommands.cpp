// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyGuildCommands.h"
#include "LeeroyProtocol.h"
#include <algorithm>
#include <limits>
#include <set>

namespace Leeroy::Guild
{
namespace
{
using json = nlohmann::json;
constexpr auto kU32Max = std::numeric_limits<std::uint32_t>::max();
constexpr auto kNoListLimit = std::numeric_limits<std::size_t>::max();
constexpr std::array<std::string_view, 7> kBotCommandActions = {"co", "nc", "rti", "formation", "cast", "do", "use"};
constexpr std::array<std::string_view, 5> kRaidActions = {"ready_check", "dc_on", "dc_off", "dc_pause", "disband"};

template <typename Range, typename T>
bool Contains(Range const& values, T const& value)
{
    return std::find(values.begin(), values.end(), value) != values.end();
}

bool Unsigned(json const& value, std::uint32_t low, std::uint32_t high)
{
    return value.is_number_unsigned() && value.get<std::uint64_t>() >= low
        && value.get<std::uint64_t>() <= high;
}

class Fields
{
public:
    Fields(json const& value, std::string& error) : _value(value), _error(error) { _error.clear(); }
    bool Failed() const { return !_error.empty(); }
    void Require(bool valid, char const* key)
    {
        if (!Failed() && !valid)
            _error = std::string(Err::BadField) + key;
    }
    json const* Find(char const* key) const
    {
        if (!_value.is_object())
            return nullptr;
        auto it = _value.find(key);
        return it == _value.end() ? nullptr : &*it;
    }
    void Number(char const* key, std::uint32_t& out, std::uint32_t low = 1,
        std::uint32_t high = kU32Max, bool optional = false)
    {
        if (Failed())
            return;
        auto v = Find(key);
        if (!v && optional)
            return;
        Require(v && Unsigned(*v, low, high), key);
        if (!Failed())
            out = v->get<std::uint32_t>();
    }
    void Boolean(char const* key, bool& out)
    {
        if (Failed())
            return;
        if (auto v = Find(key))
        {
            Require(v->is_boolean(), key);
            if (!Failed())
                out = v->get<bool>();
        }
    }
    void String(char const* key, std::string& out, bool optional = false)
    {
        if (Failed())
            return;
        auto v = Find(key);
        if (!v && optional)
            return;
        Require(v && v->is_string(), key);
        if (!Failed())
            out = v->get<std::string>();
    }
    template <typename T>
    void List(char const* key, std::vector<T>& out, std::size_t low = 1,
        std::size_t high = kNoListLimit, std::uint32_t itemLow = 1,
        std::uint32_t itemHigh = kU32Max, bool optional = false)
    {
        if (Failed())
            return;
        auto v = Find(key);
        if (!v && optional)
            return;
        Require(v && v->is_array() && v->size() >= low && v->size() <= high, key);
        if (Failed())
            return;
        std::set<std::uint32_t> seen;
        out.clear();
        for (auto const& item : *v)
        {
            Require(Unsigned(item, itemLow, itemHigh), key);
            if (Failed())
                return;
            auto number = item.get<std::uint32_t>();
            Require(seen.insert(number).second, key);
            if (Failed())
                return;
            out.push_back(static_cast<T>(number));
        }
    }
    void RaidSize(std::uint32_t& size)
    {
        Number("size", size);
        Require(size == 10 || size == 25, "size");
    }
    void Assistants(std::vector<std::uint32_t>& assistants, std::vector<std::uint32_t> const& members)
    {
        List("assistants", assistants, 0, members.size(), 1, kU32Max, true);
        Require(std::all_of(assistants.begin(), assistants.end(),
            [&](auto guid) { return Contains(members, guid); }), "assistants");
    }
    template <typename T>
    std::optional<T> Result(T out) const
    {
        return Failed() ? std::nullopt : std::optional<T>(std::move(out));
    }

private:
    json const& _value;
    std::string& _error;
};
}

std::optional<FormRaidCmd> ParseFormRaid(json const& j, std::string& error)
{
    Fields f(j, error);
    FormRaidCmd out;
    f.Number("leader_guid", out.leaderGuid);
    // Members precede size in the contract. Use a valid size as their bound without
    // reporting a later field's error before malformed members have been checked.
    auto size = f.Find("size");
    auto cap = size && Unsigned(*size, 10, 25) && (*size == 10 || *size == 25)
        ? size->get<std::uint32_t>() - 1 : 24;
    f.List("members", out.members, 1, cap);
    f.Require(!Contains(out.members, out.leaderGuid), "members");
    f.RaidSize(out.size);
    f.Boolean("heroic", out.heroic);
    f.Number("map_id", out.mapId, 0);
    f.Number("main_tank_guid", out.mainTankGuid);
    f.Require(out.mainTankGuid == out.leaderGuid || Contains(out.members, out.mainTankGuid), "main_tank_guid");
    f.Assistants(out.assistants, out.members);
    return f.Result(std::move(out));
}

std::optional<SummonCmd> ParseSummon(json const& j, std::string& error)
{
    Fields f(j, error);
    SummonCmd out;
    f.Number("map_id", out.mapId, 0);
    f.List("guids", out.guids);
    f.String("where", out.where, true);
    f.Require(out.where == "entrance" || out.where == "inside", "where");
    f.Boolean("summon_human", out.summonHuman);
    f.Number("summoner_guid", out.summonerGuid, 0, kU32Max, true);
    return f.Result(std::move(out));
}

std::optional<ClearLockoutCmd> ParseClearLockout(json const& j, std::string& error)
{
    Fields f(j, error);
    ClearLockoutCmd out;
    f.Number("map_id", out.mapId, 0);
    f.List("guids", out.guids);
    f.List("difficulties", out.difficulties, 1, 4, 0, 3, true);
    return f.Result(std::move(out));
}

std::optional<SetLootMasterCmd> ParseSetLootMaster(json const& j, std::string& error)
{
    Fields f(j, error);
    SetLootMasterCmd out;
    f.Number("member_guid", out.memberGuid);
    f.Number("looter_guid", out.looterGuid);
    f.Number("threshold", out.threshold, 2, 5, true);
    return f.Result(std::move(out));
}

std::optional<GiveLootCmd> ParseGiveLoot(json const& j, std::string& error)
{
    Fields f(j, error);
    GiveLootCmd out;
    f.Number("map", out.map, 0);
    f.Number("instance_id", out.instanceId, 0);
    f.Number("source_entry", out.sourceEntry);
    f.Number("index", out.index, 0, 255);
    f.Number("item_id", out.itemId);
    f.Number("to_guid", out.toGuid);
    return f.Result(std::move(out));
}

std::optional<OfflineRaidCmd> ParseOfflineRaid(json const& j, std::string& error)
{
    Fields f(j, error);
    OfflineRaidCmd out;
    f.Number("map_id", out.mapId, 0);
    f.RaidSize(out.size);
    f.Boolean("heroic", out.heroic);
    f.Number("leader_guid", out.leaderGuid);
    f.List("members", out.members, 1, out.size ? out.size - 1 : 0);
    f.Require(!Contains(out.members, out.leaderGuid), "members");
    f.Assistants(out.assistants, out.members);
    f.Number("loot_master_guid", out.lootMasterGuid);
    f.Require(out.lootMasterGuid == out.leaderGuid || Contains(out.members, out.lootMasterGuid), "loot_master_guid");
    f.Number("max_wipes", out.maxWipes, 1, 20, true);
    f.Number("max_wipes_per_boss", out.maxWipesPerBoss, 1, 10, true);
    f.Number("timeout_min", out.timeoutMin, 10, 360, true);
    auto count = static_cast<std::uint32_t>(out.members.size()) + 1;
    out.minMembers = std::min(out.size * 4 / 5, count);
    f.Number("min_members", out.minMembers, 1, count, true);
    return f.Result(std::move(out));
}

std::optional<GearUpCmd> ParseGearUp(json const& j, std::string& error)
{
    Fields f(j, error);
    GearUpCmd out;
    f.List("guids", out.guids, 1, 40);
    f.Number("ilvl", out.ilvl, 1, 400, true);
    f.Number("level_to", out.levelTo, 1, 80, true);
    return f.Result(std::move(out));
}

std::optional<RaidControlCmd> ParseRaidControl(json const& j, std::string& error)
{
    Fields f(j, error);
    RaidControlCmd out;
    f.Number("member_guid", out.memberGuid);
    f.String("action", out.action);
    f.Require(Contains(kRaidActions, out.action), "action");
    bool disband = out.action == "disband";
    f.Number("leader_guid", out.leaderGuid, disband ? 0 : 1, kU32Max, disband);
    f.String("param", out.param, true);
    return f.Result(std::move(out));
}

std::optional<RemoveFriendCmd> ParseRemoveFriend(json const& j, std::string& error)
{
    Fields f(j, error);
    RemoveFriendCmd out;
    f.Number("guid", out.guid);
    return f.Result(std::move(out));
}

std::optional<SetSpecCmd> ParseSetSpec(json const& j, std::string& error)
{
    Fields f(j, error);
    SetSpecCmd out;
    f.Number("guid", out.guid);
    f.Number("spec_no", out.specNo, 0, 9);
    f.Boolean("glyphs", out.glyphs);
    return f.Result(std::move(out));
}

std::optional<BotDoCmd> ParseBotDo(json const& j, std::string& error)
{
    Fields f(j, error);
    BotDoCmd out;
    f.List("guids", out.guids, 1, 40);
    f.String("action", out.action);
    f.Require(Contains(kBotCommandActions, out.action), "action");
    f.String("param", out.param);
    f.Require(BotDoParamAllowed(out.action, out.param), "param");
    f.Number("target_guid", out.targetGuid, 0, kU32Max, true);
    return f.Result(std::move(out));
}

std::optional<StockItemsCmd> ParseStockItems(json const& j, std::string& error)
{
    Fields f(j, error);
    StockItemsCmd out;
    f.Number("guid", out.guid);
    f.List("items", out.items, 1, kStockItems.size());
    f.Require(std::all_of(out.items.begin(), out.items.end(),
        [](auto item) { return Contains(kStockItems, item); }), "items");
    f.Number("count", out.count, 1, 20, true);
    return f.Result(std::move(out));
}

void RegisterGuildCommandTypes()
{
    for (auto name : kCommandTypes)
        RegisterExtensionCommand(name);
}

bool BotDoParamAllowed(std::string_view action, std::string_view param)
{
    if (action == "co" || action == "nc")
    {
        if (param.empty())
            return false;
        for (;;)
        {
            auto comma = param.find(',');
            auto entry = param.substr(0, comma);
            if (entry.size() < 2 || (entry.front() == '+' ? !Contains(kBotStrategyAdd, entry.substr(1))
                : entry.front() == '-' ? !Contains(kBotStrategyRemove, entry.substr(1)) : true))
                return false;
            if (comma == std::string_view::npos)
                return true;
            param.remove_prefix(comma + 1);
        }
    }
    if (action == "rti") return Contains(kRtiIcons, param);
    if (action == "formation") return Contains(kFormations, param);
    if (action == "cast") return Contains(kBotSpells, param);
    if (action == "do") return Contains(kBotActions, param);
    if (action == "use")
        return std::any_of(kStockItems.begin(), kStockItems.end(),
            [&](auto item) { return param == std::to_string(item); });
    return false;
}

std::uint8_t RaidDifficulty(std::uint32_t size, bool heroic)
{
    return static_cast<std::uint8_t>((size == 25 ? 1 : 0) + (heroic ? 2 : 0));
}

json MakeJobEvent(JobEvent const& e)
{
    json skipped = json::array();
    for (auto const& [guid, reason] : e.skipped)
        skipped.push_back({{"guid", guid}, {"reason", reason}});
    return {{"type", "job"}, {"id", e.id}, {"kind", e.kind}, {"state", e.state},
        {"reason", e.reason}, {"stage", e.stage}, {"guids", e.guids}, {"skipped", skipped},
        {"kills", e.kills}, {"wipes", e.wipes}, {"instance_id", e.instanceId},
        {"guid", e.guid}, {"level", e.level}, {"avg_ilvl", e.avgIlvl}};
}
}
