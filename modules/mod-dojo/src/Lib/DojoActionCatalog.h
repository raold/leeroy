// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_DOJO_DOJO_ACTION_CATALOG_H
#define MOD_DOJO_DOJO_ACTION_CATALOG_H

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include <nlohmann/json.hpp>

// The v1 joint action catalog (D20) and its masks (D23). No AzerothCore includes: the runtime describes the kit's
// spells and reads what an actor knows into ActorKnowledge; everything here is a pure function of those, and
// leeroy_dojo.actions computes the same catalog, hash and masks from tests/fixtures/action_cases.json.
//
// An action is (ability, movement, facing), with entries[0] the noop that keeps whatever command is pending. Abilities
// are none, one cast per kit spell in manifest order, stop_cast, target_enemy, clear_target, attack_on and attack_off;
// a cast's target slot is fixed by its spell (the enemy, or centred on the caster). Movement is hold or one of eight
// directions relative to the actor's facing after the command's facing part; facing keeps, turns toward or turns away
// from the visible enemy. The executor applies a command's parts in the order stop-cast, hold, facing, ability,
// movement, so a move launched by the same command as a cast-time spell cancels it under the core's own rules.
namespace Dojo
{
inline constexpr std::string_view kActionSchema = "leeroy-dojo-actions-v1";
inline constexpr std::uint32_t kMoveHorizonMs = 250;   // one movement command runs this long at the ordinary speed

enum class AbilityKind : std::uint8_t
{
    None,
    Cast,
    StopCast,
    TargetEnemy,
    ClearTarget,
    AttackOn,
    AttackOff,
};

enum class TargetSlot : std::uint8_t
{
    None,
    Self,    // centred on the caster
    Enemy,   // the visible enemy, as an explicit unit target
};

enum class Movement : std::uint8_t
{
    Hold,
    Forward,
    ForwardLeft,
    Left,
    BackLeft,
    Back,
    BackRight,
    Right,
    ForwardRight,
};
inline constexpr std::size_t kMovements = 9;

enum class Facing : std::uint8_t
{
    Keep,
    Toward,
    Away,
};
inline constexpr std::size_t kFacings = 3;

// One kit spell as the runtime describes it from the spell data.
struct KitAbility
{
    std::uint32_t spellId = 0;
    std::string name;                        // the manifest's name, e.g. "Frost Nova"
    TargetSlot target = TargetSlot::Enemy;   // Self or Enemy
    bool needsFacing = false;                // the target must be in front of the caster
};

struct Ability
{
    AbilityKind kind = AbilityKind::None;
    std::string name;
    std::size_t kit = 0;                     // Cast: the index into ActionCatalog::kit
    TargetSlot target = TargetSlot::None;
    bool needsFacing = false;
};

struct CatalogEntry
{
    bool noop = false;
    std::size_t ability = 0;
    Movement movement = Movement::Hold;
    Facing facing = Facing::Keep;
    std::string name;                        // "noop" or "<ability>/<movement>/<facing>"
};

struct ActionCatalog
{
    std::vector<KitAbility> kit;
    std::vector<Ability> abilities;
    std::vector<CatalogEntry> entries;       // entries[0] is the noop
    std::string canonical;                   // the canonical JSON the hash covers
    std::string sha256;
};

// An empty kit, an id of 0, a target other than Self or Enemy, or two spells with the same or a reserved name is an
// error. Empty error on success.
std::optional<ActionCatalog> BuildCatalog(std::vector<KitAbility> const& kit, std::string& error);

std::optional<std::size_t> FindAction(ActionCatalog const& catalog, std::string_view name);

std::string_view MovementName(Movement movement);
std::string_view FacingName(Facing facing);
std::string_view TargetName(TargetSlot target);

// Radians counter-clockwise from the actor's facing (forward 0, left pi/2); hold is 0.
float MovementAngle(Movement movement);

// A direction with a backward component, which the client walks at its run-back speed.
bool IsBackward(Movement movement);

// The lower-case, underscore-separated ability name of a kit spell, e.g. "Frost Nova" -> "frost_nova".
std::string AbilityName(std::string_view spellName);

nlohmann::json CatalogToJson(ActionCatalog const& catalog);

// What one actor knows of its own state and the enemy slot when it decides: since plan Task 7, the newest percept that
// has matured under the perceptual delay (DojoPerception.h), never the current authoritative state.
struct KitKnowledge
{
    bool known = false;                      // in the actor's active spellbook
    std::uint32_t cooldownMs = 0;            // the spell's own cooldown remaining
    std::uint32_t gcdMs = 0;                 // the global cooldown remaining for its category
    bool costKnown = false;
    std::uint32_t cost = 0;                  // mana at the current modifiers
    bool rangeKnown = false;
    float maxRange = 0.0f;                   // yards
};

struct ActorKnowledge
{
    bool alive = false;
    std::uint32_t mana = 0;
    std::uint32_t manaRegenBound = 0;        // the most mana one regeneration tick can add
    bool casting = false;                    // a cast or channel in progress
    std::uint32_t castRemainingMs = 0;
    bool castQueued = false;                 // a request waiting in the core's spell queue
    std::vector<KitKnowledge> kit;           // in catalog kit order
    bool enemyKnown = false;                 // the enemy slot is visible
    bool enemyAlive = false;
    float enemyDistance = 0.0f;              // yards
};

struct MaskRules
{
    std::uint32_t perceptualDelayMs = 0;     // D30: the knowledge is this old when the actor decides (Task 7: 100)
    std::uint32_t commandDelayMs = 150;      // D30: a command executes this long after the decision
    std::uint32_t queueWindowMs = 400;       // the core accepts a cast this close to ready into its spell queue
    float rangeSlackYards = 3.0f;            // both actors may move before the command executes
};

// D23: only choices that cannot succeed are masked, and the noop never is. A wait (cooldown, global cooldown, a cast
// in progress) masks only when it outlasts the knowledge's age, the command delay and the spell queue window together
// (D19: the mask answers for what the actor knew, which is perceptualDelayMs old); mana masks only when one
// regeneration tick could not cover the cost; range masks beyond the slack; line of sight, facing with keep, roots,
// stuns and immunity are left to execution, which can reject. Casting a front-facing spell while turning away is the
// one combination masked for every state.
struct MaskResult
{
    std::vector<std::uint8_t> mask;          // one 0 or 1 per catalog entry
    std::vector<std::string> abilityReasons; // per ability: empty when selectable, else why not
    std::vector<std::string> facingReasons;  // per facing
};

MaskResult ComputeMask(ActionCatalog const& catalog, ActorKnowledge const& knowledge, MaskRules const& rules);
}

#endif
