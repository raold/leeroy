// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_GUILD_ADD_POLICY_H
#define MOD_LEEROY_GUILD_ADD_POLICY_H

#include <array>
#include <cstddef>
#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <vector>

namespace Leeroy::Guild
{
// The owner's `.playerbots bot add5|add10|add25|add40`, filled from his own guild's free cast members instead of
// playerbots' addclass characters. Nobody else's command, and no other command, is touched.
enum class GuildAddRole : std::uint8_t { Tank, Healer, Damage };
inline constexpr std::size_t kGuildAddRoles = 3;
using GuildAddCounts = std::array<std::uint32_t, kGuildAddRoles>;

// Tank/healer/damage targets of playerbots' add patch: 5 1/1/3, 10 2/3/5, 25 3/6/16, 40 4/10/26.
std::optional<GuildAddCounts> GuildAddComposition(std::uint32_t size);

// completions is the core's own resolution of the typed command (Acore::ChatCommands::GetAutoCompletionsFor), so
// abbreviations and case are matched exactly as the core would invoke it. Returns the size for `playerbots bot
// addN ...` and 0 otherwise. The subcommand is compared the way playerbots parses it: the first space-separated
// word of the arguments, case-sensitive.
std::uint32_t ParseGuildAddCommand(std::vector<std::string> const& completions);

struct GuildAddGate
{
    bool console = false, enabled = false;
    std::uint32_t invoker = 0, human = 0, guild = 0;
    std::size_t cast = 0;
};
// Only the configured human, in game, with mod-leeroy on and a guild cast. Everyone else reaches playerbots.
bool GuildAddIntercepts(GuildAddGate const& gate);

// The instance copy he stands in: its player cap (0 outside an instance) and whether it is a raid. The core never lets
// more than the cap in; members already on their way when it fills are sent to their homebinds.
struct GuildAddPlace
{
    std::uint32_t cap = 0;
    bool raid = false;
};
// Empty when a group of size fits where he stands; otherwise the one line he is told, before anything moves.
std::string GuildAddSizeRefusal(std::uint32_t size, GuildAddPlace const& place);
// Empty when his group of members fits the copy he has just entered; otherwise why none of them is brought in.
std::string GuildAddEntryRefusal(std::uint32_t members, GuildAddPlace const& place);

// One cast member's state just before the pick. fence is the first reason the existing ownership fences (Realm,
// training, offline raids, a body that is not a usable bot) give, empty when none holds the body.
struct GuildAddBody
{
    bool online = false, cast = false, otherTeam = false, dead = false, inCombat = false, grouped = false;
    bool inBattleground = false, queued = false, teleporting = false, inFlight = false, elsewhere = false;
    bool locked = false;   // saved to another copy of the instance he is in
    std::string fence;
};
// Empty when the member is free to join his group.
std::string GuildAddSkipReason(GuildAddBody const& body);

// The raid buffs and debuffs a member's bot applies in WotLK 3.3.5 (talents, and what playerbots' default strategies
// for that tree cast), one bit each. Same-kind effects do not stack, so one source covers the raid; Replenishment
// reaches 10 members, so a 25 or 40 wants two sources.
enum class GuildAddBuff : std::uint8_t
{
    Bloodlust, Replenishment,
    MeleeHaste, SpellCrit, AttackPower, Damage, PhysicalCrit,
    SpellDamageTaken, BleedDamage, PhysicalDamageTaken, SpellHit, CritTaken, SpellCritTaken,
    ArmorReduction, SpellPower, Kings, Fortitude, MarkOfTheWild, ArcaneIntellect, BattleRes,
};
inline constexpr std::size_t kGuildAddBuffs = 20;
using GuildAddBuffs = std::uint32_t;
constexpr GuildAddBuffs GuildAddBuffBit(GuildAddBuff buff) { return GuildAddBuffs(1) << static_cast<unsigned>(buff); }
// 100 for Bloodlust and Replenishment, 10 for the % buffs and debuffs, 1 for the rest: one higher kind outweighs any
// number of lower ones a single member can bring.
std::uint32_t GuildAddBuffWeight(GuildAddBuff buff);

// What a healer is for: Holy paladin on the tanks, Discipline's absorbs, and the raid healers.
enum class GuildAddHealer : std::uint8_t { None, Tank, Absorb, RaidShaman, RaidDruid, RaidPriest };

struct GuildAddSpec
{
    GuildAddBuffs buffs = 0;
    bool melee = false;
    GuildAddHealer healer = GuildAddHealer::None;   // None unless role is Healer and the tab is a healing tree
};
// cls is the class id, tab the talent tree with most points (-1 unknown: only class-wide buffs count).
GuildAddSpec GuildAddSpecFacts(std::uint8_t cls, std::int8_t tab, GuildAddRole role);

struct GuildAddMember
{
    GuildAddRole role = GuildAddRole::Damage;
    std::uint8_t cls = 0;
    std::int8_t tab = -1;
};
struct GuildAddCandidate
{
    std::uint32_t guid = 0;
    GuildAddRole role = GuildAddRole::Damage;
    std::uint8_t cls = 0;
    std::int8_t tab = -1;
    std::uint32_t gear = 0;   // average item level, whole levels; 0 when unknown
};
struct GuildAddPick
{
    std::uint32_t guid = 0;
    GuildAddRole role = GuildAddRole::Damage;
    GuildAddBuffs buffs = 0;
};

struct GuildAddPlan
{
    std::uint32_t size = 0, members = 0;
    bool full = false;
    GuildAddCounts open{};             // slots of each role this run tries to fill
    GuildAddBuffs covered = 0;         // what his connected members bring before the fill
    std::vector<GuildAddPick> picks;   // tanks, then healers, then damage
};

// members counts every slot of his group (him and offline members included); connected are the members whose role
// is known, him included. Each counts toward its own role while that role still has room, so a re-run fills only
// the gaps. When members outnumber their roles, damage loses its open slots first, then healers.
// Tanks: among those within 10 item levels of the best-geared tank left, the tank class his tanks have fewest of,
// then gear. Healers fill the size's template (5: a Holy paladin or Discipline priest; 10: Holy paladin, Discipline,
// a raid healer; 25: two Holy paladins, one Discipline, three raid healers mixing shaman, druid and Holy priest; 40:
// three, two and five), minus healers already there and cut to the open slots; a kind nobody free has goes to the
// best-geared healer left. Damage: the member adding the most weight of buffs his group and the picks so far lack,
// then gear, then the melee/ranged split nearest even. The last ties go to the class his group has fewest of, then
// the lowest guid.
GuildAddPlan PlanGuildAdd(std::uint32_t size, std::uint32_t members, std::vector<GuildAddMember> const& connected,
    std::vector<GuildAddCandidate> candidates);

// added is what actually joined, per role; open slots not added stayed empty for want of a free guild member.
// notBrought joined but could not be teleported to him. unavailable counts skip reasons for the tally line.
std::vector<std::string> GuildAddMessages(GuildAddPlan const& plan, GuildAddCounts const& added,
    std::uint32_t notBrought, std::map<std::string, std::uint32_t> const& unavailable);

// covered is what his group brings once the fill is done. One chat line naming what it still lacks: Bloodlust and
// Replenishment for a 5-man, those and the % buffs and debuffs for a raid. Empty when none is missing, and when the
// fill added nobody (his group was full, or nobody was free).
std::string GuildAddMissingLine(GuildAddPlan const& plan, GuildAddCounts const& added, GuildAddBuffs covered);
}

#endif
