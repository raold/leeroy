// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_LEEROY_LEEROY_REALM_COMMANDS_H
#define MOD_LEEROY_LEEROY_REALM_COMMANDS_H

#include "LeeroyRealmPolicy.h"
#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>
#include <nlohmann/json.hpp>

namespace Leeroy::Realm
{
inline constexpr std::array<std::string_view, 34> kCommandTypes = {
    "realm_actors", "realm_say", "realm_emote", "realm_move", "rpg_status", "leave_pvp", "rearm", "wg_join",
    "arena_team", "arena_queue", "duel", "craft", "item_count", "equip", "set_profession", "transmog_set",
    "calendar_post", "calendar_signup", "calendar_update", "mail_send", "bank_tab_buy", "bank_money",
    "bank_deposit", "bank_withdraw", "bank_stock", "grant_achievement", "guild_rename", "guild_grow",
    "guild_admit", "guild_note", "announce", "get_realm_pvp", "guild_readmit", "world_say"
};
inline constexpr std::array<std::uint32_t, 7> kGrantable = {3117, 4078, 4576, 2186, 3007, 1723, 1786};
inline constexpr std::uint32_t kGuildTabard = 5976;
inline constexpr std::array<std::string_view, 9> kRpgStatuses = {
    "idle", "rest", "wander npc", "wander random", "go grind", "go camp", "do quest", "do gather", "travel flight"
};
inline constexpr std::array<std::string_view, 7> kSayChannels = {
    "trade", "general", "lfg", "recruitment", "say", "yell", "whisper"
};
inline constexpr std::array<std::uint32_t, 14> kProfessionSkills = {
    171, 164, 333, 202, 182, 773, 755, 165, 186, 393, 197, 185, 356, 129
};
inline constexpr std::array<std::uint32_t, 4> kHordeCapitals = {1637, 1497, 1638, 3487};
inline constexpr std::array<std::uint32_t, 4> kAllianceCapitals = {1519, 1537, 1657, 3557};
inline constexpr std::array<std::uint32_t, 2> kNeutralCities = {3703, 4395};
inline constexpr std::array<std::string_view, 13> kPlaceKinds = {
    "city", "inn", "fish", "duel", "holiday", "arena", "arenamaster", "herb", "ore", "camp", "show", "home", "photo"
};
inline constexpr std::array<std::string_view, 4> kItemUses = {"gift", "bank", "holiday", "prize"};
inline constexpr std::size_t kMaxCsvBytes = 1024 * 1024, kMaxCsvRows = 4096, kMaxCsvLineBytes = 1024;
inline constexpr std::uint32_t kMaxItemCount = 1000;
// AchievementFlags (DBCEnums.h): the core's two realm-first kinds.
inline constexpr std::uint32_t kAchievementRealmFirstReach = 0x100, kAchievementRealmFirstKill = 0x200;
// AreaFlags (DBCEnums.h): the zone flag the core's own in-city rule tests
// (Player::UpdateZone: rest and no-PvP in a capital, sanctuaries included).
inline constexpr std::uint32_t kAreaFlagCapital = 0x100;
inline bool IsRealmFirstAchievement(std::uint32_t flags)
{ return (flags & (kAchievementRealmFirstReach | kAchievementRealmFirstKill)) != 0; }
inline bool IsCityZone(std::uint32_t zoneFlags) { return (zoneFlags & kAreaFlagCapital) != 0; }

// Test-only registration. Runtime handlers register through LeeroyRegisterCommand.
void RegisterRealmCommandTypes();
bool IsQueued(std::string_view type);
bool ReturnsValue(std::string_view type);
bool IsPrimaryProfession(std::uint32_t skill);

struct ActorsCmd { std::vector<std::uint32_t> guids, gankers, guilds; };
struct SayCmd { std::uint32_t guid = 0; std::string channel, text; std::uint32_t toGuid = 0; };
struct WorldSayCmd { SayCmd speech; std::uint32_t guild = 0; };
struct EmoteCmd { std::uint32_t guid = 0; std::string emote; std::uint32_t targetGuid = 0; };
struct MoveCmd { std::uint32_t guid = 0; std::string place; std::uint32_t slot = 0; };
struct RpgStatusCmd { std::uint32_t guid = 0; std::string status; std::uint32_t questId = 0; };
struct LeavePvpCmd { std::vector<std::uint32_t> guids; std::string pvpOwner; };
struct GetRealmPvpCmd { std::string pvpOwner, originId; };
struct RearmCmd { std::vector<std::uint32_t> guids; };
struct WgJoinCmd { std::vector<std::uint32_t> guids; };
// The v1 discriminator always occupies "type". Payload keys are arena_type/event_type,
// including arena event bodies; there is no colliding legacy "type" alias.
struct ArenaTeamCmd
{
    // Deferred through the shared body budget; terminal realm_job.value carries
    // the team id. The immediate ACK proves admission only.
    std::string action;
    std::uint32_t captainGuid = 0, arenaType = 0;
    std::string name;
    std::uint32_t teamId = 0, guid = 0;
};
struct ArenaQueueCmd { std::uint32_t teamId = 0, captainGuid = 0; };
struct DuelCmd { std::uint32_t guid = 0, targetGuid = 0; };
struct CraftCmd { std::uint32_t guid = 0, spellId = 0, count = 1; };
struct ItemCountCmd { std::uint32_t guid = 0; std::vector<std::uint32_t> itemIds; };
struct EquipCmd { std::uint32_t guid = 0, itemId = 0; };
struct SetProfessionCmd { std::uint32_t guid = 0, skillId = 0; };
struct TransmogSetCmd { std::uint32_t guid = 0, slot = 0, entry = 0; };
struct CalendarPostCmd
{
    std::uint32_t creatorGuid = 0;
    std::string title, description, eventType;
    std::uint64_t at = 0;
    std::int32_t dungeonId = -1;
    bool guildEvent = true, inviteHuman = false;
};
struct CalendarSignupCmd { std::uint64_t eventId = 0; std::uint32_t guid = 0; std::string status; };
struct CalendarUpdateCmd { std::uint64_t eventId = 0; std::string title, description; std::uint64_t at = 0; };
struct MailItem { std::uint32_t itemId = 0, count = 0; std::string from; };
struct MailSendCmd
{
    std::uint32_t fromGuid = 0;
    std::string subject, body;
    std::vector<MailItem> items;
    std::uint32_t money = 0, delayS = 600;
};
struct BankTabBuyCmd { std::uint32_t officerGuid = 0, tab = 0; std::string name, icon; };
struct BankMoneyCmd { std::uint32_t amount = 0; };
struct BankDepositCmd { std::uint32_t guid = 0, itemId = 0, count = 0, tab = 0; };
struct BankWithdrawCmd { std::uint32_t guid = 0, itemId = 0, count = 0, tab = 0; };
struct BankStockCmd { std::uint32_t officerGuid = 0, tab = 0, itemId = 0, count = 0; };
struct GrantAchievementCmd { std::uint32_t guid = 0, achievementId = 0; };
struct GuildRenameCmd { std::uint32_t guildId = 0; std::string name; };
struct GuildGrowCmd { std::uint32_t guildId = 0; std::vector<std::uint32_t> guids; };
struct GuildAdmitCmd { std::uint32_t guid = 0, rank = 4; };
// Consented re-admission of one member he kicked. kick_ts is the exact ts of the
// guard his_kick event the mind received; rank is the readmission rank the
// native receipt derives from that kick (ReadmitRank).
struct GuildReadmitCmd { std::string id; std::uint32_t guid = 0; std::uint64_t kickTs = 0; std::uint32_t rank = 0; };
struct GuildNoteCmd { std::uint32_t guid = 0; std::string officerNote; };
struct AnnounceCmd : KillIdentity
{
    std::string id, killId, bossName;
    std::uint32_t attempts = 0, fightS = 0;
    std::string first;
};
// Runtime-only metric gate, after exact proof lookup. Zero omits a metric. A
// named metric must equal the proof's own: attempts the native count (never
// 0, which is unknown), fight seconds the measured human-present duration.
struct AnnouncementMetrics
{
    std::uint32_t attempts = 0, fightS = 0;
    bool operator==(AnnouncementMetrics const&) const = default;
};
std::optional<AnnouncementMetrics> ValidatedAnnouncementMetrics(AnnounceCmd const&, Kill const&);

// The bridge owns envelope validation. Parsers validate wire fields only;
// identities, live busy/ownership rules, emote IDs, place/item whitelist lookup,
// calendar future time and the actual kill receipt remain runtime checks.
std::optional<ActorsCmd> ParseActors(nlohmann::json const&, std::string& error);
std::optional<SayCmd> ParseSay(nlohmann::json const&, std::string& error);
std::optional<WorldSayCmd> ParseWorldSay(nlohmann::json const&, std::string& error);
std::optional<EmoteCmd> ParseEmote(nlohmann::json const&, std::string& error);
std::optional<MoveCmd> ParseMove(nlohmann::json const&, std::string& error);
std::optional<RpgStatusCmd> ParseRpgStatus(nlohmann::json const&, std::string& error);
std::optional<LeavePvpCmd> ParseLeavePvp(nlohmann::json const&, std::string& error);
std::optional<GetRealmPvpCmd> ParseGetRealmPvp(nlohmann::json const&, std::string& error);
std::optional<RearmCmd> ParseRearm(nlohmann::json const&, std::string& error);
std::optional<WgJoinCmd> ParseWgJoin(nlohmann::json const&, std::string& error);
std::optional<ArenaTeamCmd> ParseArenaTeam(nlohmann::json const&, std::string& error);
std::optional<ArenaQueueCmd> ParseArenaQueue(nlohmann::json const&, std::string& error);
std::optional<DuelCmd> ParseDuel(nlohmann::json const&, std::string& error);
std::optional<CraftCmd> ParseCraft(nlohmann::json const&, std::string& error);
std::optional<ItemCountCmd> ParseItemCount(nlohmann::json const&, std::string& error);
std::optional<EquipCmd> ParseEquip(nlohmann::json const&, std::string& error);
std::optional<SetProfessionCmd> ParseSetProfession(nlohmann::json const&, std::string& error);
std::optional<TransmogSetCmd> ParseTransmogSet(nlohmann::json const&, std::string& error);
std::optional<CalendarPostCmd> ParseCalendarPost(nlohmann::json const&, std::string& error);
std::optional<CalendarSignupCmd> ParseCalendarSignup(nlohmann::json const&, std::string& error);
std::optional<CalendarUpdateCmd> ParseCalendarUpdate(nlohmann::json const&, std::string& error);
std::optional<MailSendCmd> ParseMailSend(nlohmann::json const&, std::string& error);
std::optional<BankTabBuyCmd> ParseBankTabBuy(nlohmann::json const&, std::string& error);
std::optional<BankMoneyCmd> ParseBankMoney(nlohmann::json const&, std::string& error);
std::optional<BankDepositCmd> ParseBankDeposit(nlohmann::json const&, std::string& error);
std::optional<BankWithdrawCmd> ParseBankWithdraw(nlohmann::json const&, std::string& error);
std::optional<BankStockCmd> ParseBankStock(nlohmann::json const&, std::string& error);
std::optional<GrantAchievementCmd> ParseGrantAchievement(nlohmann::json const&, std::string& error);
std::optional<GuildRenameCmd> ParseGuildRename(nlohmann::json const&, std::string& error);
std::optional<GuildGrowCmd> ParseGuildGrow(nlohmann::json const&, std::string& error);
std::optional<GuildAdmitCmd> ParseGuildAdmit(nlohmann::json const&, std::string& error);
std::optional<GuildNoteCmd> ParseGuildNote(nlohmann::json const&, std::string& error);
std::optional<GuildReadmitCmd> ParseGuildReadmit(nlohmann::json const&, std::string& error);
std::optional<AnnounceCmd> ParseAnnounce(nlohmann::json const&, std::string& error);

struct Place
{
    std::string name;
    std::uint32_t map = 0, zone = 0;
    float x = 0, y = 0, z = 0, o = 0;
    std::string kind;
};
struct ItemRule { std::uint32_t itemId = 0; std::string use; std::uint32_t maxCount = 0; };
// Strict unquoted CSV with exact header; blank/comment lines allowed. Place
// names are ASCII identifiers; duplicate item IDs are allowed only across uses.
// Map <=999, zone <=65535, xyz within +/-20000, orientation within [0,2*pi].
std::optional<std::vector<Place>> ParsePlaces(std::string_view csv, std::string& error);
std::optional<std::vector<ItemRule>> ParseItems(std::string_view csv, std::string& error);
// Leeroy.Realm.PlacesFile and ItemsFile: a relative value names a file in the
// module config directory (beside mod_leeroy.conf), a rooted one stays as
// written, and an empty one names no file.
std::string ResolveCsvPath(std::string_view moduleConfigDir, std::string_view configured);
// The whole file, or nothing when it is missing, not a regular file, unreadable
// or larger than kMaxCsvBytes.
std::optional<std::string> ReadCsvFile(std::string const& path);

struct RealmJob
{
    std::string id, kind;
    bool ok = false;
    std::string reason;
    std::uint32_t guid = 0;
    std::uint64_t value = 0;
    std::string pvpOwner{};
    // Absent means unknown. False proves the entire failed job changed nobody.
    std::optional<bool> changed{};
    // Members whose step native skipped, as they were and no longer held. On the wire only when
    // there are some, so older minds and jobs without skips see the old shape.
    std::vector<std::uint32_t> skipped{};
};
nlohmann::json MakeRealmJob(RealmJob const&);
// Body only; the drain supplies this proof's already captured publishedAtMs to
// EncodeEvent exactly once, before handing the encoded line to bridge replay.
nlohmann::json MakeRealmKill(Kill const&);
// The same exact body for a verified 5-man guild kill, under its own type.
nlohmann::json MakeRealmDungeonKill(Kill const&);
struct ContinuationOwner;
nlohmann::json MakeRealmPvp(std::string_view id, ContinuationOwner const&);
nlohmann::json MakeChannelChat(std::string_view channel, std::uint32_t fromGuid, std::string_view fromName,
    std::uint32_t zone, std::string_view text, bool viaBridge);
nlohmann::json MakePvpKill(std::uint32_t killer, std::uint32_t victim, std::uint32_t map, std::uint32_t zone);
// zone is the winner's; city is true when either duelist's zone is a city (IsCityZone).
nlohmann::json MakeDuelEnd(std::uint32_t winner, std::uint32_t loser, std::string_view result,
    std::uint32_t map, std::uint32_t zone, bool city);
// An unscoped achievement completion: never published inside LeeroyQuietScope.
nlohmann::json MakeRealmAchievement(std::uint32_t guid, std::uint32_t achievementId, std::string_view name,
    std::uint32_t category, std::uint32_t flags, bool realmFirst);
nlohmann::json MakePvpFlag(std::uint32_t guid, bool flagged);
nlohmann::json MakeWg(std::string_view state, std::string_view defender, std::uint32_t seconds, double updateMs);
nlohmann::json MakeArenaQueue(std::uint32_t teamId, std::uint32_t arenaType, bool rated);
nlohmann::json MakeArenaEnd(std::uint32_t arenaType, std::uint32_t winnerTeam, std::uint32_t loserTeam,
    std::int32_t winnerChange, std::int32_t loserChange, std::vector<std::uint32_t> const& members);
nlohmann::json MakeGameEvent(std::uint32_t eventId, std::string_view state);
nlohmann::json MakeCalendarRsvp(std::uint64_t eventId, std::string_view status);
nlohmann::json MakeMailTaken(std::uint32_t itemGuid, std::uint32_t itemId, std::uint32_t count);
nlohmann::json MakeGuard(std::uint32_t guid, std::string_view action, std::uint32_t rank, std::string_view reason);
std::string FormatAnnouncement(std::string_view guild, std::string_view boss, std::uint32_t size,
    bool heroic, std::uint32_t attempts, std::uint32_t fightS, bool ever);
}

#endif
