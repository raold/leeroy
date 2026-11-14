// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_STRINGS_H
#define MOD_LEEROY_STRINGS_H

#include <array>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <nlohmann/json.hpp>

namespace Leeroy::Guild
{
// The owner's strings in game (D6 Part B): he asks one cast member, from the guild console, to come to him, join his
// party, follow him, duel him, emote at him or sit down. Each is his own ask, carried out at once on the world thread
// or refused with one code; nothing here takes a Realm body-ledger rank, and every native fence still holds.
inline constexpr std::string_view kStringDoType = "string_do";

enum class StringKind : std::uint8_t { ComeToMe, JoinGroup, Follow, DuelMe, Emote, Sit };
std::optional<StringKind> ParseStringKind(std::string_view name);
std::string_view StringKindName(StringKind kind);

// come_to_me keeps a member who is not in his group where she arrived, playerbots' stay, for this long.
inline constexpr std::uint64_t kStringStayMs = 600000;
// A sit lasts this long at most (#244's social seat), while he stays within kStringSitYards.
inline constexpr std::uint32_t kStringSitSeconds = 300;
inline constexpr float kStringSitYards = 20.0f;
// He sees a text emote within the core's text emote listen range (25 yards by default).
inline constexpr float kStringEmoteYards = 25.0f;
// The Realm's duel path challenges within this distance (RealmBodies Duel).
inline constexpr float kStringDuelYards = 30.0f;
// A party: he and up to four others. join_group never converts it to a raid.
inline constexpr std::uint32_t kStringPartySize = 5;

// AreaTable flags (DBCEnums.h AreaFlags), mirrored so the policy needs no core header.
inline constexpr std::uint32_t kAreaSlaveCapital = 0x00000008;
inline constexpr std::uint32_t kAreaSlaveCapital2 = 0x00000020;
inline constexpr std::uint32_t kAreaAllowDuels = 0x00000040;
inline constexpr std::uint32_t kAreaCapital = 0x00000100;
inline constexpr std::uint32_t kAreaCity = 0x00000200;
inline constexpr std::uint32_t kAreaSanctuary = 0x00000800;
inline constexpr std::uint32_t kAreaTown = 0x00200000;
inline constexpr std::uint32_t kAreaRestHorde = 0x00400000;
inline constexpr std::uint32_t kAreaRestAlliance = 0x00800000;
// come_to_me brings a member to him only in a city or a rest area: a capital and its parts, a town with an inn, or a
// faction rest zone (or wherever the core has him resting).
inline constexpr std::uint32_t kStringGatherAreas = kAreaCapital | kAreaSlaveCapital | kAreaSlaveCapital2 | kAreaCity
    | kAreaTown | kAreaRestHorde | kAreaRestAlliance;

// The text emotes a string may ask for, aimed at him (LeeroyEmotes.cpp names).
inline constexpr std::array<std::string_view, 16> kStringEmotes = {
    "applaud", "bow", "cheer", "dance", "flirt", "hello", "highfive", "hug", "kiss", "laugh", "salute", "shy",
    "smile", "thank", "wave", "wink"};
bool StringEmoteAllowed(std::string_view name);

// {guid, kind, target_guid (0 or him), emote (kind emote only)}. Fields only: the bridge checked v, id and type.
struct StringDoCmd
{
    std::uint32_t guid = 0;
    StringKind kind = StringKind::ComeToMe;
    std::uint32_t targetGuid = 0;
    std::string emote;
};
std::optional<StringDoCmd> ParseStringDo(nlohmann::json const& message, std::string& error);

// What the world thread saw of him and the member when the string arrived.
struct StringFacts
{
    bool enabled = false;          // Leeroy.Enable and Leeroy.Strings.Enable
    // him
    bool humanOnline = false, humanAlive = false;
    bool humanCombat = false;      // his fight: him, or anyone of his group on his map, in combat
    bool humanTravelling = false;  // teleporting, on a taxi, a transport or a vehicle
    bool humanInstance = false, humanBattleground = false, humanDuel = false;
    bool humanResting = false;     // the core has him resting (an inn or a city)
    std::uint32_t humanArea = 0;   // his area's AreaTable flags
    std::uint32_t humanZone = 0;   // his zone's AreaTable flags
    // her
    bool cast = false;             // a pinned cast member of his guild, run by playerbots on a random-bot account
    bool online = false;
    std::string fence;             // the first ownership fence: a Realm lease, training, an offline raid, a bot job
    bool dead = false, combat = false, travelling = false, duel = false;
    bool battleground = false, queued = false, instance = false;
    std::uint32_t area = 0;        // her area's AreaTable flags
    // the two of them
    bool sameMap = false, sameZone = false;
    float distance = 0.0f;         // yards, on the same map
    // his group
    bool hisGroup = false;         // she is a member of the group he is in
    bool otherGroup = false;       // she is in another group, or has an invitation to one
    bool leader = true;            // he leads his group (true when he has none)
    bool groupSpecial = false;     // his group is a Dungeon Finder or battleground group
    bool groupRaid = false;
    std::uint32_t groupMembers = 0;   // his group's members, 0 without a group
    std::string joinRefusal;       // GuildAdd's own refusal for her (join_group), empty when she may join
    std::string emote;             // kind emote
};

// Empty when the string may be carried out now, else its one refusal code. Every kind first: off, human_offline,
// not_cast, not_online, the fence, dead, combat (hers or his: never inside his fight), in_instance (hers or his: never
// onto or inside any instance copy), in_bg, queued, travelling, human_dead. Then per kind:
//   come_to_me  grouped (in someone else's group), bad_area (he is not in a city or rest area);
//   join_group  in_group, lfg_group, not_leader, in_raid, group_full (a party of 5), GuildAdd's own refusal;
//   follow      not_in_group (not in his group), busy:raid_owner (his raid);
//   duel_me     in_group (they are grouped), busy:duel, too_far (another zone, or beyond kStringDuelYards), bad_area
//               (either stands where the core allows no duel, or in a sanctuary);
//   emote       bad_emote, too_far (beyond kStringEmoteYards or another map);
//   sit         too_far (beyond kStringSitYards or another map).
std::string StringRefusal(StringKind kind, StringFacts const& facts);
}

#endif
