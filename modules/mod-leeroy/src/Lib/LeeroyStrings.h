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
// His invitations in game (mind/leeroy_mind/invites) use the same command marked "via": "invite", and add two kinds:
// go_to puts her at a meeting spot (LeeroyInviteSpots.h) and keeps her there as come_to_me does, and fish has her
// fish beside him or at a fishing spot through playerbots' own "master fishing". A string marked as an invitation,
// and every go_to and fish, answers to Leeroy.Invites.Enable instead of Leeroy.Strings.Enable.
inline constexpr std::string_view kStringDoType = "string_do";

enum class StringKind : std::uint8_t { ComeToMe, JoinGroup, Follow, DuelMe, Emote, Sit, GoTo, Fish };
std::optional<StringKind> ParseStringKind(std::string_view name);
std::string_view StringKindName(StringKind kind);
// go_to and fish: only ever an invitation.
bool InviteKind(StringKind kind);

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
// fish: she fishes at most this long, beside him only this close, and stops once he has come and gone farther than
// kInviteLeftYards (or to another map). At a fishing spot she counts as there within kInviteSpotYards of it.
inline constexpr std::uint64_t kInviteFishMs = 900000;
inline constexpr float kInviteFishYards = 40.0f;
inline constexpr float kInviteLeftYards = 60.0f;
inline constexpr float kInviteSpotYards = 15.0f;

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

// {guid, kind, target_guid (0 or him), emote (kind emote only), spot and slot (go_to only: a kInviteSpots name and
// 0 or 1), via ("invite" or absent)}. Fields only: the bridge checked v, id and type.
struct StringDoCmd
{
    std::uint32_t guid = 0;
    StringKind kind = StringKind::ComeToMe;
    std::uint32_t targetGuid = 0;
    std::string emote;
    std::string spot;
    std::uint32_t slot = 0;
    bool invite = false;   // via "invite", and every go_to and fish: Leeroy.Invites.Enable answers for it
};
std::optional<StringDoCmd> ParseStringDo(nlohmann::json const& message, std::string& error);

// What the world thread saw of him and the member when the string arrived.
struct StringFacts
{
    bool enabled = false;          // Leeroy.Enable and Leeroy.Strings.Enable (an invitation's: Leeroy.Invites.Enable)
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
    bool spotValid = false;        // go_to: the spot's point lies on its map, in its zone
    bool fishing = false;          // fish: she has the fishing skill
    bool atFishSpot = false;       // fish: she stands at the fishing spot a go_to of his sent her to
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
//   sit         too_far (beyond kStringSitYards or another map);
//   go_to       in_group (she is with him already), grouped (in someone else's group), bad_spot;
//   fish        grouped, no_fishing (no fishing skill), too_far (neither within kInviteFishYards of him nor at the
//               fishing spot he sent her to).
// For an invitation (go_to, fish, or via "invite") `off` means Leeroy.Invites.Enable is 0.
std::string StringRefusal(StringKind kind, StringFacts const& facts);

// What a string of ours holds on one body until it ends (Guild/Strings.cpp journals one per body, AI and session).
struct StringHoldState
{
    std::uint64_t until = 0;      // come_to_me's stay end; 0 for a follow hold, which lasts with its group
    std::uint64_t group = 0;      // the group a follow hold lasts with; 0 for come_to_me's
    std::uint64_t arriveBy = 0;   // come_to_me: the stay starts once she has landed, by this time
    bool stay = false;            // we added playerbots' stay, and take only that back
    bool follow = false;          // a follow string of ours is in force
    bool unfollowed = false;      // a stop took her follow off: given back when lifted inside that group
    int spot = -1;                // go_to: the kInviteSpots index she was sent to (her arrival is there, not by him)
    bool invite = false;          // the hold's last string was an invitation (Leeroy.Invites.Enable keeps it)
    bool fish = false;            // fish: our master fishing is in force
    bool fishAdded = false;       // we added playerbots' "master fishing", and take only that back
    std::uint64_t fishUntil = 0;  // the fishing ends by then at the latest
    bool met = false;             // he has been within kInviteFishYards of her while she fished
};
// A live come_to_me hold (from her arrival until its 10 minutes end) is a native fence, busy:string: the Realm,
// training and offline raids refuse her until it ends. His own asks (his add command, his strings) pass it. A follow hold never fences:
// she is in his group, which the Realm already refuses.
bool StringHoldFences(StringHoldState const& hold);

// The world tick's view of one held body.
struct StringHoldFacts
{
    std::uint64_t now = 0;
    bool on = false;            // Leeroy.Enable and Leeroy.Strings.Enable (an invitation's hold: Leeroy.Invites.Enable)
    bool humanOnline = false;
    std::uint64_t group = 0;    // her group now, 0 for none
    bool raid = false;          // that group is a raid
    bool humanNearby = false;          // he is within kInviteFishYards of her, on her map
    bool left = false;          // he is farther than kInviteLeftYards, on another map or offline
};
enum class StringHoldStep : std::uint8_t
{
    Keep,
    Lift,          // take back what the hold added (its stay, its master fishing), then forget it
    LiftFollow,    // the same, and give her follow back: still in the group a stop was made in
    Drop,          // forget it untouched: she joined a group, whose join reset her strategies, ours with them
    EndFish,       // take back only our master fishing; the follow part of the hold goes on
};
// come_to_me's and go_to's hold: Drop once she is in any group; Lift when the switch is off, he is offline, its 10
// minutes are up or she never landed. A follow hold: Lift once her group is another (or none); LiftFollow when the
// switch is off or his party became a raid, where a follow string can no longer undo the stop. Fishing ends (Lift
// outside his group, where it is the hold's reason to be; EndFish beside a follow of his group) when the switch is
// off, kInviteFishMs are up, or he came within kInviteFishYards and has left again; a fishing-only hold in his group
// lasts in his raid too.
StringHoldStep DecideStringHold(StringHoldState const& hold, StringHoldFacts const& facts);
// The tick's note before it decides: he has come to her while she fished.
void NoteStringHold(StringHoldState& hold, StringHoldFacts const& facts);
}

#endif
