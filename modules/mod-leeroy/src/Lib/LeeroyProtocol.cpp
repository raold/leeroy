// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyProtocol.h"
#include <array>
#include <chrono>
#include <limits>
#include <mutex>
#include <set>
#include <utility>

namespace Leeroy
{
namespace
{
using json = nlohmann::json;

struct ChannelEntry
{
    Channel channel;
    std::string_view name;
};

constexpr std::array<ChannelEntry, 9> kChannels = {{
    {Channel::Guild, "guild"}, {Channel::Officer, "officer"}, {Channel::Party, "party"}, {Channel::Raid, "raid"},
    {Channel::RaidWarning, "raid_warning"}, {Channel::Say, "say"}, {Channel::Yell, "yell"},
    {Channel::Whisper, "whisper"}, {Channel::Emote, "emote"},
}};

struct CommandName
{
    CommandType type;
    std::string_view name;
};

constexpr std::array<CommandName, 9> kCommandNames = {{
    {CommandType::Ping, "ping"}, {CommandType::GetCast, "get_cast"}, {CommandType::Say, "say"},
    {CommandType::Emote, "emote"}, {CommandType::HumanSay, "human_say"}, {CommandType::SetRank, "set_rank"},
    {CommandType::SetMotd, "set_motd"}, {CommandType::AddFriend, "add_friend"}, {CommandType::Afk, "afk"},
}};

std::mutex& ExtensionMutex()
{
    static std::mutex mutex;
    return mutex;
}

std::set<std::string, std::less<>>& ExtensionTypes()
{
    static std::set<std::string, std::less<>> types;
    return types;
}

std::optional<CommandType> ParseCommandType(std::string_view name)
{
    for (CommandName const& entry : kCommandNames)
        if (entry.name == name)
            return entry.type;
    return std::nullopt;
}

bool IsSayChannel(Channel c)
{
    return c != Channel::RaidWarning && c != Channel::Emote;
}

std::string_view BossStateName(BossState s)
{
    switch (s)
    {
        case BossState::Engaged: return "engaged";
        case BossState::Killed: return "killed";
        case BossState::Wiped: return "wiped";
    }
    return "wiped";
}

std::string_view LootMethodName(LootMethod m)
{
    switch (m)
    {
        case LootMethod::Need: return "need";
        case LootMethod::Greed: return "greed";
        case LootMethod::Master: return "master";
    }
    return "need";
}

std::string_view BgWinnerName(BgWinner w)
{
    switch (w)
    {
        case BgWinner::Horde: return "horde";
        case BgWinner::Alliance: return "alliance";
        case BgWinner::None: return "none";
    }
    return "none";
}

json Envelope(std::int64_t ts, std::string_view type)
{
    json j = json::object();
    j["v"] = kProtocolVersion;
    j["type"] = type;
    j["ts"] = ts;
    return j;
}

// Reads fields of one decoded command and records the first bad one.
class FieldReader
{
public:
    FieldReader(json const& msg, std::string& error) : _msg(msg), _error(error) { }

    bool Failed() const { return !_error.empty(); }

    void Guid(char const* key, bool required, std::uint32_t& out)
    {
        if (Failed())
            return;
        json const* v = Find(key);
        if (!v)
            return required ? Fail(key) : void();
        if (!v->is_number_unsigned() || v->get<std::uint64_t>() > std::numeric_limits<std::uint32_t>::max())
            return Fail(key);
        out = v->get<std::uint32_t>();
        if (required && out == 0)
            Fail(key);
    }

    void Str(char const* key, bool required, bool nonEmpty, std::string& out)
    {
        if (Failed())
            return;
        json const* v = Find(key);
        if (!v)
            return required ? Fail(key) : void();
        if (!v->is_string())
            return Fail(key);
        out = v->get<std::string>();
        if (nonEmpty && out.empty())
            Fail(key);
    }

    void Chan(char const* key, Channel& out)
    {
        if (Failed())
            return;
        json const* v = Find(key);
        if (!v || !v->is_string())
            return Fail(key);
        std::optional<Channel> c = ParseChannel(v->get_ref<std::string const&>());
        if (!c || !IsSayChannel(*c))
            return Fail(key);
        out = *c;
    }

    void Rank(char const* key, std::uint8_t& out)
    {
        if (Failed())
            return;
        json const* v = Find(key);
        if (!v || !v->is_number_unsigned() || v->get<std::uint64_t>() > 9)
            return Fail(key);
        out = v->get<std::uint8_t>();
    }

    void Bool(char const* key, bool& out)
    {
        if (Failed())
            return;
        json const* v = Find(key);
        if (!v || !v->is_boolean())
            return Fail(key);
        out = v->get<bool>();
    }

private:
    json const* Find(char const* key) const
    {
        auto it = _msg.find(key);
        return it == _msg.end() ? nullptr : &*it;
    }

    void Fail(char const* key)
    {
        _error = std::string(Err::BadField) + key;
    }

    json const& _msg;
    std::string& _error;
};

void DecodeFields(json const& msg, Command& cmd, std::string& error)
{
    FieldReader r(msg, error);
    switch (cmd.type)
    {
        case CommandType::Say:
            r.Guid("guid", true, cmd.guid);
            r.Chan("channel", cmd.channel);
            r.Str("text", true, false, cmd.text);
            r.Guid("to_guid", cmd.channel == Channel::Whisper, cmd.toGuid);
            break;
        case CommandType::HumanSay:
            r.Chan("channel", cmd.channel);
            r.Str("text", true, false, cmd.text);
            r.Str("to_name", cmd.channel == Channel::Whisper, cmd.channel == Channel::Whisper, cmd.toName);
            break;
        case CommandType::Emote:
            r.Guid("guid", true, cmd.guid);
            r.Str("emote", true, true, cmd.emote);
            r.Guid("target_guid", false, cmd.targetGuid);
            break;
        case CommandType::SetRank:
            r.Guid("guid", true, cmd.guid);
            r.Rank("rank", cmd.rank);
            break;
        case CommandType::SetMotd:
            r.Str("text", true, false, cmd.text);
            break;
        case CommandType::AddFriend:
            r.Guid("guid", true, cmd.guid);
            r.Str("note", false, false, cmd.note);
            r.Str("message", false, false, cmd.message);
            break;
        case CommandType::Afk:
            r.Guid("guid", true, cmd.guid);
            r.Bool("on", cmd.on);
            break;
        case CommandType::Ping:
        case CommandType::GetCast:
        case CommandType::Extension:
            break;
    }
}
}

std::int64_t NowUnixMs()
{
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}

std::string_view ChannelName(Channel c)
{
    for (ChannelEntry const& entry : kChannels)
        if (entry.channel == c)
            return entry.name;
    return "say";
}

std::optional<Channel> ParseChannel(std::string_view name)
{
    for (ChannelEntry const& entry : kChannels)
        if (entry.name == name)
            return entry.channel;
    return std::nullopt;
}

bool RegisterExtensionCommand(std::string_view type)
{
    if (ParseCommandType(type))
        return false;
    std::lock_guard<std::mutex> lock(ExtensionMutex());
    return ExtensionTypes().emplace(type).second;
}

bool IsExtensionCommand(std::string_view type)
{
    std::lock_guard<std::mutex> lock(ExtensionMutex());
    return ExtensionTypes().find(type) != ExtensionTypes().end();
}

DecodeResult DecodeCommand(std::string_view line)
{
    DecodeResult result;
    json msg = json::parse(line.begin(), line.end(), nullptr, false);
    if (msg.is_discarded() || !msg.is_object())
    {
        result.error = std::string(Err::BadJson);
        return result;
    }

    auto idIt = msg.find("id");
    bool const hasId = idIt != msg.end() && idIt->is_string();
    if (hasId)
        result.id = idIt->get<std::string>();

    auto vIt = msg.find("v");
    if (vIt == msg.end() || !vIt->is_number_integer() || vIt->get<std::int64_t>() != kProtocolVersion)
    {
        result.error = std::string(Err::UnsupportedVersion);
        return result;
    }
    if (!hasId)
    {
        result.error = std::string(Err::MissingId);
        return result;
    }

    auto typeIt = msg.find("type");
    std::string type = typeIt != msg.end() && typeIt->is_string() ? typeIt->get<std::string>() : std::string();
    std::optional<CommandType> core = ParseCommandType(type);
    bool const extension = !core && IsExtensionCommand(type);
    if (!core && !extension)
    {
        result.error = std::string(Err::UnknownType) + type;
        return result;
    }

    Command cmd;
    cmd.id = result.id;
    if (extension)
    {
        cmd.type = CommandType::Extension;
        cmd.extType = std::move(type);
        cmd.raw = std::move(msg);
        result.command = std::move(cmd);
        return result;
    }

    cmd.type = *core;
    result.ackable = true;
    DecodeFields(msg, cmd, result.error);
    if (result.error.empty())
        result.command = std::move(cmd);
    return result;
}

std::optional<BossState> ClassifyBossTransition(std::uint8_t newState, std::uint8_t oldState)
{
    if (oldState == 5 || newState == oldState)
        return std::nullopt;
    if (newState == 1)
        return BossState::Engaged;
    if (newState == 3)
        return BossState::Killed;
    if (oldState == 1 && (newState == 0 || newState == 2))
        return BossState::Wiped;
    return std::nullopt;
}

std::string DumpLine(json const& j)
{
    return j.dump(-1, ' ', false, json::error_handler_t::replace);
}

std::string EncodeHello(std::int64_t ts, std::uint32_t guildId, std::uint32_t humanGuid)
{
    json j = Envelope(ts, "hello");
    j["server"] = "mod-leeroy";
    j["version"] = kModuleVersion;
    j["guild_id"] = guildId;
    j["human_guid"] = humanGuid;
    return DumpLine(j);
}

std::string EncodeLogin(std::int64_t ts, bool login, std::uint32_t guid, bool human)
{
    json j = Envelope(ts, login ? "login" : "logout");
    j["guid"] = guid;
    j["human"] = human;
    return DumpLine(j);
}

std::string EncodeChat(std::int64_t ts, ChatEvent const& e)
{
    json j = Envelope(ts, "chat");
    j["channel"] = ChannelName(e.channel);
    j["from_guid"] = e.fromGuid;
    j["from_name"] = e.fromName;
    j["to_guid"] = e.toGuid;
    j["text"] = e.text;
    j["map"] = e.map;
    j["zone"] = e.zone;
    j["via_bridge"] = e.viaBridge;
    return DumpLine(j);
}

std::string EncodeBoss(std::int64_t ts, BossEvent const& e)
{
    json j = Envelope(ts, "boss");
    j["map"] = e.map;
    j["instance_id"] = e.instanceId;
    j["boss_id"] = e.bossId;
    j["boss_name"] = e.bossName;
    j["state"] = BossStateName(e.state);
    j["members"] = e.members;
    return DumpLine(j);
}

std::string EncodeDeath(std::int64_t ts, std::uint32_t guid, std::uint32_t map, std::uint32_t killerEntry)
{
    json j = Envelope(ts, "death");
    j["guid"] = guid;
    j["map"] = map;
    j["killer_entry"] = killerEntry;
    return DumpLine(j);
}

std::string EncodeResurrect(std::int64_t ts, std::uint32_t guid)
{
    json j = Envelope(ts, "resurrect");
    j["guid"] = guid;
    return DumpLine(j);
}

std::string EncodeLootDrop(std::int64_t ts, LootDropEvent const& e)
{
    json items = json::array();
    for (LootItem const& item : e.items)
    {
        json i = json::object();
        i["item_id"] = item.itemId;
        i["name"] = item.name;
        i["quality"] = item.quality;
        i["index"] = item.index;
        items.push_back(std::move(i));
    }
    json j = Envelope(ts, "loot_drop");
    j["map"] = e.map;
    j["instance_id"] = e.instanceId;
    j["source_entry"] = e.sourceEntry;
    j["items"] = std::move(items);
    return DumpLine(j);
}

std::string EncodeLootWon(std::int64_t ts, std::uint32_t guid, std::uint32_t itemId, std::string_view name,
    LootMethod m)
{
    json j = Envelope(ts, "loot_won");
    j["guid"] = guid;
    j["item_id"] = itemId;
    j["name"] = name;
    j["method"] = LootMethodName(m);
    return DumpLine(j);
}

std::string EncodeAchievement(std::int64_t ts, std::uint32_t guid, std::uint32_t achievementId,
    std::string_view name)
{
    json j = Envelope(ts, "achievement");
    j["guid"] = guid;
    j["achievement_id"] = achievementId;
    j["name"] = name;
    return DumpLine(j);
}

std::string EncodeBgEnd(std::int64_t ts, std::uint32_t bgType, BgWinner winner,
    std::vector<std::uint32_t> const& members)
{
    json j = Envelope(ts, "bg_end");
    j["bg_type"] = bgType;
    j["winner"] = BgWinnerName(winner);
    j["members"] = members;
    return DumpLine(j);
}

std::string EncodeZone(std::int64_t ts, std::uint32_t guid, std::uint32_t zoneId, std::string_view zoneName)
{
    json j = Envelope(ts, "zone");
    j["guid"] = guid;
    j["zone_id"] = zoneId;
    j["zone_name"] = zoneName;
    return DumpLine(j);
}

std::string EncodeGroup(std::int64_t ts, std::uint32_t leaderGuid, bool raid,
    std::vector<std::uint32_t> const& members)
{
    json j = Envelope(ts, "group");
    j["leader_guid"] = leaderGuid;
    j["raid"] = raid;
    j["members"] = members;
    return DumpLine(j);
}

std::string EncodeDcStatus(std::int64_t ts, std::uint32_t tankGuid, bool enabled, bool paused,
    std::string_view payload)
{
    json j = Envelope(ts, "dc_status");
    j["tank_guid"] = tankGuid;
    j["enabled"] = enabled;
    j["paused"] = paused;
    j["payload"] = payload;
    return DumpLine(j);
}

std::string EncodeTextEmote(std::int64_t ts, std::uint32_t guid, std::string_view emote, std::uint32_t targetGuid,
    bool viaBridge)
{
    json j = Envelope(ts, "text_emote");
    j["guid"] = guid;
    j["emote"] = emote;
    j["target_guid"] = targetGuid;
    j["via_bridge"] = viaBridge;
    return DumpLine(j);
}

std::string EncodeAfk(std::int64_t ts, std::uint32_t guid, bool on)
{
    json j = Envelope(ts, "afk");
    j["guid"] = guid;
    j["on"] = on;
    return DumpLine(j);
}

std::string EncodeAck(std::int64_t ts, std::string_view id, bool ok, std::string_view error)
{
    json j = Envelope(ts, "ack");
    j["id"] = id;
    j["ok"] = ok;
    j["error"] = error;
    return DumpLine(j);
}

std::string EncodePong(std::int64_t ts, std::string_view id)
{
    json j = Envelope(ts, "pong");
    j["id"] = id;
    return DumpLine(j);
}

std::string EncodeError(std::int64_t ts, std::string_view message, std::string_view id)
{
    json j = Envelope(ts, "error");
    j["message"] = message;
    if (!id.empty())
        j["id"] = id;
    return DumpLine(j);
}

std::string EncodeEvent(std::int64_t ts, json body)
{
    if (!body.is_object())
        body = json::object();
    body["v"] = kProtocolVersion;
    body["ts"] = ts;
    return DumpLine(body);
}
}
