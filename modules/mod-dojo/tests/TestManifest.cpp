// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

// Scenario manifests: the shared accept/refuse cases (tests/fixtures/scenario_cases.json, which
// dojo/tests/test_scenario.py also runs), the hash Python pinned next to the real manifest, and the loadout checks the
// runtime applies to what it observes in the game.

#include "DojoManifest.h"
#include "DojoSha256.h"
#include "TestHarness.h"

#include <algorithm>
#include <fstream>
#include <sstream>
#include <string>

using namespace Dojo;
using nlohmann::json;

namespace
{
std::string ReadFile(std::string const& path)
{
    std::ifstream in(path, std::ios::binary);
    std::ostringstream text;
    text << in.rdbuf();
    return text.str();
}

std::string const kManifestPath = std::string(DOJO_SCENARIOS) + "/frost_mirror_small_v1.json";

json& At(json& doc, json const& path, std::size_t upto)
{
    json* node = &doc;
    for (std::size_t i = 0; i < upto; ++i)
        node = path[i].is_string() ? &(*node)[path[i].get<std::string>()] : &(*node)[path[i].get<std::size_t>()];
    return *node;
}

std::string Apply(json const& c)
{
    if (c.contains("raw"))
        return c["raw"].get<std::string>();
    json doc = json::parse(ReadFile(kManifestPath));
    if (c.contains("set"))
    {
        json const& path = c["set"];
        json& parent = At(doc, path, path.size() - 1);
        json const& last = path.back();
        if (last.is_string())
            parent[last.get<std::string>()] = c["value"];
        else if (last.get<std::size_t>() < parent.size())
            parent[last.get<std::size_t>()] = c["value"];
        else
            parent.push_back(c["value"]);
    }
    if (c.contains("remove"))
    {
        json const& path = c["remove"];
        json& parent = At(doc, path, path.size() - 1);
        parent.erase(path.back().get<std::string>());
    }
    return doc.dump(2);
}

Observation Observed(Loadout const& l)
{
    Observation o;
    o.level = l.level;
    o.race = l.race.id;
    o.cls = l.cls.id;
    o.gender = l.gender;
    std::uint32_t spent = 0;
    for (TalentPick const& t : l.talents)
    {
        o.talents.emplace_back(t.talentId, t.rank);
        spent += t.rank;
    }
    std::sort(o.talents.begin(), o.talents.end());
    o.freeTalentPoints = (l.level - 9) - spent;
    for (GlyphPick const& g : l.glyphs)
        o.glyphs[g.slot] = g.glyphId;
    for (EquipPick const& e : l.equipment)
        o.equipment[e.slot] = e.itemId;
    o.spellbook = l.spellbook;
    o.health = o.maxHealth = l.maxHealth;
    o.mana = o.maxMana = l.maxMana;
    o.auras = {12497, 32727};
    o.stats = {{"intellect", 57800}, {"stamina", 97400}};
    return o;
}
}

DOJO_TEST(Sha256_KnownVectors)
{
    CHECK_EQ(Sha256Hex(""), std::string("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"));
    CHECK_EQ(Sha256Hex("abc"), std::string("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"));
    CHECK_EQ(Sha256Hex("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq"),
        std::string("248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"));
    std::string million(1000000, 'a');
    CHECK_EQ(Sha256Hex(million), std::string("cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0"));
    // Fed in pieces that straddle the 64-byte block boundary.
    Sha256 sha;
    for (std::size_t i = 0; i < million.size(); i += 63)
        sha.Update(std::string_view(million).substr(i, 63));
    std::array<std::uint8_t, 32> const digest = sha.Final();
    CHECK_EQ(int(digest[0]), 0xcd);
    CHECK_EQ(int(digest[31]), 0xd0);
}

DOJO_TEST(Manifest_SharedCases)
{
    json const cases = json::parse(ReadFile(std::string(DOJO_TEST_FIXTURES) + "/scenario_cases.json"))["cases"];
    CHECK(cases.size() > 30);
    for (json const& c : cases)
    {
        std::string const name = c["name"].get<std::string>();
        ManifestParse const parsed = ParseManifest(Apply(c));
        bool const ok = parsed.manifest.has_value();
        CHECK_MSG(ok == c["ok"].get<bool>(), name + (parsed.errors.empty() ? std::string() : ": " + parsed.errors.front()));
        if (!ok && c.contains("error"))
        {
            std::string all;
            for (std::string const& e : parsed.errors)
                all += e + "\n";
            CHECK_MSG(all.find(c["error"].get<std::string>()) != std::string::npos, name + " said: " + all);
        }
    }
}

DOJO_TEST(Manifest_HashMatchesThePythonPin)
{
    ManifestParse const parsed = ParseManifest(ReadFile(kManifestPath));
    CHECK(parsed.manifest.has_value());
    if (!parsed.manifest)
        return;
    std::string const pin = ReadFile(std::string(DOJO_SCENARIOS) + "/frost_mirror_small_v1.sha256").substr(0, 64);
    CHECK_EQ(parsed.manifest->sha256, pin);
    CHECK_EQ(Sha256Hex(parsed.manifest->canonical), pin);
    // Layout and key order are not part of the hash.
    json doc = json::parse(ReadFile(kManifestPath));
    ManifestParse const compact = ParseManifest(doc.dump());
    CHECK(compact.manifest && compact.manifest->sha256 == pin);
    // Any change of value is.
    doc["loadout"]["initial"]["max_mana"] = doc["loadout"]["initial"]["max_mana"].get<std::uint32_t>() + 1;
    ManifestParse const changed = ParseManifest(doc.dump());
    CHECK(changed.manifest && changed.manifest->sha256 != pin);
}

DOJO_TEST(Manifest_ReadsTheFrozenScenario)
{
    ManifestParse const parsed = ParseManifest(ReadFile(kManifestPath));
    if (!parsed.manifest)
    {
        CHECK(false);
        return;
    }
    Manifest const& m = *parsed.manifest;
    CHECK_EQ(m.id, std::string("frost_mirror_small_v1"));
    CHECK(!m.rules.rated);
    CHECK_EQ(m.rules.matchSeconds, 180u);
    CHECK_EQ(m.arena.mapId, 559u);
    CHECK_EQ(m.actors[0].team, std::string("alliance"));
    CHECK_EQ(m.actors[1].team, std::string("horde"));
    std::uint32_t points = 0;
    for (TalentPick const& t : m.loadout.talents)
        points += t.rank;
    CHECK_EQ(points, 71u);
    CHECK_EQ(m.loadout.kit.size(), 3u);
    CHECK(m.canonical.find("\": ") == std::string::npos && m.canonical.find(", \"") == std::string::npos);
    CHECK(ParseManifest(std::string(kMaxManifestBytes + 1, ' ')).errors.size() == 1);
}

DOJO_TEST(Manifest_CheckLoadoutAcceptsTheFrozenLoadout)
{
    ManifestParse const parsed = ParseManifest(ReadFile(kManifestPath));
    if (!parsed.manifest)
    {
        CHECK(false);
        return;
    }
    Loadout const& l = parsed.manifest->loadout;
    Observation const o = Observed(l);
    CHECK(CheckLoadout(l, o).empty());
    CHECK(CompareActors(o, o).empty());
    CHECK_EQ(Fingerprint(o), Fingerprint(Observed(l)));
}

DOJO_TEST(Manifest_CheckLoadoutNamesEachDifference)
{
    ManifestParse const parsed = ParseManifest(ReadFile(kManifestPath));
    if (!parsed.manifest)
    {
        CHECK(false);
        return;
    }
    Loadout const& l = parsed.manifest->loadout;
    struct Case
    {
        char const* what;
        void (*change)(Observation&);
    };
    Case const cases[] = {
        {"level", [](Observation& o) { o.level = 79; }},
        {"talents", [](Observation& o) { o.talents.pop_back(); }},
        {"free talent points", [](Observation& o) { o.freeTalentPoints = 1; }},
        {"glyph slot 0", [](Observation& o) { o.glyphs[0] = 0; }},
        {"equipment slot 15", [](Observation& o) { o.equipment[15] = 42343; }},
        {"enchantments", [](Observation& o) { o.itemExtras = 1; }},
        {"outside the equipment slots", [](Observation& o) { o.otherItems = 1; }},
        {"spellbook differs (extra 116", [](Observation& o) { o.spellbook.insert(o.spellbook.begin(), 116); }},
        {"max health", [](Observation& o) { o.maxHealth += 1; }},
        {"mana (initial is full)", [](Observation& o) { o.mana -= 1; }},
        {"health (initial is full)", [](Observation& o) { o.health -= 1; }},
    };
    for (Case const& c : cases)
    {
        Observation o = Observed(l);
        c.change(o);
        std::vector<std::string> const d = CheckLoadout(l, o);
        std::string all;
        for (std::string const& line : d)
            all += line + "\n";
        CHECK_MSG(all.find(c.what) != std::string::npos, std::string(c.what) + " not reported: " + all);
    }
}

DOJO_TEST(Manifest_CompareActorsFindsAnyDifference)
{
    ManifestParse const parsed = ParseManifest(ReadFile(kManifestPath));
    if (!parsed.manifest)
    {
        CHECK(false);
        return;
    }
    Observation const a = Observed(parsed.manifest->loadout);
    Observation b = a;
    b.auras.push_back(2479);
    std::vector<std::string> d = CompareActors(a, b);
    CHECK_EQ(d.size(), 1u);
    CHECK(!d.empty() && d[0].find("auras") != std::string::npos);
    CHECK(Fingerprint(a) != Fingerprint(b));
    b = a;
    b.stats[0].second += 1;
    d = CompareActors(a, b);
    CHECK(!d.empty() && d[0].find("stats") != std::string::npos);
}
