// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#include "DojoManifest.h"

#include "DojoSha256.h"

#include <algorithm>
#include <cmath>
#include <initializer_list>
#include <iterator>
#include <set>

using nlohmann::json;

namespace Dojo
{
namespace
{
constexpr double kMaxCoordinate = 1e6;

bool IsPlainText(std::string const& s, std::size_t max)
{
    if (s.empty() || s.size() > max)
        return false;
    return std::all_of(s.begin(), s.end(), [](char c) { return c >= 0x20 && c < 0x7f; });
}

bool IsSlug(std::string const& s)
{
    if (s.empty() || s.size() > 64)
        return false;
    return std::all_of(s.begin(), s.end(), [](char c) { return (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_'; });
}

// AzerothCore's default name rule for a synthetic character: 2-12 letters, capitalised.
bool IsCharacterName(std::string const& s)
{
    if (s.size() < 2 || s.size() > 12 || !(s[0] >= 'A' && s[0] <= 'Z'))
        return false;
    return std::all_of(s.begin() + 1, s.end(), [](char c) { return c >= 'a' && c <= 'z'; });
}

bool IsAccountName(std::string const& s)
{
    if (s.size() < 3 || s.size() > 16)
        return false;
    return std::all_of(s.begin(), s.end(), [](char c) { return (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9'); });
}

// Collects errors with a JSON path; every accessor refuses rather than defaults.
class Reader
{
public:
    std::vector<std::string> errors;

    void Fail(std::string const& path, std::string const& why) { errors.push_back(path + ": " + why); }

    bool Object(json const& v, std::string const& path, std::initializer_list<char const*> keys)
    {
        if (!v.is_object())
        {
            Fail(path, "must be an object");
            return false;
        }
        std::set<std::string> allowed(keys.begin(), keys.end());
        bool ok = true;
        for (auto const& [key, _] : v.items())
            if (!allowed.count(key))
            {
                Fail(path + "." + key, "is not a known key");
                ok = false;
            }
        for (char const* key : keys)
            if (!v.contains(key))
            {
                Fail(path + "." + key, "is missing");
                ok = false;
            }
        return ok;
    }

    std::uint32_t Uint(json const& v, std::string const& path, std::uint64_t min, std::uint64_t max)
    {
        if (!v.is_number_unsigned() && !(v.is_number_integer() && v.get<std::int64_t>() >= 0))
        {
            Fail(path, "must be a non-negative integer");
            return 0;
        }
        std::uint64_t const n = v.get<std::uint64_t>();
        if (n < min || n > max)
        {
            Fail(path, "must be " + std::to_string(min) + "-" + std::to_string(max));
            return 0;
        }
        return std::uint32_t(n);
    }

    double Number(json const& v, std::string const& path)
    {
        if (!v.is_number() || v.is_boolean())
        {
            Fail(path, "must be a number");
            return 0;
        }
        double const d = v.get<double>();
        if (!std::isfinite(d) || std::fabs(d) > kMaxCoordinate)
        {
            Fail(path, "must be finite and within 1e6");
            return 0;
        }
        // At most six decimals, so Python and nlohmann print the same shortest form.
        if (v.is_number_float() && std::round(d * 1e6) / 1e6 != d)
        {
            Fail(path, "may have at most six decimals");
            return 0;
        }
        return d;
    }

    std::string Text(json const& v, std::string const& path, std::size_t max = 128)
    {
        if (!v.is_string() || !IsPlainText(v.get<std::string>(), max))
        {
            Fail(path, "must be printable ASCII text of 1-" + std::to_string(max) + " characters");
            return {};
        }
        return v.get<std::string>();
    }

    bool Bool(json const& v, std::string const& path)
    {
        if (!v.is_boolean())
        {
            Fail(path, "must be true or false");
            return false;
        }
        return v.get<bool>();
    }

    Point Pos(json const& v, std::string const& path, bool withMap = false)
    {
        Point p;
        bool const ok = withMap ? Object(v, path, {"map_id", "x", "y", "z", "o"}) : Object(v, path, {"x", "y", "z", "o"});
        if (!ok)
            return p;
        p.x = Number(v["x"], path + ".x");
        p.y = Number(v["y"], path + ".y");
        p.z = Number(v["z"], path + ".z");
        p.o = Number(v["o"], path + ".o");
        if (p.o < 0 || p.o >= 2 * 3.14159266)
            Fail(path + ".o", "must be an orientation in [0, 2 pi)");
        return p;
    }

    NamedId Named(json const& v, std::string const& path, std::uint32_t maxId)
    {
        NamedId n;
        if (!Object(v, path, {"id", "name"}))
            return n;
        n.id = Uint(v["id"], path + ".id", 1, maxId);
        n.name = Text(v["name"], path + ".name");
        return n;
    }
};

void ReadLoadout(Reader& r, json const& v, Loadout& l)
{
    std::string const path = "loadout";
    if (!r.Object(v, path, {"level", "race", "class", "gender", "appearance", "talents", "glyphs", "equipment", "kit",
            "spellbook", "initial"}))
        return;
    l.level = r.Uint(v["level"], path + ".level", 1, kMaxLevel);
    l.race = r.Named(v["race"], path + ".race", 11);
    l.cls = r.Named(v["class"], path + ".class", 11);
    l.gender = r.Uint(v["gender"], path + ".gender", 0, 1);

    json const& a = v["appearance"];
    if (r.Object(a, path + ".appearance", {"skin", "face", "hair_style", "hair_color", "facial_hair"}))
    {
        l.appearance.skin = r.Uint(a["skin"], path + ".appearance.skin", 0, 255);
        l.appearance.face = r.Uint(a["face"], path + ".appearance.face", 0, 255);
        l.appearance.hairStyle = r.Uint(a["hair_style"], path + ".appearance.hair_style", 0, 255);
        l.appearance.hairColor = r.Uint(a["hair_color"], path + ".appearance.hair_color", 0, 255);
        l.appearance.facialHair = r.Uint(a["facial_hair"], path + ".appearance.facial_hair", 0, 255);
    }

    if (!v["talents"].is_array())
        r.Fail(path + ".talents", "must be an array");
    else
    {
        std::set<std::uint32_t> ids;
        std::uint32_t points = 0;
        for (std::size_t i = 0; i < v["talents"].size(); ++i)
        {
            json const& t = v["talents"][i];
            std::string const p = path + ".talents[" + std::to_string(i) + "]";
            if (!r.Object(t, p, {"talent_id", "rank", "spell_id", "name"}))
                continue;
            TalentPick pick{r.Uint(t["talent_id"], p + ".talent_id", 1, 0xFFFFFF), r.Uint(t["rank"], p + ".rank", 1, 5),
                r.Uint(t["spell_id"], p + ".spell_id", 1, 0xFFFFFF), r.Text(t["name"], p + ".name")};
            if (!ids.insert(pick.talentId).second)
                r.Fail(p + ".talent_id", "repeats a talent");
            points += pick.rank;
            l.talents.push_back(pick);
        }
        if (l.level >= 10 && points > l.level - 9)
            r.Fail(path + ".talents", "spend " + std::to_string(points) + " points; level " + std::to_string(l.level)
                + " has " + std::to_string(l.level - 9));
    }

    if (!v["glyphs"].is_array())
        r.Fail(path + ".glyphs", "must be an array");
    else
    {
        std::set<std::uint32_t> slots;
        for (std::size_t i = 0; i < v["glyphs"].size(); ++i)
        {
            json const& g = v["glyphs"][i];
            std::string const p = path + ".glyphs[" + std::to_string(i) + "]";
            if (!r.Object(g, p, {"slot", "glyph_id", "spell_id", "name"}))
                continue;
            GlyphPick pick{r.Uint(g["slot"], p + ".slot", 0, kGlyphSlots - 1), r.Uint(g["glyph_id"], p + ".glyph_id", 1, 0xFFFF),
                r.Uint(g["spell_id"], p + ".spell_id", 1, 0xFFFFFF), r.Text(g["name"], p + ".name")};
            if (!slots.insert(pick.slot).second)
                r.Fail(p + ".slot", "repeats a glyph slot");
            l.glyphs.push_back(pick);
        }
    }

    if (!v["equipment"].is_array())
        r.Fail(path + ".equipment", "must be an array");
    else
    {
        std::set<std::uint32_t> slots;
        for (std::size_t i = 0; i < v["equipment"].size(); ++i)
        {
            json const& e = v["equipment"][i];
            std::string const p = path + ".equipment[" + std::to_string(i) + "]";
            if (!r.Object(e, p, {"slot", "item_id", "name"}))
                continue;
            EquipPick pick{r.Uint(e["slot"], p + ".slot", 0, kEquipmentSlots - 1), r.Uint(e["item_id"], p + ".item_id", 1, 0xFFFFFF),
                r.Text(e["name"], p + ".name")};
            if (!slots.insert(pick.slot).second)
                r.Fail(p + ".slot", "repeats an equipment slot");
            l.equipment.push_back(pick);
        }
    }

    if (!v["kit"].is_array() || v["kit"].empty())
        r.Fail(path + ".kit", "must be a non-empty array");
    else
    {
        std::set<std::uint32_t> ids;
        for (std::size_t i = 0; i < v["kit"].size(); ++i)
        {
            json const& k = v["kit"][i];
            std::string const p = path + ".kit[" + std::to_string(i) + "]";
            if (!r.Object(k, p, {"spell_id", "name", "rank"}))
                continue;
            KitSpell spell{r.Uint(k["spell_id"], p + ".spell_id", 1, 0xFFFFFF), r.Text(k["name"], p + ".name"),
                r.Uint(k["rank"], p + ".rank", 1, 32)};
            if (!ids.insert(spell.spellId).second)
                r.Fail(p + ".spell_id", "repeats a kit spell");
            l.kit.push_back(spell);
        }
    }

    if (!v["spellbook"].is_array() || v["spellbook"].empty())
        r.Fail(path + ".spellbook", "must be a non-empty array");
    else
    {
        for (std::size_t i = 0; i < v["spellbook"].size(); ++i)
            l.spellbook.push_back(r.Uint(v["spellbook"][i], path + ".spellbook[" + std::to_string(i) + "]", 1, 0xFFFFFF));
        if (!std::is_sorted(l.spellbook.begin(), l.spellbook.end())
            || std::adjacent_find(l.spellbook.begin(), l.spellbook.end()) != l.spellbook.end())
            r.Fail(path + ".spellbook", "must be sorted without repeats");
        for (KitSpell const& k : l.kit)
            if (!std::binary_search(l.spellbook.begin(), l.spellbook.end(), k.spellId))
                r.Fail(path + ".spellbook", "lacks the kit spell " + std::to_string(k.spellId));
    }

    json const& init = v["initial"];
    if (r.Object(init, path + ".initial", {"max_health", "max_mana"}))
    {
        l.maxHealth = r.Uint(init["max_health"], path + ".initial.max_health", 1, 10000000);
        l.maxMana = r.Uint(init["max_mana"], path + ".initial.max_mana", 1, 10000000);
    }
}

std::string Join(std::vector<std::uint32_t> const& v)
{
    std::string out;
    for (std::uint32_t const n : v)
        out += (out.empty() ? "" : ",") + std::to_string(n);
    return out;
}
}

std::string CanonicalJson(json const& value)
{
    // nlohmann's objects are std::map, so keys come out sorted; ensure_ascii as Python's default.
    return value.dump(-1, ' ', true);
}

ManifestParse ParseManifest(std::string_view text)
{
    ManifestParse out;
    if (text.size() > kMaxManifestBytes)
    {
        out.errors.push_back("manifest is larger than " + std::to_string(kMaxManifestBytes) + " bytes");
        return out;
    }
    json doc = json::parse(text.begin(), text.end(), nullptr, false);
    if (doc.is_discarded())
    {
        out.errors.push_back("manifest is not valid JSON");
        return out;
    }
    Reader r;
    Manifest m;
    if (r.Object(doc, "manifest", {"kind", "format", "id", "curriculum", "version", "description", "rules", "arena",
            "fixture", "actors", "loadout"}))
    {
        if (!doc["kind"].is_string() || doc["kind"].get<std::string>() != kScenarioKind)
            r.Fail("kind", "must be \"" + std::string(kScenarioKind) + "\"");
        r.Uint(doc["format"], "format", 1, 1);
        m.id = doc["id"].is_string() ? doc["id"].get<std::string>() : "";
        if (!IsSlug(m.id))
            r.Fail("id", "must be 1-64 of a-z 0-9 _");
        m.curriculum = doc["curriculum"].is_string() ? doc["curriculum"].get<std::string>() : "";
        if (!IsSlug(m.curriculum))
            r.Fail("curriculum", "must be 1-64 of a-z 0-9 _");
        m.version = r.Uint(doc["version"], "version", 1, 1000000);
        m.description = r.Text(doc["description"], "description", 512);

        json const& rules = doc["rules"];
        if (r.Object(rules, "rules", {"rated", "prep_seconds", "match_seconds"}))
        {
            m.rules.rated = r.Bool(rules["rated"], "rules.rated");
            if (m.rules.rated)
                r.Fail("rules.rated", "must be false: a training arena never touches rating");
            m.rules.prepSeconds = r.Uint(rules["prep_seconds"], "rules.prep_seconds", 0, 120);
            m.rules.matchSeconds = r.Uint(rules["match_seconds"], "rules.match_seconds", 1, 3600);
        }

        json const& arena = doc["arena"];
        if (r.Object(arena, "arena", {"map_id", "map", "battleground_type_id", "battleground", "arena_type", "bounds"}))
        {
            m.arena.mapId = r.Uint(arena["map_id"], "arena.map_id", 1, 0xFFFF);
            m.arena.map = r.Text(arena["map"], "arena.map");
            m.arena.battlegroundTypeId = r.Uint(arena["battleground_type_id"], "arena.battleground_type_id", 1, 0xFFFF);
            m.arena.battleground = r.Text(arena["battleground"], "arena.battleground");
            m.arena.arenaType = r.Uint(arena["arena_type"], "arena.arena_type", 2, 5);
            if (m.arena.arenaType != 2 && m.arena.arenaType != 3 && m.arena.arenaType != 5)
                r.Fail("arena.arena_type", "must be a stock arena type (2, 3 or 5)");
            json const& b = arena["bounds"];
            if (r.Object(b, "arena.bounds", {"x", "y", "radius", "z_min", "z_max"}))
            {
                m.arena.bounds = {r.Number(b["x"], "arena.bounds.x"), r.Number(b["y"], "arena.bounds.y"),
                    r.Number(b["radius"], "arena.bounds.radius"), r.Number(b["z_min"], "arena.bounds.z_min"),
                    r.Number(b["z_max"], "arena.bounds.z_max")};
                if (m.arena.bounds.radius <= 0 || m.arena.bounds.zMin >= m.arena.bounds.zMax)
                    r.Fail("arena.bounds", "needs a positive radius and z_min below z_max");
            }
        }

        json const& fixture = doc["fixture"];
        if (r.Object(fixture, "fixture", {"account", "staging"}))
        {
            m.fixture.account = fixture["account"].is_string() ? fixture["account"].get<std::string>() : "";
            if (!IsAccountName(m.fixture.account))
                r.Fail("fixture.account", "must be 3-16 of A-Z 0-9");
            m.fixture.staging = r.Pos(fixture["staging"], "fixture.staging", true);
            if (fixture["staging"].is_object() && fixture["staging"].contains("map_id"))
                m.fixture.stagingMap = r.Uint(fixture["staging"]["map_id"], "fixture.staging.map_id", 0, 0xFFFF);
        }

        json const& actors = doc["actors"];
        if (!actors.is_array() || actors.size() != kScenarioActors)
            r.Fail("actors", "must hold exactly two actors");
        else
            for (std::size_t i = 0; i < kScenarioActors; ++i)
            {
                std::string const p = "actors[" + std::to_string(i) + "]";
                json const& a = actors[i];
                if (!r.Object(a, p, {"slot", "name", "team", "start"}))
                    continue;
                ActorSpec& s = m.actors[i];
                s.slot = r.Uint(a["slot"], p + ".slot", 0, kScenarioActors - 1);
                if (s.slot != i)
                    r.Fail(p + ".slot", "must be " + std::to_string(i));
                s.name = a["name"].is_string() ? a["name"].get<std::string>() : "";
                if (!IsCharacterName(s.name))
                    r.Fail(p + ".name", "must be 2-12 letters, capitalised");
                s.team = a["team"].is_string() ? a["team"].get<std::string>() : "";
                if (s.team != "alliance" && s.team != "horde")
                    r.Fail(p + ".team", "must be \"alliance\" or \"horde\" (the arena side)");
                s.start = r.Pos(a["start"], p + ".start");
                ArenaBounds const& b = m.arena.bounds;
                if (b.radius > 0 && (std::hypot(s.start.x - b.x, s.start.y - b.y) > b.radius
                    || s.start.z < b.zMin || s.start.z > b.zMax))
                    r.Fail(p + ".start", "is outside the arena bounds");
            }
        if (m.actors[0].name == m.actors[1].name && !m.actors[0].name.empty())
            r.Fail("actors", "need two different names");
        if (m.actors[0].team == m.actors[1].team && !m.actors[0].team.empty())
            r.Fail("actors", "need one actor on each side");

        ReadLoadout(r, doc["loadout"], m.loadout);
    }
    if (!r.errors.empty())
    {
        out.errors = std::move(r.errors);
        return out;
    }
    m.canonical = CanonicalJson(doc);
    m.sha256 = Sha256Hex(m.canonical);
    out.manifest = std::move(m);
    return out;
}

json ObservationToJson(Observation const& o)
{
    json talents = json::array();
    for (auto const& [id, rank] : o.talents)
        talents.push_back(json::array({id, rank}));
    json stats = json::object();
    for (auto const& [name, value] : o.stats)
        stats[name] = value;
    return json{{"level", o.level}, {"race", o.race}, {"class", o.cls}, {"gender", o.gender}, {"talents", talents},
        {"free_talent_points", o.freeTalentPoints}, {"glyphs", o.glyphs}, {"equipment", o.equipment},
        {"item_extras", o.itemExtras}, {"other_items", o.otherItems}, {"spellbook", o.spellbook},
        {"health", o.health}, {"max_health", o.maxHealth}, {"mana", o.mana}, {"max_mana", o.maxMana},
        {"auras", o.auras}, {"stats", stats}};
}

std::string Fingerprint(Observation const& o)
{
    return Sha256Hex(CanonicalJson(ObservationToJson(o)));
}

std::vector<std::string> CheckLoadout(Loadout const& l, Observation const& o)
{
    std::vector<std::string> d;
    auto want = [&d](char const* what, std::uint64_t expected, std::uint64_t actual) {
        if (expected != actual)
            d.push_back(std::string(what) + " is " + std::to_string(actual) + ", not " + std::to_string(expected));
    };
    want("level", l.level, o.level);
    want("race", l.race.id, o.race);
    want("class", l.cls.id, o.cls);
    want("gender", l.gender, o.gender);

    std::vector<std::pair<std::uint32_t, std::uint32_t>> talents;
    std::uint32_t spent = 0;
    for (TalentPick const& t : l.talents)
    {
        talents.emplace_back(t.talentId, t.rank);
        spent += t.rank;
    }
    std::sort(talents.begin(), talents.end());
    if (talents != o.talents)
        d.push_back("talents differ from the manifest");
    if (l.level >= 10)
        want("free talent points", (l.level - 9) - std::min(spent, l.level - 9), o.freeTalentPoints);

    std::array<std::uint32_t, kGlyphSlots> glyphs{};
    for (GlyphPick const& g : l.glyphs)
        glyphs[g.slot] = g.glyphId;
    for (std::size_t i = 0; i < kGlyphSlots; ++i)
        if (glyphs[i] != o.glyphs[i])
            d.push_back("glyph slot " + std::to_string(i) + " holds " + std::to_string(o.glyphs[i]) + ", not "
                + std::to_string(glyphs[i]));

    std::array<std::uint32_t, kEquipmentSlots> equipment{};
    for (EquipPick const& e : l.equipment)
        equipment[e.slot] = e.itemId;
    for (std::size_t i = 0; i < kEquipmentSlots; ++i)
        if (equipment[i] != o.equipment[i])
            d.push_back("equipment slot " + std::to_string(i) + " holds " + std::to_string(o.equipment[i]) + ", not "
                + std::to_string(equipment[i]));
    want("item enchantments, gems or random properties", 0, o.itemExtras);
    want("items outside the equipment slots", 0, o.otherItems);

    if (l.spellbook != o.spellbook)
    {
        std::vector<std::uint32_t> extra, missing;
        std::set_difference(o.spellbook.begin(), o.spellbook.end(), l.spellbook.begin(), l.spellbook.end(),
            std::back_inserter(extra));
        std::set_difference(l.spellbook.begin(), l.spellbook.end(), o.spellbook.begin(), o.spellbook.end(),
            std::back_inserter(missing));
        d.push_back("spellbook differs (extra " + Join(extra) + "; missing " + Join(missing) + ")");
    }
    want("max health", l.maxHealth, o.maxHealth);
    want("max mana", l.maxMana, o.maxMana);
    want("health (initial is full)", o.maxHealth, o.health);
    want("mana (initial is full)", o.maxMana, o.mana);
    return d;
}

std::vector<std::string> CompareActors(Observation const& a, Observation const& b)
{
    std::vector<std::string> d;
    json const ja = ObservationToJson(a);
    json const jb = ObservationToJson(b);
    for (auto const& [key, value] : ja.items())
        if (value != jb[key])
            d.push_back(key + " differs between the actors");
    return d;
}
}
