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
// What this build understands beyond the base protocol, listed in hello's "features": item_links, the {{item:<id>}}
// placeholders in say and realm_say text (LeeroyItemLinks.h). A mind sends a placeholder only to a hello that lists it.
// regear: gear_up's "regear" mode, and every other gear_up a capped incremental pass that keeps the loot a bot holds
// (GearUpJob.cpp); a mind sends a regear, and expects those averages, only to a hello that lists it.
// dummy_party_on_arrival: dummy_trial moves each bot untouched and forms its solo party at the dummy, so the core's
// one-member-group disband on a map change cannot take it (DummyTrialJob.cpp); a mind runs its lab only on such a build.
// invites: string_do's go_to and fish kinds and its "via": "invite" mark (Leeroy.Invites.Enable, LeeroyStrings.h); a
// mind sends them only to a hello that lists it.
// lead: lead_do, a cast member of his group leading it in the open world while he follows (Leeroy.Lead.Enable,
// LeeroyLead.h); a mind sends it only to a hello that lists it.
// The build adds more at hello time (EncodeHello's `extra`): dc_toc and dc_ulduar, the raids the dungeon-clear it was
// built with can run for an offline raid (LeeroyOfflineRaidPolicy.h DcRaidFeatures); a mind plans those raids only
// on a hello that lists them.
inline constexpr std::string_view kFeatures[] = {"item_links", "playstyle", "raidlead", "mechanics", "regear",
    "dummy_party_on_arrival", "invites", "lead", "mechanics_nearest", "mechanics_naxx", "combat_quiet"};

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
    bool quietInCombat = false; // Say only: ambient lines stop at the world-thread send boundary.
    std::string message;
    std::string extType;      // Extension only: the type name
    nlohmann::json raw;       // Extension only: the whole message
    std::uint64_t connectionSerial = 0; // Set by PopCommands while it owns the inbox lock.
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
inline constexpr std::string_view CombatQuiet = "combat_quiet";
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
std::optional<BossState> ClassifyBossTransition(std::uint8_t newState, std::uint8_t oldState,
    std::uint32_t map = 0, std::uint32_t boss = 0);

enum class LootMethod : std::uint8_t { Need, Greed, Master };
enum class BgWinner : std::uint8_t { Horde, Alliance, None };

struct ChatEvent
{
    Channel channel = Channel::Say;
    std::uint32_t fromGuid = 0;
    std::string fromName;
    std::uint32_t toGuid = 0;
    std::string text;
    std::uint32_t map = 0;
    std::uint32_t zone = 0;
    bool viaBridge = false;
};

struct BossEvent
{
    std::uint32_t map = 0, instanceId = 0, bossId = 0;
    std::string bossName;
    BossState state = BossState::Engaged;
    std::vector<std::uint32_t> members;
};

struct LootItem
{
    std::uint32_t itemId = 0;
    std::string name;
    std::uint8_t quality = 0;
    std::uint32_t index = 0;
};

struct LootDropEvent
{
    std::uint32_t map = 0, instanceId = 0, sourceEntry = 0;
    std::vector<LootItem> items;
};

// Compact, invalid UTF-8 replaced (error_handler_t::replace), never throws.
std::string DumpLine(nlohmann::json const& j);

// Every encoder returns one line without '\n' carrying "v":1, "type", "ts".
// startedMs names the worldserver process (Unix ms at script registration); 0 leaves it out.
// Runtime features are opt-in. Extra build features follow in order, each listed once.
std::string EncodeHello(std::int64_t ts, std::uint32_t guildId, std::uint32_t humanGuid, std::int64_t startedMs = 0,
    std::vector<std::string> const& extra = {}, bool memberBankMail = false, bool trainingOn = false,
    bool bankRestock = false);
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
