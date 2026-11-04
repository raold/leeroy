// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#include "DojoRuntime.h"

#ifndef DOJO_TRAINING_BUILD
#error "DOJO_TRAINING_BUILD is not set: mod-dojo.cmake was skipped (the module folder must be named exactly mod-dojo)"
#endif

namespace Dojo
{
WorkerState& State()
{
    static WorkerState state;
    return state;
}

std::string FormatChecks(std::vector<ServerCheck> const& checks)
{
    std::string out;
    for (ServerCheck const& c : checks)
        out += std::string(c.ok ? "ok   " : "FAIL ") + c.check + ": " + c.detail + "\n";
    return out;
}
}

#if DOJO_TRAINING_BUILD

#include "BattlegroundMgr.h"
#include "Config.h"
#include "DBCStores.h"
#include "ObjectMgr.h"
#include "PlayerbotAIConfig.h"
#include "SpellInfo.h"
#include "SpellMgr.h"
#include "World.h"

#include <cmath>
#include <fstream>
#include <sstream>

namespace Dojo
{
namespace
{
constexpr double kPositionTolerance = 0.05;

void Add(std::vector<ServerCheck>& out, bool ok, std::string check, std::string detail)
{
    out.push_back({ok, std::move(check), std::move(detail)});
}

std::string Locale(char const* const* names)
{
    char const* name = names[sWorld->GetDefaultDbcLocale()];
    return name ? name : "";
}

std::string Locale(std::array<char const*, 16> const& names)
{
    return Locale(names.data());
}

void CheckSpellName(std::vector<ServerCheck>& out, std::string const& what, std::uint32_t spellId, std::string const& name)
{
    SpellInfo const* info = sSpellMgr->GetSpellInfo(spellId);
    if (!info)
        Add(out, false, what, "spell " + std::to_string(spellId) + " does not exist");
    else if (Locale(info->SpellName) != name)
        Add(out, false, what, "spell " + std::to_string(spellId) + " is \"" + Locale(info->SpellName) + "\", not \"" + name + "\"");
    else
        Add(out, true, what, "spell " + std::to_string(spellId) + " " + name);
}
}

std::vector<ServerCheck> CheckTrainingRules(std::uint32_t prepSeconds)
{
    std::vector<ServerCheck> out;
    auto rate = [&out](char const* name, ServerConfigs rate) {
        float const v = sWorld->getRate(rate);
        Add(out, v == 1.0f, std::string("rule ") + name, std::to_string(v));
    };
    rate("Rate.Health", RATE_HEALTH);
    rate("Rate.Mana", RATE_POWER_MANA);
    rate("Rate.Talent", RATE_TALENT);
    rate("Rate.MoveSpeed.Player", RATE_MOVESPEED_PLAYER);
    Add(out, sWorld->getIntConfig(CONFIG_EXPANSION) == 2, "rule Expansion", std::to_string(sWorld->getIntConfig(CONFIG_EXPANSION)));
    Add(out, sWorld->getIntConfig(CONFIG_MAX_PLAYER_LEVEL) == 80, "rule MaxPlayerLevel",
        std::to_string(sWorld->getIntConfig(CONFIG_MAX_PLAYER_LEVEL)));
    std::uint32_t const prep = sWorld->getIntConfig(CONFIG_ARENA_PREP_TIME);
    Add(out, prep == prepSeconds, "rule Arena.PrepTime", std::to_string(prep) + " s (the scenario expects "
        + std::to_string(prepSeconds) + ")");
    int32 const mapThreads = sConfigMgr->GetOption<int32>("MapUpdate.Threads", 1, false);
    Add(out, mapThreads == 0, "rule MapUpdate.Threads", std::to_string(mapThreads) + " (D11 needs 0)");

    PlayerbotAIConfig const& bots = sPlayerbotAIConfig;
    Add(out, bots.Enabled, "rule Playerbots.Enabled", bots.Enabled ? "1" : "0");
    Add(out, bots.BotCheatMask == 0, "rule bot cheats", "mask " + std::to_string(bots.BotCheatMask));
    Add(out, bots.WorldBuffs.empty(), "rule bot world buffs", std::to_string(bots.WorldBuffs.size()) + " configured");
    Add(out, !bots.RandomBotAutologin, "rule random bot autologin", bots.RandomBotAutologin ? "on" : "off");
    Add(out, bots.MinRandomBots == 0 && bots.MaxRandomBots == 0, "rule random bot population",
        std::to_string(bots.MinRandomBots) + "-" + std::to_string(bots.MaxRandomBots));
    Add(out, bots.AddClassAccountPoolSize == 0, "rule add-class pool", std::to_string(bots.AddClassAccountPoolSize));
    Add(out, !bots.RandomBotJoinBG && !bots.RandomBotJoinLfg && bots.RandomBotRaidBrowserListings == 0,
        "rule random bot queues", std::string("bg ") + (bots.RandomBotJoinBG ? "on" : "off") + ", lfg "
        + (bots.RandomBotJoinLfg ? "on" : "off") + ", raid browser " + std::to_string(bots.RandomBotRaidBrowserListings));
    Add(out, bots.RandomBotGuildCount == 0 && bots.RandomBotArenaTeam2v2Count == 0 && bots.RandomBotArenaTeam3v3Count == 0
        && bots.RandomBotArenaTeam5v5Count == 0, "rule bot guilds and arena teams", "none");
    return out;
}

std::optional<Manifest> LoadManifestFile(std::string const& path, std::string const& sha256, std::string& error)
{
    std::ifstream in(path, std::ios::binary);
    if (!in)
    {
        error = "cannot read " + path;
        return std::nullopt;
    }
    std::ostringstream text;
    text << in.rdbuf();
    std::string const body = text.str();
    if (body.size() > kMaxManifestBytes)
    {
        error = path + " is larger than " + std::to_string(kMaxManifestBytes) + " bytes";
        return std::nullopt;
    }
    ManifestParse parsed = ParseManifest(body);
    if (!parsed.manifest)
    {
        error = path + ": " + (parsed.errors.empty() ? std::string("invalid") : parsed.errors.front());
        if (parsed.errors.size() > 1)
            error += " (and " + std::to_string(parsed.errors.size() - 1) + " more)";
        return std::nullopt;
    }
    if (parsed.manifest->sha256 != sha256)
    {
        error = path + " hashes to " + parsed.manifest->sha256 + ", not the pinned Dojo.Scenario.Sha256 " + sha256;
        return std::nullopt;
    }
    return parsed.manifest;
}

std::vector<ServerCheck> ValidateScenario(Manifest const& m)
{
    std::vector<ServerCheck> out;
    ArenaSpec const& a = m.arena;

    MapEntry const* map = sMapStore.LookupEntry(a.mapId);
    if (!map)
        Add(out, false, "arena map", std::to_string(a.mapId) + " does not exist");
    else
        Add(out, map->IsBattleArena() && Locale(map->name) == a.map, "arena map",
            std::to_string(a.mapId) + " \"" + Locale(map->name) + "\"" + (map->IsBattleArena() ? ", an arena" : ", not an arena"));

    BattlemasterListEntry const* bm = sBattlemasterListStore.LookupEntry(a.battlegroundTypeId);
    bool bmMap = false;
    if (bm)
        for (int32 const id : bm->mapid)
            bmMap = bmMap || id == int32(a.mapId);
    Add(out, bm && bmMap && Locale(bm->name) == a.battleground, "arena battleground",
        bm ? std::to_string(a.battlegroundTypeId) + " \"" + Locale(bm->name) + "\"" + (bmMap ? " on the map" : " not on the map")
           : std::to_string(a.battlegroundTypeId) + " does not exist");
    Add(out, BattlegroundMgr::IsArenaType(BattlegroundTypeId(a.battlegroundTypeId)), "arena type",
        "battleground type " + std::to_string(a.battlegroundTypeId) + (BattlegroundMgr::IsArenaType(
            BattlegroundTypeId(a.battlegroundTypeId)) ? " is an arena" : " is not an arena"));

    Battleground* bgTemplate = sBattlegroundMgr->GetBattlegroundTemplate(BattlegroundTypeId(a.battlegroundTypeId));
    Add(out, bgTemplate != nullptr, "arena template", bgTemplate ? "loaded" : "missing from battleground_template");
    if (bgTemplate)
        for (ActorSpec const& actor : m.actors)
        {
            TeamId const team = actor.team == "alliance" ? TEAM_ALLIANCE : TEAM_HORDE;
            Position const* start = bgTemplate->GetTeamStartPosition(team);
            bool const same = start && std::fabs(start->GetPositionX() - actor.start.x) < kPositionTolerance
                && std::fabs(start->GetPositionY() - actor.start.y) < kPositionTolerance
                && std::fabs(start->GetPositionZ() - actor.start.z) < kPositionTolerance
                && std::fabs(start->GetOrientation() - actor.start.o) < kPositionTolerance;
            Add(out, same, "start " + actor.name, start ? actor.team + " start is (" + std::to_string(start->GetPositionX())
                + ", " + std::to_string(start->GetPositionY()) + ", " + std::to_string(start->GetPositionZ()) + ", "
                + std::to_string(start->GetOrientation()) + ")" : "no start position for " + actor.team);
        }

    Loadout const& l = m.loadout;
    PvPDifficultyEntry const* bracket = GetBattlegroundBracketByLevel(a.mapId, l.level);
    Add(out, bracket != nullptr, "arena bracket", bracket ? "level " + std::to_string(l.level) + " is in "
        + std::to_string(bracket->minLevel) + "-" + std::to_string(bracket->maxLevel) : "no bracket for level " + std::to_string(l.level));

    ChrRacesEntry const* race = sChrRacesStore.LookupEntry(l.race.id);
    Add(out, race && Locale(race->name) == l.race.name, "race", race ? std::to_string(l.race.id) + " \"" + Locale(race->name) + "\""
        : std::to_string(l.race.id) + " does not exist");
    ChrClassesEntry const* cls = sChrClassesStore.LookupEntry(l.cls.id);
    Add(out, cls && Locale(cls->name) == l.cls.name, "class", cls ? std::to_string(l.cls.id) + " \"" + Locale(cls->name) + "\""
        : std::to_string(l.cls.id) + " does not exist");
    Add(out, sObjectMgr->GetPlayerInfo(l.race.id, l.cls.id) != nullptr, "race and class", "playercreateinfo "
        + std::string(sObjectMgr->GetPlayerInfo(l.race.id, l.cls.id) ? "exists" : "is missing"));

    for (KitSpell const& k : l.kit)
    {
        CheckSpellName(out, "kit " + k.name, k.spellId, k.name);
        SpellInfo const* info = sSpellMgr->GetSpellInfo(k.spellId);
        if (!info)
            continue;
        std::uint32_t const rank = sSpellMgr->GetSpellRank(k.spellId);
        Add(out, rank == k.rank, "kit " + k.name + " rank", "rank " + std::to_string(rank));
        // The highest rank a level-N character can know: the next rank in the chain must need a higher level.
        std::uint32_t const next = sSpellMgr->GetNextSpellInChain(k.spellId);
        SpellInfo const* nextInfo = next ? sSpellMgr->GetSpellInfo(next) : nullptr;
        bool const highest = !nextInfo || nextInfo->SpellLevel > l.level;
        Add(out, highest && info->SpellLevel <= l.level && info->SpellFamilyName == SPELLFAMILY_MAGE && !info->IsPassive(),
            "kit " + k.name + " level", "learned at " + std::to_string(info->SpellLevel) + (highest ? ", highest rank for level "
            + std::to_string(l.level) : ", a higher rank " + std::to_string(next) + " is available"));
    }

    std::uint32_t const classMask = l.cls.id ? 1u << (l.cls.id - 1) : 0;
    for (TalentPick const& t : l.talents)
    {
        TalentEntry const* talent = sTalentStore.LookupEntry(t.talentId);
        TalentTabEntry const* tab = talent ? sTalentTabStore.LookupEntry(talent->TalentTab) : nullptr;
        if (!talent || !tab)
        {
            Add(out, false, "talent " + t.name, std::to_string(t.talentId) + " does not exist");
            continue;
        }
        bool const classOk = (tab->ClassMask & classMask) != 0;
        bool const rankOk = t.rank >= 1 && t.rank <= MAX_TALENT_RANK && talent->RankID[t.rank - 1] == t.spellId
            && (t.rank == MAX_TALENT_RANK || talent->RankID[t.rank] == 0);
        Add(out, classOk && rankOk, "talent " + t.name, std::to_string(t.talentId) + " rank " + std::to_string(t.rank)
            + (classOk ? "" : ", not this class's") + (rankOk ? ", the talent's last rank" : ", spell or rank mismatch (ranks must be full)"));
        CheckSpellName(out, "talent " + t.name + " spell", t.spellId, t.name);
    }

    for (GlyphPick const& g : l.glyphs)
    {
        GlyphPropertiesEntry const* glyph = sGlyphPropertiesStore.LookupEntry(g.glyphId);
        Add(out, glyph && glyph->SpellId == g.spellId, "glyph " + g.name, glyph ? std::to_string(g.glyphId) + " casts "
            + std::to_string(glyph->SpellId) : std::to_string(g.glyphId) + " does not exist");
        CheckSpellName(out, "glyph " + g.name + " spell", g.spellId, g.name);
    }

    for (EquipPick const& e : l.equipment)
    {
        ItemTemplate const* item = sObjectMgr->GetItemTemplate(e.itemId);
        if (!item)
        {
            Add(out, false, "item " + e.name, std::to_string(e.itemId) + " does not exist");
            continue;
        }
        bool const plain = item->RandomProperty == 0 && item->RandomSuffix == 0;
        bool const usable = item->RequiredLevel <= l.level && (item->AllowableClass & classMask)
            && (item->AllowableRace & (1u << (l.race.id - 1)))
            && !item->HasFlag2(ITEM_FLAG2_FACTION_HORDE) && !item->HasFlag2(ITEM_FLAG2_FACTION_ALLIANCE);
        Add(out, item->Name1 == e.name && plain && usable, "item " + e.name, std::to_string(e.itemId) + " \"" + item->Name1 + "\""
            + (plain ? "" : ", has random properties") + (usable ? "" : ", not usable by this race, class and level"));
    }

    MapEntry const* staging = sMapStore.LookupEntry(m.fixture.stagingMap);
    Add(out, staging && !staging->Instanceable(), "fixture staging map", staging ? std::to_string(m.fixture.stagingMap) + " \""
        + Locale(staging->name) + "\"" + (staging->Instanceable() ? ", instanced" : "") : "does not exist");
    return out;
}
}

#endif
