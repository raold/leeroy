// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_GROUP_REZ_POLICY_H
#define MOD_LEEROY_GROUP_REZ_POLICY_H

#include <array>
#include <cstdint>
#include <map>
#include <optional>
#include <vector>

namespace Leeroy::Guild
{
// A rezzer this far from a body can be walked in. Stock playerbots looks only within
// SpellDistance * 2 (57 yards) and in sight, and inside a dungeon or raid it never releases a
// dead bot while he is on the map, so a body left farther out lay there for good.
inline constexpr float kGroupRezReachYards = 100.0f;
// A fight (an engaged enemy, or a member engaged with one) this close to a body keeps it waiting.
inline constexpr float kGroupRezCombatYards = 100.0f;
// A member counts as engaged only with a living enemy this close to it. Inside a dungeon the core
// never ends combat by distance, so a member can stay flagged by a creature a wing away.
inline constexpr float kGroupRezEngagedYards = 80.0f;
// A mounted rezzer this close to the ground can be put down without a fall.
inline constexpr float kGroupRezGroundYards = 3.0f;
inline constexpr std::uint64_t kGroupRezRetryMs = 3000;
// A rezzer whose cast was refused is passed over, for every body, for this long, so another can
// try. A transient refusal (the global cooldown) waits kGroupRezRetryMs instead.
inline constexpr std::uint64_t kGroupRezRefusedMs = 30000;
// A rezzer short of mana for its rez is asked to drink again after this long.
inline constexpr std::uint64_t kGroupRezDrinkRetryMs = 25000;
// A walk-in that has not reached range and sight by then gives that rezzer up for the body...
inline constexpr std::uint64_t kGroupRezApproachMs = 25000;
// ...for this long.
inline constexpr std::uint64_t kGroupRezUnreachableMs = 60000;
// group_rez_blocked is logged at most once per reason in this window.
inline constexpr std::uint64_t kGroupRezBlockLogMs = 60000;
inline constexpr std::uint32_t kGroupRezDefaultReleaseSeconds = 60;

// Out-of-combat rez spells, highest rank first: Redemption, Resurrection, Ancestral Spirit, Revive.
// Null for a class without one.
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

// A living bot of the group on the body's map that knows a rez spell.
struct GroupRezCandidate
{
    std::uint32_t guid = 0;
    float distance = 0;    // yards to the body
    float castRange = 0;   // the rez spell's range
    bool los = false;
    bool mounted = false;  // on a mount or in Flight Form
    bool grounded = true;  // within kGroupRezGroundYards of the ground and not falling
    std::uint32_t mana = 0;
    std::uint32_t manaCost = 0; // what its rez costs it now
    bool combat = false;   // still in combat, so it cannot cast an out-of-combat rez
    bool busy = false;     // casting, in a vehicle, on a taxi, teleporting or already used
    bool fenced = false;   // a Realm lease or training owns it
};

// Per rezzer, across every body: a refusal or a drink applies to all of them.
struct GroupRezCaster
{
    std::optional<std::uint64_t> refusedUntil;
    std::optional<std::uint64_t> drinkAskedAt;
};
using GroupRezCasters = std::map<std::uint32_t, GroupRezCaster>;

// Per body.
struct GroupRezMemory
{
    std::optional<std::uint64_t> quietSince; // dead with the group out of combat
    std::optional<std::uint64_t> lastCast;
    std::optional<std::uint32_t> approachCaster; // the rezzer walking in to this body
    std::uint64_t approachSince = 0;
    std::map<std::uint32_t, std::uint64_t> unreachableAt; // rezzer -> when its walk-in gave up
};

// transient: a global cooldown or a cast already in progress, retried after kGroupRezRetryMs.
void NoteGroupRezRefused(GroupRezCasters& casters, std::uint32_t caster, std::uint64_t now, bool transient);
void NoteGroupRezDrinkAsked(GroupRezCasters& casters, std::uint32_t caster, std::uint64_t now);
// The rezzer has no navmesh path to within its spell's range of the body: given up for it.
void NoteGroupRezUnreachable(GroupRezMemory& memory, std::uint32_t caster, std::uint64_t now);
// Drops caster entries with nothing left to remember.
void PruneGroupRezCasters(GroupRezCasters& casters, std::uint64_t now);

enum class GroupRezStep : std::uint8_t { None, Cast, Approach, Drink, Release };

// Why a body got no rez this second; None while one is cast, walked in or under way.
enum class GroupRezBlock : std::uint8_t
{
    None,
    Combat,          // a fight near the body
    Fenced,          // a Realm lease or training owns the body
    AwaitingAnswer,  // a rez landed and waits for an answer
    SelfRes,         // the body has a soulstone or reincarnation to use
    NoCaster,        // no living rezzer of his group on the body's map
    NoCasterInRange, // none within kGroupRezReachYards
    AllOom,          // every free rezzer in reach lacks the mana for its rez (they are asked to drink)
    Unreachable,     // every free rezzer in reach failed to walk in to range and sight
    CasterCombat,    // every rezzer in reach is still in combat
    CastersBusy,     // every rezzer in reach is casting, travelling, airborne, fenced or passed over
};
char const* GroupRezBlockCode(GroupRezBlock block);

struct GroupRezDecision
{
    GroupRezStep step = GroupRezStep::None;
    std::uint32_t caster = 0;
    GroupRezBlock block = GroupRezBlock::None;
};

// A fight or a fence restarts the release clock; a rez under way or waiting for an answer
// holds the release without restarting it. A bot outside an instance releases after releaseMs
// of quiet (0 never), so it runs back from the graveyard. Otherwise, of the free rezzers with
// the mana for their rez within kGroupRezReachYards: the one already walking in keeps the body;
// else the nearest within its spell's range and in sight casts; else the nearest walks in. With
// none, the nearest free one short of mana and not asked lately is asked to drink.
GroupRezDecision DecideGroupRez(GroupRezBody const& body, bool fightNear,
    std::vector<GroupRezCandidate> const& candidates, GroupRezMemory& memory, GroupRezCasters const& casters,
    std::uint64_t now, std::uint64_t releaseMs);

// True when group_rez_blocked for this reason is due (once per kGroupRezBlockLogMs); records it.
bool GroupRezBlockLogDue(std::map<GroupRezBlock, std::uint64_t>& logged, GroupRezBlock block, std::uint64_t now);
}
#endif
