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

struct GuildAddMember
{
    GuildAddRole role = GuildAddRole::Damage;
    std::uint8_t cls = 0;
};
struct GuildAddCandidate
{
    std::uint32_t guid = 0;
    GuildAddRole role = GuildAddRole::Damage;
    std::uint8_t cls = 0;
};
struct GuildAddPick
{
    std::uint32_t guid = 0;
    GuildAddRole role = GuildAddRole::Damage;
};

struct GuildAddPlan
{
    std::uint32_t size = 0, members = 0;
    bool full = false;
    GuildAddCounts open{};             // slots of each role this run tries to fill
    std::vector<GuildAddPick> picks;   // tanks, then healers, then damage
};

// members counts every slot of his group (him and offline members included); connected are the members whose role
// is known, him included. Each counts toward its own role while that role still has room, so a re-run fills only
// the gaps. When members outnumber their roles, damage loses its open slots first, then healers. Within a role,
// classes the group has fewest of come first (buffs spread across classes); ties go to the lowest guid.
GuildAddPlan PlanGuildAdd(std::uint32_t size, std::uint32_t members, std::vector<GuildAddMember> const& connected,
    std::vector<GuildAddCandidate> candidates);

// added is what actually joined, per role; open slots not added stayed empty for want of a free guild member.
// notBrought joined but could not be teleported to him. unavailable counts skip reasons for the tally line.
std::vector<std::string> GuildAddMessages(GuildAddPlan const& plan, GuildAddCounts const& added,
    std::uint32_t notBrought, std::map<std::string, std::uint32_t> const& unavailable);
}

#endif
