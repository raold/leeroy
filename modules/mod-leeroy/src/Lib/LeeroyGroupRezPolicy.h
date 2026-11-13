// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_GROUP_REZ_POLICY_H
#define MOD_LEEROY_GROUP_REZ_POLICY_H

#include <array>
#include <cstdint>
#include <optional>
#include <vector>

namespace Leeroy::Guild
{
// Playerbots looks for a body to rez within SpellDistance * 2 (28.5 * 2) and in line of sight.
inline constexpr float kGroupRezSearchYards = 57.0f;
// A group member fighting this close to a body keeps it waiting; one fighting farther away on
// the same map (a continent) does not.
inline constexpr float kGroupRezCombatYards = 100.0f;
// A mounted rezzer this close to the ground can be put down without a fall.
inline constexpr float kGroupRezGroundYards = 3.0f;
inline constexpr std::uint64_t kGroupRezRetryMs = 3000;
inline constexpr std::uint32_t kGroupRezDefaultReleaseSeconds = 60;

// Out-of-combat rez spells, highest rank first: Redemption, Resurrection, Ancestral Spirit.
// Null for a class without one (a druid's Revive is not in 3.3.5).
std::array<std::uint32_t, 7> const* RezRanks(std::uint8_t playerClass);

// A member of the owner's group lying dead (not released).
struct GroupRezBody
{
    bool bot = false;       // false for the owner: never released for him
    bool instance = false;  // dungeon or raid: waits for a rez, never released
    bool requested = false; // a rez landed and waits for an answer
    bool rezzing = false;   // someone is casting a rez on this body
    bool selfRes = false;   // soulstone or reincarnation ready
    bool fenced = false;    // a Realm lease or training owns it
};

// A living bot of the group on the body's map that could rez it now: out of combat, not
// casting, not in a vehicle or on a taxi, not fenced, knowing a rez spell.
struct GroupRezCandidate
{
    std::uint32_t guid = 0;
    float distance = 0;    // yards to the body
    float castRange = 0;   // the rez spell's range
    bool los = false;
    bool mounted = false;
    bool grounded = true;  // within kGroupRezGroundYards of the ground and not falling
};

struct GroupRezMemory
{
    std::optional<std::uint64_t> quietSince; // dead with the group out of combat
    std::optional<std::uint64_t> lastCast;
};

enum class GroupRezStep : std::uint8_t { None, Cast, Dismount, Release };

struct GroupRezDecision
{
    GroupRezStep step = GroupRezStep::None;
    std::uint32_t caster = 0;
};

// Combat or a fence restarts the release clock; a rez under way or waiting for an answer
// holds the release without restarting it. Cast when the nearest usable rezzer is within its
// spell's range, else put a mounted one down so playerbots walks it in. A bot outside an
// instance releases after releaseMs of quiet (0 never), so it runs back from the graveyard.
GroupRezDecision DecideGroupRez(GroupRezBody const& body, bool groupCombat,
    std::vector<GroupRezCandidate> const& candidates, GroupRezMemory& memory,
    std::uint64_t now, std::uint64_t releaseMs);
}
#endif
