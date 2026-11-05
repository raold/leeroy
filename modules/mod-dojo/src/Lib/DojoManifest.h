// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_DOJO_DOJO_MANIFEST_H
#define MOD_DOJO_DOJO_MANIFEST_H

#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

// A dojo scenario manifest (dojo/scenarios/*.json): the frozen arena, fixture actors and their shared loadout. No
// AzerothCore includes: this checks the document's shape and computes its canonical form and SHA-256, the same bytes
// leeroy_dojo.scenario hashes in Python. Whether each id exists in the installed data is the runtime's check
// (src/Runtime/DojoScenario.cpp). Unknown keys, wrong types and out-of-range values are refused, never defaulted.
namespace Dojo
{
inline constexpr std::string_view kScenarioKind = "leeroy-dojo-scenario";
inline constexpr std::size_t kMaxManifestBytes = 256 * 1024;
inline constexpr std::size_t kEquipmentSlots = 19;   // EQUIPMENT_SLOT_HEAD .. EQUIPMENT_SLOT_TABARD
inline constexpr std::size_t kGlyphSlots = 6;        // MAX_GLYPH_SLOT_INDEX
inline constexpr std::size_t kScenarioActors = 2;
inline constexpr std::uint32_t kMaxLevel = 80;

struct Point
{
    double x = 0, y = 0, z = 0, o = 0;
};

struct NamedId
{
    std::uint32_t id = 0;
    std::string name;
};

struct KitSpell
{
    std::uint32_t spellId = 0;
    std::string name;
    std::uint32_t rank = 0;
};

struct TalentPick
{
    std::uint32_t talentId = 0;
    std::uint32_t rank = 0;      // 1-based
    std::uint32_t spellId = 0;   // the talent's spell at that rank
    std::string name;
};

struct GlyphPick
{
    std::uint32_t slot = 0;      // player glyph index 0-5
    std::uint32_t glyphId = 0;   // GlyphProperties.dbc
    std::uint32_t spellId = 0;
    std::string name;
};

struct EquipPick
{
    std::uint32_t slot = 0;      // EquipmentSlots
    std::uint32_t itemId = 0;
    std::string name;
};

struct ActorSpec
{
    std::uint32_t slot = 0;
    std::string name;
    std::string team;            // "alliance" (gold) or "horde" (green): the arena side, not the faction
    Point start;
};

struct Bounds
{
    double x = 0, y = 0, radius = 0, zMin = 0, zMax = 0;
};

struct ArenaSpec
{
    std::uint32_t mapId = 0;
    std::string map;
    std::uint32_t battlegroundTypeId = 0;
    std::string battleground;
    std::uint32_t arenaType = 0;
    Bounds bounds;
};

struct RulesSpec
{
    bool rated = false;
    std::uint32_t prepSeconds = 0;
    std::uint32_t matchSeconds = 0;
};

struct FixtureSpec
{
    std::string account;
    std::uint32_t stagingMap = 0;
    Point staging;
};

struct Appearance
{
    std::uint32_t skin = 0, face = 0, hairStyle = 0, hairColor = 0, facialHair = 0;
};

struct Loadout
{
    std::uint32_t level = 0;
    NamedId race;
    NamedId cls;
    std::uint32_t gender = 0;
    Appearance appearance;
    std::vector<TalentPick> talents;
    std::vector<GlyphPick> glyphs;
    std::vector<EquipPick> equipment;
    std::vector<KitSpell> kit;
    std::vector<std::uint32_t> spellbook;   // every active spell the actor knows, sorted
    std::uint32_t maxHealth = 0;
    std::uint32_t maxMana = 0;
};

struct Manifest
{
    std::string id;
    std::string curriculum;
    std::uint32_t version = 0;
    std::string description;
    RulesSpec rules;
    ArenaSpec arena;
    FixtureSpec fixture;
    std::array<ActorSpec, kScenarioActors> actors;
    Loadout loadout;
    std::string canonical;   // the canonical JSON the hash covers
    std::string sha256;      // lower-case hex of SHA-256(canonical)
};

struct ManifestParse
{
    std::optional<Manifest> manifest;
    std::vector<std::string> errors;
};

// Sorted keys, no whitespace, non-ASCII escaped: Python's json.dumps(sort_keys=True, separators=(",", ":")).
std::string CanonicalJson(nlohmann::json const& value);

ManifestParse ParseManifest(std::string_view text);

// What a fixture actor actually is, read from the game by the runtime. Positions and the arena side are not part of
// it: two actors with the same loadout have equal observations.
struct Observation
{
    std::uint32_t level = 0, race = 0, cls = 0, gender = 0;
    std::vector<std::pair<std::uint32_t, std::uint32_t>> talents;   // (talent id, rank) held, sorted
    std::uint32_t freeTalentPoints = 0;
    std::array<std::uint32_t, kGlyphSlots> glyphs{};
    std::array<std::uint32_t, kEquipmentSlots> equipment{};
    std::uint32_t itemExtras = 0;    // enchantments, gems or random properties on equipped items
    std::uint32_t otherItems = 0;    // items anywhere but the equipment slots
    std::vector<std::uint32_t> spellbook;   // active spells known, sorted
    std::uint32_t health = 0, maxHealth = 0, mana = 0, maxMana = 0;
    std::vector<std::uint32_t> auras;       // applied aura spell ids, sorted, arena team markers excluded
    std::vector<std::pair<std::string, std::int64_t>> stats;   // derived stats, sorted by name
};

nlohmann::json ObservationToJson(Observation const& observation);

// SHA-256 of the observation's canonical JSON: equal for equal loadouts.
std::string Fingerprint(Observation const& observation);

// Every way the observation differs from the manifest's frozen loadout and its full initial health and mana.
std::vector<std::string> CheckLoadout(Loadout const& loadout, Observation const& observation);

// Every field in which two actors differ.
std::vector<std::string> CompareActors(Observation const& a, Observation const& b);
}

#endif
