// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#include "DojoActionCatalog.h"

#include "DojoManifest.h"
#include "DojoSha256.h"

#include <array>
#include <set>

using nlohmann::json;

namespace Dojo
{
namespace
{
constexpr std::array<Movement, kMovements> kAllMovements = {Movement::Hold, Movement::Forward, Movement::ForwardLeft,
    Movement::Left, Movement::BackLeft, Movement::Back, Movement::BackRight, Movement::Right, Movement::ForwardRight};
constexpr std::array<Facing, kFacings> kAllFacings = {Facing::Keep, Facing::Toward, Facing::Away};
constexpr float kPi = 3.14159265358979323846f;

// The fixed abilities after the kit's casts, in catalog order.
struct FixedAbility
{
    AbilityKind kind;
    char const* name;
    TargetSlot target;
};
constexpr std::array<FixedAbility, 5> kFixedAbilities = {{
    {AbilityKind::StopCast, "stop_cast", TargetSlot::None},
    {AbilityKind::TargetEnemy, "target_enemy", TargetSlot::Enemy},
    {AbilityKind::ClearTarget, "clear_target", TargetSlot::None},
    {AbilityKind::AttackOn, "attack_on", TargetSlot::Enemy},
    {AbilityKind::AttackOff, "attack_off", TargetSlot::None},
}};
}

std::string_view MovementName(Movement movement)
{
    switch (movement)
    {
        case Movement::Hold: return "hold";
        case Movement::Forward: return "forward";
        case Movement::ForwardLeft: return "forward_left";
        case Movement::Left: return "left";
        case Movement::BackLeft: return "back_left";
        case Movement::Back: return "back";
        case Movement::BackRight: return "back_right";
        case Movement::Right: return "right";
        case Movement::ForwardRight: return "forward_right";
    }
    return "?";
}

std::string_view FacingName(Facing facing)
{
    switch (facing)
    {
        case Facing::Keep: return "keep";
        case Facing::Toward: return "toward";
        case Facing::Away: return "away";
    }
    return "?";
}

std::string_view TargetName(TargetSlot target)
{
    switch (target)
    {
        case TargetSlot::None: return "none";
        case TargetSlot::Self: return "self";
        case TargetSlot::Enemy: return "enemy";
    }
    return "?";
}

float MovementAngle(Movement movement)
{
    switch (movement)
    {
        case Movement::Hold:
        case Movement::Forward: return 0.0f;
        case Movement::ForwardLeft: return kPi / 4.0f;
        case Movement::Left: return kPi / 2.0f;
        case Movement::BackLeft: return 3.0f * kPi / 4.0f;
        case Movement::Back: return kPi;
        case Movement::BackRight: return -3.0f * kPi / 4.0f;
        case Movement::Right: return -kPi / 2.0f;
        case Movement::ForwardRight: return -kPi / 4.0f;
    }
    return 0.0f;
}

bool IsBackward(Movement movement)
{
    return movement == Movement::BackLeft || movement == Movement::Back || movement == Movement::BackRight;
}

std::string AbilityName(std::string_view spellName)
{
    std::string out;
    bool gap = false;
    for (char const c : spellName)
    {
        bool const alnum = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9');
        if (!alnum)
        {
            gap = !out.empty();
            continue;
        }
        if (gap)
            out += '_';
        gap = false;
        out += (c >= 'A' && c <= 'Z') ? char(c - 'A' + 'a') : c;
    }
    return out;
}

json CatalogToJson(ActionCatalog const& catalog)
{
    json kit = json::array();
    for (KitAbility const& k : catalog.kit)
        kit.push_back({{"spell_id", k.spellId}, {"name", k.name}, {"target", std::string(TargetName(k.target))},
            {"facing", k.needsFacing}});
    json abilities = json::array();
    for (Ability const& a : catalog.abilities)
        abilities.push_back(a.name);
    json movements = json::array();
    for (Movement const m : kAllMovements)
        movements.push_back(std::string(MovementName(m)));
    json facings = json::array();
    for (Facing const f : kAllFacings)
        facings.push_back(std::string(FacingName(f)));
    return {{"schema", std::string(kActionSchema)}, {"move_horizon_ms", kMoveHorizonMs}, {"kit", kit},
        {"abilities", abilities}, {"movements", movements}, {"facings", facings}, {"size", catalog.entries.size()}};
}

std::optional<ActionCatalog> BuildCatalog(std::vector<KitAbility> const& kit, std::string& error)
{
    error.clear();
    if (kit.empty())
    {
        error = "the kit is empty";
        return std::nullopt;
    }
    ActionCatalog catalog;
    catalog.kit = kit;
    std::set<std::string> names = {"none", "noop"};
    for (FixedAbility const& f : kFixedAbilities)
        names.insert(f.name);
    catalog.abilities.push_back({AbilityKind::None, "none", 0, TargetSlot::None, false});
    for (std::size_t i = 0; i < kit.size(); ++i)
    {
        KitAbility const& k = kit[i];
        std::string const name = AbilityName(k.name);
        if (!k.spellId || name.empty() || (k.target != TargetSlot::Self && k.target != TargetSlot::Enemy))
        {
            error = "kit spell " + std::to_string(i) + " needs an id, a name and a self or enemy target";
            return std::nullopt;
        }
        if (!names.insert(name).second)
        {
            error = "kit spell " + k.name + " repeats or reuses the ability name " + name;
            return std::nullopt;
        }
        catalog.abilities.push_back({AbilityKind::Cast, name, i, k.target, k.needsFacing});
    }
    for (FixedAbility const& f : kFixedAbilities)
        catalog.abilities.push_back({f.kind, f.name, 0, f.target, false});

    catalog.entries.push_back({true, 0, Movement::Hold, Facing::Keep, "noop"});
    for (std::size_t a = 0; a < catalog.abilities.size(); ++a)
        for (Movement const m : kAllMovements)
            for (Facing const f : kAllFacings)
                catalog.entries.push_back({false, a, m, f,
                    catalog.abilities[a].name + "/" + std::string(MovementName(m)) + "/" + std::string(FacingName(f))});
    catalog.canonical = CanonicalJson(CatalogToJson(catalog));
    catalog.sha256 = Sha256Hex(catalog.canonical);
    return catalog;
}

std::optional<std::size_t> FindAction(ActionCatalog const& catalog, std::string_view name)
{
    for (std::size_t i = 0; i < catalog.entries.size(); ++i)
        if (catalog.entries[i].name == name)
            return i;
    return std::nullopt;
}

MaskResult ComputeMask(ActionCatalog const& catalog, ActorKnowledge const& knowledge, MaskRules const& rules)
{
    MaskResult result;
    std::uint64_t const wait = std::uint64_t(rules.perceptualDelayMs) + rules.commandDelayMs + rules.queueWindowMs;
    result.abilityReasons.resize(catalog.abilities.size());
    for (std::size_t a = 0; a < catalog.abilities.size(); ++a)
    {
        Ability const& ability = catalog.abilities[a];
        std::string& why = result.abilityReasons[a];
        if (!knowledge.alive)
        {
            why = "dead";
            continue;
        }
        bool const enemy = ability.target == TargetSlot::Enemy;
        switch (ability.kind)
        {
            case AbilityKind::None:
            case AbilityKind::ClearTarget:
            case AbilityKind::AttackOff:
                break;
            case AbilityKind::StopCast:
                if (!knowledge.casting && !knowledge.castQueued)
                    why = "not casting";
                break;
            case AbilityKind::TargetEnemy:
                if (!knowledge.enemyKnown)
                    why = "no visible enemy";
                break;
            case AbilityKind::AttackOn:
                if (!knowledge.enemyKnown)
                    why = "no visible enemy";
                else if (!knowledge.enemyAlive)
                    why = "enemy dead";
                break;
            case AbilityKind::Cast:
            {
                KitKnowledge const known = ability.kit < knowledge.kit.size() ? knowledge.kit[ability.kit] : KitKnowledge{};
                if (!known.known)
                    why = "not known";
                else if (enemy && !knowledge.enemyKnown)
                    why = "no visible enemy";
                else if (enemy && !knowledge.enemyAlive)
                    why = "enemy dead";
                else if (enemy && known.rangeKnown && knowledge.enemyDistance > known.maxRange + rules.rangeSlackYards)
                    why = "out of range";
                else if (known.cooldownMs > wait)
                    why = "cooldown";
                else if (known.gcdMs > wait)
                    why = "global cooldown";
                else if (knowledge.casting && knowledge.castRemainingMs > wait)
                    why = "casting";
                else if (known.costKnown && std::uint64_t(knowledge.mana) + knowledge.manaRegenBound < known.cost)
                    why = "mana";
                break;
            }
        }
    }
    result.facingReasons.resize(kFacings);
    for (std::size_t f = 0; f < kFacings; ++f)
    {
        if (!knowledge.alive)
            result.facingReasons[f] = "dead";
        else if (kAllFacings[f] != Facing::Keep && !knowledge.enemyKnown)
            result.facingReasons[f] = "no visible enemy";
    }
    result.mask.assign(catalog.entries.size(), 0);
    for (std::size_t i = 0; i < catalog.entries.size(); ++i)
    {
        CatalogEntry const& e = catalog.entries[i];
        if (e.noop)
        {
            result.mask[i] = 1;
            continue;
        }
        Ability const& ability = catalog.abilities[e.ability];
        bool open = result.abilityReasons[e.ability].empty() && result.facingReasons[std::size_t(e.facing)].empty();
        if (ability.kind == AbilityKind::Cast && ability.needsFacing && e.facing == Facing::Away)
            open = false;
        result.mask[i] = open ? 1 : 0;
    }
    return result;
}
}
