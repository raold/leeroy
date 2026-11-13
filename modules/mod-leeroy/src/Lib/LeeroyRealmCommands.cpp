// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyRealmCommands.h"
#include "LeeroyRealmContinuations.h"
#include "LeeroyProtocol.h"
#include <algorithm>
#include <charconv>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <limits>
#include <set>
#include <utility>

namespace Leeroy::Realm
{
namespace
{
using json = nlohmann::json;
constexpr auto kU32Max = std::numeric_limits<std::uint32_t>::max();
constexpr auto kU64Max = std::numeric_limits<std::uint64_t>::max();
constexpr auto kMaxTimestamp = static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max());
constexpr std::uint64_t kMaxWireUnixMs = kMaxFightStartedAtMs; // 9999-12-31, the mind's Unix-ms bound.
constexpr std::array<std::string_view, 18> kQueued = {
    "realm_move", "rpg_status", "leave_pvp", "rearm", "wg_join", "arena_queue", "duel", "craft", "equip",
    "set_profession", "transmog_set", "calendar_signup", "mail_send", "bank_deposit", "bank_withdraw",
    "bank_stock", "guild_grow", "arena_team"
};
constexpr std::array<std::string_view, 3> kArenaActions = {"create", "add", "drop"};
constexpr std::array<std::string_view, 5> kCalendarTypes = {"raid", "dungeon", "pvp", "meeting", "other"};
constexpr std::array<std::string_view, 4> kSignupStatuses = {"signed_up", "tentative", "declined", "accepted"};
constexpr std::array<std::string_view, 2> kItemSources = {"new", "bags"};
constexpr std::array<std::string_view, 2> kFirst = {"week", "ever"};

template <typename Range, typename T>
bool Contains(Range const& values, T const& value)
{
    return std::find(values.begin(), values.end(), value) != values.end();
}

bool Unsigned(json const& value, std::uint64_t low, std::uint64_t high)
{
    return value.is_number_unsigned() && value.get<std::uint64_t>() >= low && value.get<std::uint64_t>() <= high;
}

bool Identifier(std::string_view name)
{
    return !name.empty() && name.size() <= 64 && std::all_of(name.begin(), name.end(), [](unsigned char c) {
        return (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_';
    });
}

class Fields
{
public:
    Fields(json const& value, std::string& error) : _value(value), _error(error) { _error.clear(); }
    bool Failed() const { return !_error.empty(); }
    void Require(bool valid, char const* key)
    {
        if (!Failed() && !valid) _error = std::string(Err::BadField) + key;
    }
    json const* Find(char const* key) const
    {
        if (!_value.is_object()) return nullptr;
        auto it = _value.find(key);
        return it == _value.end() ? nullptr : &*it;
    }
    template <typename T>
    void Number(char const* key, T& out, std::uint64_t low = 1, std::uint64_t high = kU32Max, bool optional = false)
    {
        if (Failed()) return;
        auto v = Find(key);
        if (!v && optional) return;
        Require(v && Unsigned(*v, low, high) && v->get<std::uint64_t>() <= std::numeric_limits<T>::max(), key);
        if (!Failed()) out = static_cast<T>(v->get<std::uint64_t>());
    }
    void DungeonId(std::int32_t& out)
    {
        if (Failed()) return;
        auto v = Find("dungeon_id");
        if (!v) return;
        bool valid = v->is_number_unsigned() ? v->get<std::uint64_t>() <= std::numeric_limits<std::int32_t>::max()
            : v->is_number_integer() && v->get<std::int64_t>() >= -1
                && v->get<std::int64_t>() <= std::numeric_limits<std::int32_t>::max();
        Require(valid, "dungeon_id");
        if (!Failed()) out = v->get<std::int32_t>();
    }
    void Difficulty(std::int32_t& out)
    {
        if (Failed()) return;
        auto v = Find("difficulty");
        bool const valid = v && (v->is_number_unsigned() ? v->get<std::uint64_t>() <= 3
            : v->is_number_integer() && v->get<std::int64_t>() >= -1 && v->get<std::int64_t>() <= 3);
        Require(valid, "difficulty");
        if (!Failed()) out = v->get<std::int32_t>();
    }
    void Boolean(char const* key, bool& out, bool optional = true)
    {
        if (Failed()) return;
        auto v = Find(key);
        if (!v && optional) return;
        Require(v && v->is_boolean(), key);
        if (!Failed()) out = v->get<bool>();
    }
    void String(char const* key, std::string& out, std::size_t low, std::size_t high, bool optional = false)
    {
        if (Failed()) return;
        auto v = Find(key);
        if (!v && optional) return;
        Require(v && v->is_string(), key);
        if (Failed()) return;
        auto const& text = v->get_ref<std::string const&>();
        Require(text.size() >= low && text.size() <= high && text.find('\0') == std::string::npos, key);
        if (!Failed()) out = text;
    }
    template <typename Range>
    void Enum(char const* key, std::string& out, Range const& choices)
    {
        String(key, out, 1, 64);
        Require(Contains(choices, out), key);
    }
    void List(char const* key, std::vector<std::uint32_t>& out, std::size_t low, std::size_t high, bool optional = false)
    {
        if (Failed()) return;
        auto v = Find(key);
        if (!v && optional) return;
        Require(v && v->is_array() && v->size() >= low && v->size() <= high, key);
        if (Failed()) return;
        std::set<std::uint32_t> seen;
        for (auto const& item : *v)
        {
            Require(Unsigned(item, 1, kU32Max), key);
            if (Failed()) return;
            auto number = item.get<std::uint32_t>();
            Require(seen.insert(number).second, key);
            if (Failed()) return;
            out.push_back(number);
        }
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

void RegisterRealmCommandTypes()
{
    for (auto name : kCommandTypes) RegisterExtensionCommand(name);
}
bool IsQueued(std::string_view type) { return Contains(kQueued, type); }
bool ReturnsValue(std::string_view type) { return type == "arena_team" || type == "item_count" || type == "calendar_post"; }
bool IsPrimaryProfession(std::uint32_t skill)
{
    return Contains(kProfessionSkills, skill) && skill != 185 && skill != 356 && skill != 129;
}

std::optional<ActorsCmd> ParseActors(json const& j, std::string& error)
{
    Fields f(j, error); ActorsCmd out;
    f.List("guids", out.guids, 0, 16);
    f.List("gankers", out.gankers, 0, 16, true);
    f.Require(std::all_of(out.gankers.begin(), out.gankers.end(), [&](auto g) { return Contains(out.guids, g); }), "gankers");
    f.List("guilds", out.guilds, 0, 3, true);
    return f.Result(std::move(out));
}
std::optional<SayCmd> ParseSay(json const& j, std::string& error)
{
    Fields f(j, error); SayCmd out;
    f.Number("guid", out.guid); f.Enum("channel", out.channel, kSayChannels);
    f.String("text", out.text, 1, 255);
    f.Number("to_guid", out.toGuid, out.channel == "whisper" ? 1 : 0, kU32Max, out.channel != "whisper");
    f.Require(out.channel == "whisper" || out.toGuid == 0, "to_guid");
    return f.Result(std::move(out));
}
std::optional<WorldSayCmd> ParseWorldSay(json const& j, std::string& error)
{
    auto speech = ParseSay(j,error);
    if (!speech) return std::nullopt;
    Fields f(j,error); WorldSayCmd out{*speech,0};
    f.Number("guild_id",out.guild);
    f.Require(out.speech.channel == "trade" || out.speech.channel == "general", "channel");
    return f.Result(std::move(out));
}
std::optional<EmoteCmd> ParseEmote(json const& j, std::string& error)
{
    Fields f(j, error); EmoteCmd out;
    f.Number("guid", out.guid); f.String("emote", out.emote, 1, 64); f.Number("target_guid", out.targetGuid, 0, kU32Max, true);
    f.Boolean("social", out.social);
    f.String("seat_id", out.seatId, 0, 128, true);
    f.Number("lease_seconds", out.leaseSeconds, 0, 900, true);
    f.Require(out.seatId.empty() || (out.social && out.emote == "stand"), "seat_id");
    f.Require(out.leaseSeconds == 0 || (out.social && out.emote == "sit"), "lease_seconds");
    f.Require(!out.social || out.emote != "stand" || !out.seatId.empty(), "seat_id");
    return f.Result(std::move(out));
}
std::optional<MoveCmd> ParseMove(json const& j, std::string& error)
{
    Fields f(j, error); MoveCmd out;
    f.Number("guid", out.guid); f.String("place", out.place, 1, 64); f.Require(Identifier(out.place), "place");
    f.Number("slot", out.slot, 0, 119, true);
    f.Boolean("social", out.social);
    return f.Result(std::move(out));
}
std::optional<VisitCmd> ParseVisit(json const& j, std::string& error)
{
    Fields f(j, error); VisitCmd out;
    f.String("place", out.place, 1, 64); f.Require(Identifier(out.place), "place");
    return f.Result(std::move(out));
}
std::optional<RpgStatusCmd> ParseRpgStatus(json const& j, std::string& error)
{
    Fields f(j, error); RpgStatusCmd out;
    f.Number("guid", out.guid); f.Enum("status", out.status, kRpgStatuses); f.Number("quest_id", out.questId, 0, kU32Max, true);
    f.Require(!out.questId || out.status == "do quest", "quest_id");
    return f.Result(std::move(out));
}
std::optional<LeavePvpCmd> ParseLeavePvp(json const& j, std::string& error)
{
    Fields f(j, error); LeavePvpCmd out;
    f.List("guids", out.guids, 1, 150);
    f.String("pvp_owner", out.pvpOwner, 0, 49, true);
    f.Require(out.pvpOwner.empty() || ContinuationLedger::ValidToken(out.pvpOwner), "pvp_owner");
    return f.Result(std::move(out));
}
std::optional<GetRealmPvpCmd> ParseGetRealmPvp(json const& j, std::string& error)
{
    Fields f(j, error); GetRealmPvpCmd out;
    f.String("pvp_owner", out.pvpOwner, 0, 49, true);
    f.Require(out.pvpOwner.empty() || ContinuationLedger::ValidToken(out.pvpOwner), "pvp_owner");
    f.String("origin_id", out.originId, 0, 128, true);
    f.Require(out.pvpOwner.empty() != out.originId.empty(), "owner_selector");
    return f.Result(std::move(out));
}
std::optional<RearmCmd> ParseRearm(json const& j, std::string& error)
{
    Fields f(j, error); RearmCmd out; f.List("guids", out.guids, 1, 150); return f.Result(std::move(out));
}
std::optional<RealmForgetCmd> ParseRealmForget(json const& j, std::string& error)
{
    Fields f(j, error); RealmForgetCmd out;
    f.String("origin_id", out.originId, 1, 128);
    f.Require(!out.originId.empty() && std::all_of(out.originId.begin(), out.originId.end(), [](unsigned char c) {
        return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')
            || c == '_' || c == '-';
    }), "origin_id");
    f.Require(!j.contains("id") || !j.at("id").is_string() || j.at("id").get<std::string>() != out.originId, "origin_id");
    return f.Result(std::move(out));
}
std::optional<WgJoinCmd> ParseWgJoin(json const& j, std::string& error)
{
    Fields f(j, error); WgJoinCmd out; f.List("guids", out.guids, 1, 40); return f.Result(std::move(out));
}
std::optional<ArenaTeamCmd> ParseArenaTeam(json const& j, std::string& error)
{
    Fields f(j, error); ArenaTeamCmd out; f.Enum("action", out.action, kArenaActions);
    if (out.action == "create")
    {
        f.Number("captain_guid", out.captainGuid); f.Number("arena_type", out.arenaType, 2, 5);
        f.Require(out.arenaType == 2 || out.arenaType == 3 || out.arenaType == 5, "arena_type");
        f.String("name", out.name, 1, 24); f.Require(!f.Find("team_id"), "team_id"); f.Require(!f.Find("guid"), "guid");
    }
    else
    {
        f.Number("team_id", out.teamId); f.Number("guid", out.guid);
        f.Require(!f.Find("captain_guid"), "captain_guid"); f.Require(!f.Find("arena_type"), "arena_type"); f.Require(!f.Find("name"), "name");
    }
    return f.Result(std::move(out));
}
std::optional<ArenaQueueCmd> ParseArenaQueue(json const& j, std::string& error)
{
    Fields f(j, error); ArenaQueueCmd out; f.Number("team_id", out.teamId); f.Number("captain_guid", out.captainGuid);
    return f.Result(std::move(out));
}
std::optional<DuelCmd> ParseDuel(json const& j, std::string& error)
{
    Fields f(j, error); DuelCmd out; f.Number("guid", out.guid); f.Number("target_guid", out.targetGuid);
    f.Require(out.guid != out.targetGuid, "target_guid"); return f.Result(std::move(out));
}
std::optional<CraftCmd> ParseCraft(json const& j, std::string& error)
{
    Fields f(j, error); CraftCmd out; f.Number("guid", out.guid); f.Number("spell_id", out.spellId); f.Number("count", out.count, 1, 5, true);
    return f.Result(std::move(out));
}
std::optional<ItemCountCmd> ParseItemCount(json const& j, std::string& error)
{
    Fields f(j, error); ItemCountCmd out; f.Number("guid", out.guid); f.List("item_ids", out.itemIds, 1, 20);
    return f.Result(std::move(out));
}
std::optional<EquipCmd> ParseEquip(json const& j, std::string& error)
{
    Fields f(j, error); EquipCmd out; f.Number("guid", out.guid); f.Number("item_id", out.itemId, kGuildTabard, kGuildTabard);
    return f.Result(std::move(out));
}
std::optional<SetProfessionCmd> ParseSetProfession(json const& j, std::string& error)
{
    Fields f(j, error); SetProfessionCmd out; f.Number("guid", out.guid); f.Number("skill_id", out.skillId);
    f.Require(Contains(kProfessionSkills, out.skillId), "skill_id"); return f.Result(std::move(out));
}
std::optional<TransmogSetCmd> ParseTransmogSet(json const& j, std::string& error)
{
    Fields f(j, error); TransmogSetCmd out; f.Number("guid", out.guid); f.Number("slot", out.slot, 0, 18); f.Number("entry", out.entry, 0);
    return f.Result(std::move(out));
}
std::optional<CalendarPostCmd> ParseCalendarPost(json const& j, std::string& error)
{
    Fields f(j, error); CalendarPostCmd out; f.Number("creator_guid", out.creatorGuid);
    f.String("title", out.title, 1, 64); f.String("description", out.description, 0, 255, true);
    f.Enum("event_type", out.eventType, kCalendarTypes); f.Number("at", out.at, 1, kMaxTimestamp); f.DungeonId(out.dungeonId);
    f.Boolean("guild_event", out.guildEvent); f.Boolean("invite_human", out.inviteHuman);
    return f.Result(std::move(out));
}
std::optional<CalendarSignupCmd> ParseCalendarSignup(json const& j, std::string& error)
{
    Fields f(j, error); CalendarSignupCmd out; f.Number("event_id", out.eventId, 1, kU64Max); f.Number("guid", out.guid);
    f.Enum("status", out.status, kSignupStatuses); return f.Result(std::move(out));
}
std::optional<CalendarUpdateCmd> ParseCalendarUpdate(json const& j, std::string& error)
{
    Fields f(j, error); CalendarUpdateCmd out; f.Number("event_id", out.eventId, 1, kU64Max);
    f.String("title", out.title, 0, 64, true); f.String("description", out.description, 0, 255, true);
    f.Number("at", out.at, 0, kMaxTimestamp, true); return f.Result(std::move(out));
}
std::optional<MailSendCmd> ParseMailSend(json const& j, std::string& error)
{
    Fields f(j, error); MailSendCmd out; f.Number("from_guid", out.fromGuid); f.String("subject", out.subject, 1, 64);
    f.String("body", out.body, 0, 512, true);
    if (!f.Failed())
        if (auto items = f.Find("items"))
        {
            f.Require(items->is_array() && items->size() <= 4, "items");
            if (!f.Failed())
            {
                for (auto const& item : *items)
                {
                    std::string itemError; Fields fields(item, itemError); MailItem parsed;
                    fields.Number("item_id", parsed.itemId); fields.Number("count", parsed.count, 1, 20);
                    fields.Enum("from", parsed.from, kItemSources);
                    f.Require(!fields.Failed(), "items");
                    if (f.Failed()) break;
                    out.items.push_back(std::move(parsed));
                }
            }
        }
    f.Number("money", out.money, 0, 1000000, true); f.Number("delay_s", out.delayS, 0, 3600, true);
    return f.Result(std::move(out));
}
std::optional<BankTabBuyCmd> ParseBankTabBuy(json const& j, std::string& error)
{
    Fields f(j, error); BankTabBuyCmd out; f.Number("officer_guid", out.officerGuid); f.Number("tab", out.tab, 0, 5);
    f.String("name", out.name, 1, 16); f.String("icon", out.icon, 0, 32); return f.Result(std::move(out));
}
std::optional<BankMoneyCmd> ParseBankMoney(json const& j, std::string& error)
{
    Fields f(j, error); BankMoneyCmd out; f.Number("amount", out.amount, 1, 60000); return f.Result(std::move(out));
}
std::optional<BankDepositCmd> ParseBankDeposit(json const& j, std::string& error)
{
    Fields f(j, error); BankDepositCmd out; f.Number("guid", out.guid); f.Number("item_id", out.itemId);
    f.Number("count", out.count, 0, kMaxItemCount); f.Number("tab", out.tab, 0, 5); return f.Result(std::move(out));
}
std::optional<BankWithdrawCmd> ParseBankWithdraw(json const& j, std::string& error)
{
    Fields f(j, error); BankWithdrawCmd out; f.Number("guid", out.guid); f.Number("item_id", out.itemId);
    f.Number("count", out.count, 1, kMaxItemCount); f.Number("tab", out.tab, 0, 5); return f.Result(std::move(out));
}
std::optional<BankStockCmd> ParseBankStock(json const& j, std::string& error)
{
    Fields f(j, error); BankStockCmd out; f.Number("officer_guid", out.officerGuid); f.Number("tab", out.tab, 0, 5);
    f.Number("item_id", out.itemId); f.Number("count", out.count, 1, kMaxItemCount); return f.Result(std::move(out));
}
std::optional<GrantAchievementCmd> ParseGrantAchievement(json const& j, std::string& error)
{
    Fields f(j, error); GrantAchievementCmd out; f.Number("guid", out.guid); f.Number("achievement_id", out.achievementId);
    f.Require(Contains(kGrantable, out.achievementId), "achievement_id"); return f.Result(std::move(out));
}
std::optional<GuildRenameCmd> ParseGuildRename(json const& j, std::string& error)
{
    Fields f(j, error); GuildRenameCmd out; f.Number("guild_id", out.guildId); f.String("name", out.name, 1, 24);
    return f.Result(std::move(out));
}
std::optional<GuildGrowCmd> ParseGuildGrow(json const& j, std::string& error)
{
    Fields f(j, error); GuildGrowCmd out; f.Number("guild_id", out.guildId); f.List("guids", out.guids, 1, 30);
    return f.Result(std::move(out));
}
std::optional<GuildAdmitCmd> ParseGuildAdmit(json const& j, std::string& error)
{
    Fields f(j, error); GuildAdmitCmd out; f.Number("guid", out.guid); f.Number("rank", out.rank, 2, 9, true);
    return f.Result(std::move(out));
}
std::optional<GuildNoteCmd> ParseGuildNote(json const& j, std::string& error)
{
    Fields f(j, error); GuildNoteCmd out; f.Number("guid", out.guid); f.String("officer_note", out.officerNote, 0, 31);
    return f.Result(std::move(out));
}
std::optional<GuildReadmitCmd> ParseGuildReadmit(json const& j, std::string& error)
{
    Fields f(j, error); GuildReadmitCmd out;
    f.String("id", out.id, 1, 128);
    f.Number("guid", out.guid);
    f.Number("kick_ts", out.kickTs, 1, kMaxWireUnixMs);
    f.Number("rank", out.rank, 2, 9);
    // A consent-bound mutation: its complete frozen schema includes the envelope.
    auto const version = f.Find("v");
    f.Require(version && version->is_number_integer() && *version == 1, "v");
    auto const type = f.Find("type");
    f.Require(type && type->is_string() && *type == "guild_readmit", "type");
    std::uint64_t timestamp = 0;
    f.Number("ts", timestamp, 0, kMaxTimestamp);
    constexpr std::array<std::string_view, 7> keys = {"v", "type", "ts", "id", "guid", "kick_ts", "rank"};
    if (j.is_object()) for (auto const& [key, value] : j.items())
    {
        (void)value;
        f.Require(Contains(keys, key), key.c_str());
    }
    return f.Result(std::move(out));
}
std::optional<AnnounceCmd> ParseAnnounce(json const& j, std::string& error)
{
    Fields f(j, error); AnnounceCmd out;
    f.String("id", out.id, 1, 128);
    f.String("kill_id", out.killId, 34, 53); f.Require(ValidKillId(out.killId), "kill_id");
    f.Number("native_epoch", out.nativeEpoch, 1, kU64Max);
    f.Number("guild_id", out.guildId); f.Number("human_guid", out.humanGuid);
    f.Number("map", out.map, 0); f.Number("instance_id", out.instanceId);
    f.Number("boss_id", out.bossId, 0);
    f.Difficulty(out.difficulty); f.Number("size", out.size, 10, 25);
    f.Require(out.size == 10 || out.size == 25, "size");
    f.Boolean("heroic", out.heroic, false);
    f.Require(ValidRaidMode(out.difficulty, out.size, out.heroic), "heroic");
    f.String("boss_name", out.bossName, 1, 48);
    f.Number("attempts", out.attempts, 0, 99); f.Number("fight_s", out.fightS, 0, 3600);
    f.Enum("first", out.first, kFirst);
    // Announce's complete frozen schema includes its ordinary v1 envelope.
    auto const version = f.Find("v");
    f.Require(version && version->is_number_integer() && *version == 1, "v");
    auto const type = f.Find("type");
    f.Require(type && type->is_string() && *type == "announce", "type");
    std::uint64_t timestamp = 0;
    f.Number("ts", timestamp, 0, kMaxTimestamp);
    constexpr std::array<std::string_view, 18> keys = {"v", "type", "ts", "id", "kill_id", "native_epoch",
        "guild_id", "human_guid", "map", "instance_id", "boss_id", "difficulty", "size", "heroic",
        "boss_name", "attempts", "fight_s", "first"};
    if (j.is_object()) for (auto const& [key, value] : j.items())
    {
        (void)value;
        f.Require(Contains(keys, key), key.c_str());
    }
    return f.Result(std::move(out));
}

namespace
{
std::string_view Trim(std::string_view text)
{
    while (!text.empty() && (text.front() == ' ' || text.front() == '\t' || text.front() == '\r')) text.remove_prefix(1);
    while (!text.empty() && (text.back() == ' ' || text.back() == '\t' || text.back() == '\r')) text.remove_suffix(1);
    return text;
}
std::vector<std::string_view> Columns(std::string_view line)
{
    std::vector<std::string_view> fields;
    for (;;)
    {
        auto comma = line.find(','); fields.push_back(Trim(line.substr(0, comma)));
        if (comma == std::string_view::npos) break;
        line.remove_prefix(comma + 1);
    }
    return fields;
}
bool CsvInteger(std::string_view text, std::uint32_t& out, std::uint32_t low, std::uint32_t high)
{
    if (text.empty()) return false;
    auto parsed = std::from_chars(text.data(), text.data() + text.size(), out);
    return parsed.ec == std::errc{} && parsed.ptr == text.data() + text.size() && out >= low && out <= high;
}
bool CsvFloat(std::string_view text, float& out, float low, float high)
{
    if (text.empty()) return false;
    auto parsed = std::from_chars(text.data(), text.data() + text.size(), out);
    return parsed.ec == std::errc{} && parsed.ptr == text.data() + text.size() && std::isfinite(out) && out >= low && out <= high;
}
template <typename Row, typename ParseRow>
std::optional<std::vector<Row>> Csv(std::string_view text, std::string_view header, std::string& error, ParseRow parse)
{
    error.clear(); std::vector<Row> rows; std::size_t lineNo = 0; bool seenHeader = false;
    if (text.size() > kMaxCsvBytes) { error = "line 1: file too large"; return std::nullopt; }
    while (!text.empty())
    {
        ++lineNo;
        auto end = text.find('\n'); auto line = text.substr(0, end);
        text = end == std::string_view::npos ? std::string_view{} : text.substr(end + 1);
        auto fail = [&](std::string_view why) { error = "line " + std::to_string(lineNo) + ": " + std::string(why); };
        if (line.size() > kMaxCsvLineBytes) { fail("line too long"); return std::nullopt; }
        line = Trim(line);
        if (line.empty() || line.front() == '#') continue;
        if (line.find('\0') != std::string_view::npos || line.find('"') != std::string_view::npos)
        { fail("invalid characters"); return std::nullopt; }
        if (!seenHeader)
        {
            if (line != header) { fail("invalid header"); return std::nullopt; }
            seenHeader = true; continue;
        }
        if (rows.size() >= kMaxCsvRows) { fail("too many rows"); return std::nullopt; }
        Row row; std::string why;
        if (!parse(Columns(line), row, why)) { fail(why); return std::nullopt; }
        rows.push_back(std::move(row));
    }
    if (!seenHeader) { error = "line 1: missing header"; return std::nullopt; }
    return rows;
}
}

std::optional<std::vector<Place>> ParsePlaces(std::string_view csv, std::string& error)
{
    std::set<std::string> names;
    return Csv<Place>(csv, "name,map,zone,x,y,z,o,kind", error, [&](auto const& fields, Place& out, std::string& why) {
        if (fields.size() != 8) { why = "expected 8 columns"; return false; }
        if (!Identifier(fields[0]) || fields[0] == "near_human") { why = "invalid name"; return false; }
        out.name = fields[0];
        if (!names.insert(out.name).second) { why = "duplicate name"; return false; }
        if (!CsvInteger(fields[1], out.map, 0, 999)) { why = "invalid map"; return false; }
        if (!CsvInteger(fields[2], out.zone, 1, 65535)) { why = "invalid zone"; return false; }
        if (!CsvFloat(fields[3], out.x, -20000, 20000) || !CsvFloat(fields[4], out.y, -20000, 20000)
            || !CsvFloat(fields[5], out.z, -20000, 20000)) { why = "invalid coordinates"; return false; }
        if (!CsvFloat(fields[6], out.o, 0, 6.283185307179586f)) { why = "invalid orientation"; return false; }
        if (!Contains(kPlaceKinds, fields[7])) { why = "invalid kind"; return false; }
        out.kind = fields[7]; return true;
    });
}
std::optional<std::vector<ItemRule>> ParseItems(std::string_view csv, std::string& error)
{
    std::set<std::pair<std::uint32_t, std::string>> seen;
    return Csv<ItemRule>(csv, "item_id,use,max_count", error, [&](auto const& fields, ItemRule& out, std::string& why) {
        if (fields.size() != 3) { why = "expected 3 columns"; return false; }
        if (!CsvInteger(fields[0], out.itemId, 1, kU32Max)) { why = "invalid item_id"; return false; }
        if (!Contains(kItemUses, fields[1])) { why = "invalid use"; return false; }
        out.use = fields[1];
        if (!seen.emplace(out.itemId, out.use).second) { why = "duplicate item/use"; return false; }
        if (!CsvInteger(fields[2], out.maxCount, 1, kMaxItemCount)) { why = "invalid max_count"; return false; }
        return true;
    });
}
std::string ResolveCsvPath(std::string_view moduleConfigDir, std::string_view configured)
{
    if (configured.empty())
        return {};
    std::filesystem::path const path(configured);
    if (path.has_root_path())
        return std::string(configured);
    return (std::filesystem::path(moduleConfigDir) / path).string();
}
std::optional<std::string> ReadCsvFile(std::string const& path)
{
    std::error_code ec;
    if (path.empty() || !std::filesystem::is_regular_file(path, ec))
        return std::nullopt;
    std::ifstream stream(path, std::ios::binary | std::ios::ate);
    if (!stream)
        return std::nullopt;
    auto const size = stream.tellg();
    if (size < 0 || size > static_cast<std::streamoff>(kMaxCsvBytes))
        return std::nullopt;
    std::string text(static_cast<std::size_t>(size), '\0');
    stream.seekg(0);
    if (!text.empty() && !stream.read(text.data(), static_cast<std::streamsize>(text.size())))
        return std::nullopt;
    return text;
}

namespace
{
json KillBody(std::string_view type, Kill const& e)
{
    return {{"type", type}, {"kill_id", e.killId}, {"native_epoch", e.nativeEpoch},
        {"guild_id", e.guildId}, {"human_guid", e.humanGuid}, {"killed_at_ms", e.killedAtMs},
        {"verified_at_ms", e.verifiedAtMs}, {"fight_started_at_ms", e.fightStartedAtMs}, {"fight_ms", e.fightMs},
        {"attempts", e.attempts}, {"map", e.map}, {"instance_id", e.instanceId},
        {"boss_id", e.bossId}, {"difficulty", e.difficulty}, {"size", e.size}, {"heroic", e.heroic},
        {"human_present", e.humanPresent}, {"cast_members", e.castMembers}};
}
}

json MakeRealmKill(Kill const& e) { return KillBody("realm_kill", e); }
json MakeRealmDungeonKill(Kill const& e) { return KillBody("realm_dungeon_kill", e); }

std::optional<AnnouncementMetrics> ValidatedAnnouncementMetrics(AnnounceCmd const& request, Kill const& proof)
{
    if (!ValidKillProof(proof) || !ValidRaidMode(proof.difficulty, proof.size, proof.heroic)
        || request.killId != proof.killId
        || static_cast<KillIdentity const&>(request) != static_cast<KillIdentity const&>(proof)) return std::nullopt;
    AnnouncementMetrics metrics;
    if (request.attempts)
    {
        if (!proof.attempts || request.attempts != proof.attempts) return std::nullopt;
        metrics.attempts = proof.attempts;
    }
    if (request.fightS)
    {
        if (!proof.humanPresent || !proof.fightStartedAtMs || proof.fightMs < 0) return std::nullopt;
        auto const seconds = static_cast<std::uint32_t>(proof.fightMs / 1000);
        if (seconds != request.fightS) return std::nullopt;
        metrics.fightS = seconds;
    }
    return metrics;
}

json MakeRealmJob(RealmJob const& e)
{
    json body = {{"type", "realm_job"}, {"id", e.id}, {"kind", e.kind}, {"state", e.ok ? "done" : "failed"},
        {"reason", e.reason}, {"guid", e.guid}, {"value", e.value}};
    if (!e.pvpOwner.empty()) body["pvp_owner"] = e.pvpOwner;
    if (!e.ok && e.changed.has_value()) body["changed"] = *e.changed;
    if (!e.skipped.empty()) body["skipped"] = e.skipped;
    if (!e.ok && !e.released.empty()) body["released"] = e.released;
    if (!e.ok && e.readbackGuid) { body["readback_guid"] = e.readbackGuid; body["check"] = e.check; }
    return body;
}
json MakeRealmForgotten(std::string const& id, std::string const& originId, std::string const& outcome, std::size_t members)
{
    return {{"type", "realm_forgotten"}, {"id", id}, {"origin_id", originId}, {"outcome", outcome}, {"members", members}};
}
json MakeRealmPvp(std::string_view id, ContinuationOwner const& owner)
{
    return {{"type", "realm_pvp"}, {"id", id}, {"pvp_owner", owner.token}, {"origin_id", owner.originId},
        {"kind", PvpKindName(owner.kind)}, {"origin_epoch", owner.originEpoch}, {"revision", owner.revision},
        {"guild", owner.guild}, {"human", owner.human}, {"members", owner.members},
        {"stage", PvpStageName(owner.stage)}, {"application_fenced", owner.applicationFenced},
        {"reason", owner.reason}, {"cleanup_id", owner.cleanupId}};
}
json MakeChannelChat(std::string_view channel, std::uint32_t fromGuid, std::string_view fromName,
    std::uint32_t zone, std::string_view text, bool viaBridge)
{
    return {{"type", "channel_chat"}, {"channel", channel}, {"from_guid", fromGuid}, {"from_name", fromName},
        {"zone", zone}, {"text", text}, {"via_bridge", viaBridge}};
}
json MakePvpKill(std::uint32_t killer, std::uint32_t victim, std::uint32_t map, std::uint32_t zone)
{ return {{"type", "pvp_kill"}, {"killer", killer}, {"victim", victim}, {"map", map}, {"zone", zone}}; }
json MakeDuelEnd(std::uint32_t winner, std::uint32_t loser, std::string_view result,
    std::uint32_t map, std::uint32_t zone, bool city)
{
    return {{"type", "duel_end"}, {"winner", winner}, {"loser", loser}, {"result", result},
        {"map", map}, {"zone", zone}, {"city", city}};
}
json MakeRealmAchievement(std::uint32_t guid, std::uint32_t achievementId, std::string_view name,
    std::uint32_t category, std::uint32_t flags, bool realmFirst)
{
    return {{"type", "realm_achievement"}, {"guid", guid}, {"achievement_id", achievementId}, {"name", name},
        {"category", category}, {"flags", flags}, {"realm_first", realmFirst}};
}
json MakePvpFlag(std::uint32_t guid, bool flagged)
{ return {{"type", "pvp_flag"}, {"guid", guid}, {"flagged", flagged}}; }
json MakeWg(std::string_view state, std::string_view defender, std::uint32_t seconds, double updateMs)
{ return {{"type", "wg"}, {"state", state}, {"defender", defender}, {"seconds", seconds}, {"update_ms", updateMs}}; }
json MakeArenaQueue(std::uint32_t teamId, std::uint32_t arenaType, bool rated)
{ return {{"type", "arena_queue"}, {"team_id", teamId}, {"arena_type", arenaType}, {"rated", rated}}; }
json MakeArenaEnd(std::uint32_t arenaType, std::uint32_t winnerTeam, std::uint32_t loserTeam,
    std::int32_t winnerChange, std::int32_t loserChange, std::vector<std::uint32_t> const& members)
{
    return {{"type", "arena_end"}, {"arena_type", arenaType}, {"winner_team", winnerTeam}, {"loser_team", loserTeam},
        {"winner_change", winnerChange}, {"loser_change", loserChange}, {"members", members}};
}
json MakeGameEvent(std::uint32_t eventId, std::string_view state)
{ return {{"type", "game_event"}, {"event_id", eventId}, {"state", state}}; }
json MakeCalendarRsvp(std::uint64_t eventId, std::string_view status)
{ return {{"type", "calendar_rsvp"}, {"event_id", eventId}, {"status", status}}; }
json MakeMailTaken(std::uint32_t itemGuid, std::uint32_t itemId, std::uint32_t count)
{ return {{"type", "mail_taken"}, {"item_guid", itemGuid}, {"item_id", itemId}, {"count", count}}; }
json MakeVisitState(std::string const& id, std::string const& place,
                    std::uint32_t guild, std::uint32_t human, bool ready, bool nearby)
{ return {{"type", "realm_visit_state"}, {"id", id}, {"place", place}, {"guild_id", guild},
          {"human_guid", human}, {"ready", ready}, {"near", ready && nearby}}; }
json MakeGuard(std::uint32_t guid, std::string_view action, std::uint32_t rank, std::string_view reason)
{ return {{"type", "guard"}, {"guid", guid}, {"action", action}, {"rank", rank}, {"reason", reason}}; }

std::string FormatAnnouncement(std::string_view guild, std::string_view boss, std::uint32_t size,
    bool heroic, std::uint32_t attempts, std::uint32_t fightS, bool ever)
{
    std::string text = std::string(guild) + " killed " + std::string(boss) + " (" + std::to_string(size)
        + (heroic ? " heroic" : "") + ")" + (ever ? " for the first time" : " this week");
    if (attempts == 1) text += ", on the first attempt";
    else if (attempts) text += ", " + std::to_string(attempts) + " attempts in";
    if (fightS) text += ", in " + std::to_string(fightS / 60) + ":" + (fightS % 60 < 10 ? "0" : "") + std::to_string(fightS % 60);
    return text + ".";
}
}
