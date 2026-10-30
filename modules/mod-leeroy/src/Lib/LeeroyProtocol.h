// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_LEEROY_LEEROY_PROTOCOL_H
#define MOD_LEEROY_LEEROY_PROTOCOL_H

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>
#include <nlohmann/json.hpp>

namespace Leeroy
{
inline constexpr int kProtocolVersion = 1;
inline constexpr std::size_t kMaxLineBytes = 65536;
inline constexpr std::size_t kEventQueueCapacity = 10000;
inline constexpr std::size_t kInboxCapacity = 1000;
inline constexpr std::string_view kModuleVersion = "0.1.0";

std::int64_t NowUnixMs();

enum class Channel : std::uint8_t { Guild, Officer, Party, Raid, RaidWarning, Say, Yell, Whisper, Emote };

std::string_view ChannelName(Channel c);
std::optional<Channel> ParseChannel(std::string_view name);

enum class CommandType : std::uint8_t
{
    Ping, GetCast, Say, Emote, HumanSay, SetRank, SetMotd, AddFriend, Afk, Extension
};

struct Command
{
    CommandType type = CommandType::Ping;
    std::string id;
    std::uint32_t guid = 0;
    Channel channel = Channel::Say;
    std::string text;
    std::uint32_t toGuid = 0;
    std::string toName;
    std::string emote;
    std::uint32_t targetGuid = 0;
    std::uint8_t rank = 0;
    std::string note;
    bool on = false;
    std::string message;
    std::string extType;      // Extension only: the type name
    nlohmann::json raw;       // Extension only: the whole message
};

// id: the line's string "id" whenever the line is a JSON object that has one, even when it is rejected
struct DecodeResult
{
    std::optional<Command> command;
    std::string id;
    std::string error;
    bool ackable = false;
};

DecodeResult DecodeCommand(std::string_view line);

// Extension commands: the decoder checks only the envelope (v, id, type) and hands over the raw message.
bool RegisterExtensionCommand(std::string_view type);   // thread-safe; false for a core type name or a duplicate
bool IsExtensionCommand(std::string_view type);         // thread-safe

namespace Err
{
inline constexpr std::string_view BadJson = "bad json";
inline constexpr std::string_view MissingId = "missing id";
inline constexpr std::string_view UnsupportedVersion = "unsupported version";
inline constexpr std::string_view UnknownType = "unknown type: ";
inline constexpr std::string_view BadField = "bad field: ";
inline constexpr std::string_view Busy = "busy";
inline constexpr std::string_view LineTooLong = "line too long";
inline constexpr std::string_view AnotherMind = "another mind is connected";
inline constexpr std::string_view NotCast = "not a cast member";
inline constexpr std::string_view Offline = "offline";
inline constexpr std::string_view Dead = "dead";
inline constexpr std::string_view HumanOffline = "human offline";
inline constexpr std::string_view TargetOffline = "target offline";
inline constexpr std::string_view NotInGroup = "not in a group";
inline constexpr std::string_view NotInRaid = "not in a raid";
inline constexpr std::string_view EmptyText = "empty text";
inline constexpr std::string_view CommandPrefix = "command prefix";
inline constexpr std::string_view UnknownEmote = "unknown emote";
inline constexpr std::string_view InvalidRank = "invalid rank";
inline constexpr std::string_view FriendsFull = "friends list full";
inline constexpr std::string_view EnemyFaction = "enemy faction";
inline constexpr std::string_view NoGuild = "guild not found";
inline constexpr std::string_view HandlerError = "handler error";   // an extension handler threw
}

enum class BossState : std::uint8_t { Engaged, Killed, Wiped };

// EncounterState values: NOT_STARTED 0, IN_PROGRESS 1, FAIL 2, DONE 3, SPECIAL 4, TO_BE_DECIDED 5
std::optional<BossState> ClassifyBossTransition(std::uint8_t newState, std::uint8_t oldState);

enum class LootMethod : std::uint8_t { Need, Greed, Master };
enum class BgWinner : std::uint8_t { Horde, Alliance, None };

struct ChatEvent
{
    Channel channel;
    std::uint32_t fromGuid;
    std::string fromName;
    std::uint32_t toGuid;
    std::string text;
    std::uint32_t map;
    std::uint32_t zone;
    bool viaBridge = false;
};

struct BossEvent
{
    std::uint32_t map, instanceId, bossId;
    std::string bossName;
    BossState state;
    std::vector<std::uint32_t> members;
};

struct LootItem
{
    std::uint32_t itemId;
    std::string name;
    std::uint8_t quality;
    std::uint32_t index;
};

struct LootDropEvent
{
    std::uint32_t map, instanceId, sourceEntry;
    std::vector<LootItem> items;
};

// Compact, invalid UTF-8 replaced (error_handler_t::replace), never throws.
std::string DumpLine(nlohmann::json const& j);

// Every encoder returns one line without '\n' carrying "v":1, "type", "ts".
std::string EncodeHello(std::int64_t ts, std::uint32_t guildId, std::uint32_t humanGuid);
std::string EncodeLogin(std::int64_t ts, bool login, std::uint32_t guid, bool human);
std::string EncodeChat(std::int64_t ts, ChatEvent const& e);
std::string EncodeBoss(std::int64_t ts, BossEvent const& e);
std::string EncodeDeath(std::int64_t ts, std::uint32_t guid, std::uint32_t map, std::uint32_t killerEntry);
std::string EncodeResurrect(std::int64_t ts, std::uint32_t guid);
std::string EncodeLootDrop(std::int64_t ts, LootDropEvent const& e);
std::string EncodeLootWon(std::int64_t ts, std::uint32_t guid, std::uint32_t itemId, std::string_view name,
    LootMethod m);
std::string EncodeAchievement(std::int64_t ts, std::uint32_t guid, std::uint32_t achievementId,
    std::string_view name);
std::string EncodeBgEnd(std::int64_t ts, std::uint32_t bgType, BgWinner winner,
    std::vector<std::uint32_t> const& members);
std::string EncodeZone(std::int64_t ts, std::uint32_t guid, std::uint32_t zoneId, std::string_view zoneName);
std::string EncodeGroup(std::int64_t ts, std::uint32_t leaderGuid, bool raid,
    std::vector<std::uint32_t> const& members);
std::string EncodeDcStatus(std::int64_t ts, std::uint32_t tankGuid, bool enabled, bool paused,
    std::string_view payload);
std::string EncodeTextEmote(std::int64_t ts, std::uint32_t guid, std::string_view emote, std::uint32_t targetGuid,
    bool viaBridge);
std::string EncodeAfk(std::int64_t ts, std::uint32_t guid, bool on);
std::string EncodeAck(std::int64_t ts, std::string_view id, bool ok, std::string_view error);
std::string EncodePong(std::int64_t ts, std::string_view id);
std::string EncodeError(std::int64_t ts, std::string_view message, std::string_view id = {});   // "id" only if set
std::string EncodeEvent(std::int64_t ts, nlohmann::json body);   // body carries "type"; adds "v" and "ts"
}

#endif
