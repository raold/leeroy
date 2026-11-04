// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#include "DojoFixtures.h"

#include "DojoExecutor.h"
#include "DojoRuntime.h"

#ifndef DOJO_TRAINING_BUILD
#error "DOJO_TRAINING_BUILD is not set: mod-dojo.cmake was skipped (the module folder must be named exactly mod-dojo)"
#endif

#if DOJO_TRAINING_BUILD

#include "AccountMgr.h"
#include "Arena.h"
#include "Battleground.h"
#include "BattlegroundMgr.h"
#include "CharacterCache.h"
#include "DBCStores.h"
#include "Item.h"
#include "Log.h"
#include "Map.h"
#include "ObjectAccessor.h"
#include "ObjectMgr.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "Playerbots.h"
#include "RandomPlayerbotMgr.h"
#include "SpellAuras.h"
#include "SpellInfo.h"
#include "SpellMgr.h"
#include "World.h"
#include "WorldSession.h"

#include <algorithm>
#include <cmath>
#include <map>
#include <set>

using nlohmann::json;

namespace Dojo
{
namespace
{
constexpr std::uint32_t kLoginMs = 60000;
constexpr std::uint32_t kTeleportMs = 60000;
constexpr std::uint32_t kLogoutMs = 30000;
constexpr float kArrivalTolerance = 1.0f;   // yards from the frozen start position
constexpr float kGroundTolerance = 3.0f;    // yards between a start position and the map's ground height
constexpr std::uint32_t kTeamMarkers[] = {SPELL_ALLIANCE_GOLD_FLAG, SPELL_ALLIANCE_GREEN_FLAG, SPELL_HORDE_GOLD_FLAG,
    SPELL_HORDE_GREEN_FLAG};

Manifest const& Scenario()
{
    return *State().manifest;
}

TeamId Side(ActorSpec const& actor)
{
    return actor.team == "alliance" ? TEAM_ALLIANCE : TEAM_HORDE;
}

std::int64_t Hundredths(float value)
{
    return std::llround(double(value) * 100.0);
}

char const* StepName(int step)
{
    static char const* const names[] = {"idle", "login", "stage", "apply", "verify", "send", "arrive", "inspect", "leave",
        "logout", "arena-gone", "done"};
    return step >= 0 && step < int(std::size(names)) ? names[step] : "?";
}
}

Director& Director::Instance()
{
    static Director director;
    return director;
}

bool Director::Resolve(std::string& error)
{
    Manifest const& m = Scenario();
    std::uint32_t const account = AccountMgr::GetId(m.fixture.account);
    if (!account)
    {
        error = "the fixture account " + m.fixture.account + " does not exist (python -m leeroy_dojo realm accounts)";
        return false;
    }
    for (std::size_t i = 0; i < kScenarioActors; ++i)
    {
        ObjectGuid const guid = sCharacterCache->GetCharacterGuidByName(m.actors[i].name);
        if (guid.IsEmpty())
        {
            error = m.actors[i].name + " does not exist (dojo fixture create)";
            return false;
        }
        if (sCharacterCache->GetCharacterAccountIdByGuid(guid) != account)
        {
            error = m.actors[i].name + " is not on the fixture account; refusing to touch another account's character";
            return false;
        }
        if (_actors[i].guid != guid.GetRawValue())
            _actors[i] = Actor{guid.GetRawValue()};
        Control::Instance().Register(i, guid.GetRawValue());
    }
    return true;
}

Player* Director::Find(std::size_t index) const
{
    if (!_actors[index].guid)
        return nullptr;
    Player* player = ObjectAccessor::FindConnectedPlayer(ObjectGuid(_actors[index].guid));
    if (!player || !player->IsInWorld() || !GET_PLAYERBOT_AI(player))
        return nullptr;
    return player;
}

bool Director::AllOnline() const
{
    for (std::size_t i = 0; i < kScenarioActors; ++i)
        if (!Find(i))
            return false;
    return true;
}

void Director::Begin(Job job, Step first)
{
    _job = job;
    _jobName = job == Job::Build ? "build" : job == Job::Restore ? "restore" : job == Job::Open ? "open" : "close";
    _step = first;
    _stepMs = 0;
    _entered = false;
    _jobOk = false;
    _failure.clear();
    _log.clear();
    _result = _jobName + " running";
}

void Director::Advance(Step next)
{
    _step = next;
    _stepMs = 0;
    _entered = false;
}

void Director::Finish(bool ok, std::string const& message)
{
    _jobOk = ok;
    _result = _jobName + (ok ? " ok: " : " FAILED: ") + message;
    LOG_INFO("module.dojo", "mod-dojo: {}", _result);
    _job = Job::None;
    _step = Step::Idle;
    _stepMs = 0;
}

void Director::Fail(std::string const& message)
{
    // A failed open never leaves actors behind: close what was opened, then report the original failure.
    if (_job == Job::Open && _arenaInstance)
    {
        _failure = message;
        _job = Job::Close;
        _closing = true;
        Advance(Step::Leave);
        _result = "open FAILED: " + message + " (closing the arena)";
        return;
    }
    LogoutAll();
    Finish(false, message);
}

bool Director::StepTimedOut(std::uint32_t limitMs) const
{
    return _stepMs > limitMs;
}

void Director::LogoutAll()
{
    for (std::size_t i = 0; i < kScenarioActors; ++i)
        if (Find(i))
            sRandomPlayerbotMgr.LogoutPlayerBot(ObjectGuid(_actors[i].guid));
}

void Director::RecordBaseline(std::size_t index, Player* player)
{
    Actor& a = _actors[index];
    a.baselineAuras.clear();
    for (auto const& [spellId, application] : player->GetAppliedAuras())
        a.baselineAuras.push_back(spellId);
    std::sort(a.baselineAuras.begin(), a.baselineAuras.end());
    a.baselineLeader = player->HasPlayerFlag(PLAYER_FLAGS_GROUP_LEADER);
    a.baselineRecorded = true;
}

std::uint32_t Director::RestoreBaseline(std::size_t index, Player* player)
{
    Actor& a = _actors[index];
    if (!a.baselineRecorded)
        return 0;
    std::set<std::uint32_t> extra;
    for (auto const& [spellId, application] : player->GetAppliedAuras())
        if (!std::binary_search(a.baselineAuras.begin(), a.baselineAuras.end(), spellId))
            extra.insert(spellId);
    for (std::uint32_t const spellId : extra)
        player->RemoveAurasDueToSpell(spellId);
    if (!a.baselineLeader && !player->GetGroup())
        player->RemovePlayerFlag(PLAYER_FLAGS_GROUP_LEADER);
    // The staging baseline is full health, mana and durability with nothing on cooldown: what a fight spent goes.
    Control::RestoreBaseline(player);
    a.baselineRecorded = false;
    return std::uint32_t(extra.size());
}

std::string Director::Create()
{
    WorkerState const& state = State();
    if (!state.scenarioReady)
        return "dojo fixture create: refused, the scenario is not ready (see dojo status)";
    if (_job != Job::None)
        return "dojo fixture create: refused, the " + _jobName + " job is running";
    Manifest const& m = Scenario();
    std::uint32_t const account = AccountMgr::GetId(m.fixture.account);
    if (!account)
        return "dojo fixture create: the fixture account " + m.fixture.account + " does not exist";
    Loadout const& l = m.loadout;
    std::string out;
    for (ActorSpec const& actor : m.actors)
    {
        ObjectGuid const existing = sCharacterCache->GetCharacterGuidByName(actor.name);
        if (!existing.IsEmpty())
        {
            if (sCharacterCache->GetCharacterAccountIdByGuid(existing) != account)
                return "dojo fixture create: refused, " + actor.name + " belongs to another account";
            out += actor.name + " exists (guid " + std::to_string(existing.GetCounter()) + ")\n";
            continue;
        }
        if (ObjectMgr::CheckPlayerName(actor.name, true) != CHAR_NAME_SUCCESS)
            return "dojo fixture create: the name " + actor.name + " is not allowed";
        WorldSession* session = new WorldSession(account, std::string(), 0, nullptr, SEC_PLAYER,
            EXPANSION_WRATH_OF_THE_LICH_KING, time_t(0), sWorld->GetDefaultDbcLocale(), 0, false, false, 0);
        CharacterCreateInfo info(actor.name, uint8(l.race.id), uint8(l.cls.id), uint8(l.gender), uint8(l.appearance.skin),
            uint8(l.appearance.face), uint8(l.appearance.hairStyle), uint8(l.appearance.hairColor),
            uint8(l.appearance.facialHair));
        Player* player = new Player(session);
        player->GetMotionMaster()->Initialize();
        if (!player->Create(sObjectMgr->GetGenerator<HighGuid::Player>().Generate(), &info))
        {
            player->CleanupsBeforeDelete();
            delete player;
            delete session;
            return out + "dojo fixture create: the core refused to create " + actor.name;
        }
        player->setCinematic(2);
        player->SetAtLoginFlag(AT_LOGIN_NONE);
        player->SaveToDB(true, false);
        sCharacterCache->AddCharacterCacheEntry(player->GetGUID(), account, player->GetName(), player->getGender(),
            player->getRace(), player->getClass(), player->GetLevel());
        out += "created " + actor.name + " (guid " + std::to_string(player->GetGUID().GetCounter()) + ", level "
            + std::to_string(player->GetLevel()) + "; dojo fixture build sets the loadout)\n";
        LOG_INFO("module.dojo", "mod-dojo: created fixture {} on account {}", actor.name, m.fixture.account);
        player->CleanupsBeforeDelete();
        delete player;
        delete session;
    }
    return out + "dojo fixture create: done";
}

std::string Director::StartBuild()
{
    if (!State().scenarioReady)
        return "dojo fixture build: refused, the scenario is not ready (see dojo status)";
    if (_job != Job::None || _arenaInstance)
        return "dojo fixture build: refused, " + (_job != Job::None ? "the " + _jobName + " job is running" : std::string("the arena is open"));
    std::string error;
    if (!Resolve(error))
        return "dojo fixture build: " + error;
    Begin(Job::Build, Step::Login);
    return "dojo fixture build: started (dojo status reports progress)";
}

std::string Director::StartRestore()
{
    if (!State().scenarioReady)
        return "dojo fixture restore: refused, the scenario is not ready (see dojo status)";
    if (_job != Job::None || _arenaInstance)
        return "dojo fixture restore: refused, " + (_job != Job::None ? "the " + _jobName + " job is running" : std::string("the arena is open"));
    std::string error;
    if (!Resolve(error))
        return "dojo fixture restore: " + error;
    Begin(Job::Restore, Step::Login);
    return "dojo fixture restore: started (dojo status reports progress)";
}

std::string Director::StartOpen()
{
    if (!State().scenarioReady)
        return "dojo arena open: refused, the scenario is not ready (see dojo status)";
    if (_job != Job::None || _arenaInstance)
        return "dojo arena open: refused, " + (_job != Job::None ? "the " + _jobName + " job is running" : std::string("the arena is already open"));
    std::string error;
    if (!Resolve(error))
        return "dojo arena open: " + error;
    _stockCasts = 0;
    _executorCasts = 0;
    _triggeredCasts = 0;
    _evidence.clear();
    Begin(Job::Open, Step::Login);
    return "dojo arena open: started (dojo status reports progress)";
}

std::string Director::StartClose()
{
    if (_job != Job::None)
        return "dojo arena close: refused, the " + _jobName + " job is running";
    if (!_arenaInstance && !AllOnline() && !Find(0) && !Find(1))
        return "dojo arena close: nothing is open";
    Begin(Job::Close, Step::Leave);
    _closing = true;
    return "dojo arena close: started";
}

Battleground* Director::Arena() const
{
    if (!_arenaInstance)
        return nullptr;
    return sBattlegroundMgr->GetBattleground(_arenaInstance, BattlegroundTypeId(Scenario().arena.battlegroundTypeId));
}

bool Director::SuppressesWinCheck(Battleground const* bg) const
{
    return bg && _arenaInstance && bg->GetInstanceID() == _arenaInstance;
}

void Director::OnFixtureCast(Player const* player, std::uint32_t spellId, bool triggered, bool executor)
{
    for (Actor const& a : _actors)
        if (a.guid && player->GetGUID().GetRawValue() == a.guid)
        {
            if (triggered)
                ++_triggeredCasts;
            else if (executor)
                ++_executorCasts;
            else
            {
                ++_stockCasts;
                LOG_WARN("module.dojo", "mod-dojo: fixture {} cast {} that no executor command asked for", player->GetName(),
                    spellId);
            }
        }
}

bool Director::ApplyLoadout(Player* player, std::string& error)
{
    Loadout const& l = Scenario().loadout;
    if (player->GetLevel() != l.level)
    {
        player->GiveLevel(uint8(l.level));
        player->SetUInt32Value(PLAYER_XP, 0);
    }
    player->InitTalentForLevel();
    player->InitGlyphsForLevel();
    player->UpdateSkillsToMaxSkillsForLevel();

    // No transient state: every aura that is not a passive (talents, racials and glyphs reapply theirs) goes, and so
    // does a raid-leader flag a past group left behind.
    std::set<std::uint32_t> transient;
    for (auto const& [spellId, application] : player->GetAppliedAuras())
        if (!application->GetBase()->GetSpellInfo()->IsPassive())
            transient.insert(spellId);
    for (std::uint32_t const spellId : transient)
        player->RemoveAurasDueToSpell(spellId);
    if (!player->GetGroup())
        player->RemovePlayerFlag(PLAYER_FLAGS_GROUP_LEADER);

    // Items: nothing but the manifest's equipment, no starting gear, food or hearthstone.
    for (uint8 slot = EQUIPMENT_SLOT_START; slot < INVENTORY_SLOT_ITEM_END; ++slot)
        if (player->GetItemByPos(INVENTORY_SLOT_BAG_0, slot))
            player->DestroyItem(INVENTORY_SLOT_BAG_0, slot, true);
    for (EquipPick const& e : l.equipment)
    {
        uint16 dest = 0;
        InventoryResult const msg = player->CanEquipNewItem(uint8(e.slot), dest, e.itemId, false);
        if (msg != EQUIP_ERR_OK)
        {
            error = "cannot equip " + e.name + " in slot " + std::to_string(e.slot) + " (inventory result " + std::to_string(msg) + ")";
            return false;
        }
        if (!player->EquipNewItem(dest, e.itemId, true))
        {
            error = "equipping " + e.name + " failed";
            return false;
        }
    }

    // Talents: reset, then learn in the manifest's order under the core's own legality checks (points, tiers,
    // dependencies); a rank the core refuses is a failed build, not a substitute.
    player->resetTalents(true);
    for (TalentPick const& t : l.talents)
    {
        player->LearnTalent(t.talentId, t.rank - 1);
        if (!player->HasTalent(t.spellId, player->GetActiveSpec()))
        {
            error = "the core refused talent " + t.name + " rank " + std::to_string(t.rank);
            return false;
        }
    }

    player->RemoveAllSpellCooldown();

    // Spells: the kit at its frozen ranks and nothing else learned. What the core teaches every character on load
    // (its class skill lines' first ranks, general abilities, racials) and what the talents grant stays, as ordinary
    // rules have it; the manifest's spellbook freezes that whole set, and the action catalog, not the spellbook,
    // restricts which of them an actor may use.
    for (KitSpell const& k : l.kit)
        if (!player->HasSpell(k.spellId))
            player->learnSpell(k.spellId);

    // Glyphs: the manifest's, each in a slot of its own type; every other slot empty.
    for (uint8 slot = 0; slot < kGlyphSlots; ++slot)
        if (std::uint32_t const old = player->GetGlyph(slot))
        {
            if (GlyphPropertiesEntry const* gp = sGlyphPropertiesStore.LookupEntry(old))
                player->RemoveAurasDueToSpell(gp->SpellId);
            player->SetGlyph(slot, 0, true);
        }
    for (GlyphPick const& g : l.glyphs)
    {
        GlyphPropertiesEntry const* gp = sGlyphPropertiesStore.LookupEntry(g.glyphId);
        GlyphSlotEntry const* gs = sGlyphSlotStore.LookupEntry(player->GetGlyphSlot(uint8(g.slot)));
        if (!gp || !gs || gp->TypeFlags != gs->TypeFlags)
        {
            error = g.name + " does not fit glyph slot " + std::to_string(g.slot);
            return false;
        }
        player->CastSpell(player, gp->SpellId, true);
        player->SetGlyph(uint8(g.slot), g.glyphId, true);
    }

    player->UpdateAllStats();
    player->SetFullHealth();
    player->SetPower(POWER_MANA, player->GetMaxPower(POWER_MANA));
    player->SaveToDB(false, false);
    return true;
}

Observation Director::Observe(Player* player) const
{
    Observation o;
    o.level = player->GetLevel();
    o.race = player->getRace();
    o.cls = player->getClass();
    o.gender = player->getGender();
    std::map<std::uint32_t, std::uint32_t> talents;
    for (auto const& [spellId, talent] : player->GetTalentMap())
    {
        if (talent->State == PLAYERSPELL_REMOVED || !talent->IsInSpec(player->GetActiveSpec()))
            continue;
        if (TalentSpellPos const* pos = GetTalentSpellPos(spellId))
            talents[pos->talent_id] = std::max<std::uint32_t>(talents[pos->talent_id], pos->rank + 1);
    }
    o.talents.assign(talents.begin(), talents.end());
    o.freeTalentPoints = player->GetFreeTalentPoints();
    for (uint8 slot = 0; slot < kGlyphSlots; ++slot)
        o.glyphs[slot] = player->GetGlyph(slot);
    for (uint8 slot = EQUIPMENT_SLOT_START; slot < EQUIPMENT_SLOT_END; ++slot)
        if (Item* item = player->GetItemByPos(INVENTORY_SLOT_BAG_0, slot))
        {
            o.equipment[slot] = item->GetEntry();
            if (item->GetItemRandomPropertyId() != 0)
                ++o.itemExtras;
            for (uint32 e = 0; e < MAX_ENCHANTMENT_SLOT; ++e)
                if (item->GetEnchantmentId(EnchantmentSlot(e)))
                    ++o.itemExtras;
        }
    for (uint8 slot = EQUIPMENT_SLOT_END; slot < INVENTORY_SLOT_ITEM_END; ++slot)
        if (player->GetItemByPos(INVENTORY_SLOT_BAG_0, slot))
            ++o.otherItems;
    for (auto const& [spellId, spell] : player->GetSpellMap())
    {
        if (spell->State == PLAYERSPELL_REMOVED || !spell->Active || !spell->IsInSpec(player->GetActiveSpec()))
            continue;
        SpellInfo const* info = sSpellMgr->GetSpellInfo(spellId);
        if (info && !info->IsPassive())
            o.spellbook.push_back(spellId);
    }
    std::sort(o.spellbook.begin(), o.spellbook.end());
    o.health = player->GetHealth();
    o.maxHealth = player->GetMaxHealth();
    o.mana = player->GetPower(POWER_MANA);
    o.maxMana = player->GetMaxPower(POWER_MANA);
    std::set<std::uint32_t> auras;
    for (auto const& [spellId, application] : player->GetAppliedAuras())
        if (std::find(std::begin(kTeamMarkers), std::end(kTeamMarkers), spellId) == std::end(kTeamMarkers))
            auras.insert(spellId);
    o.auras.assign(auras.begin(), auras.end());
    o.stats = {
        {"agility", Hundredths(player->GetStat(STAT_AGILITY))},
        {"armor", std::int64_t(player->GetArmor())},
        {"crit_spell_frost", Hundredths(player->GetFloatValue(PLAYER_SPELL_CRIT_PERCENTAGE1 + SPELL_SCHOOL_FROST))},
        {"haste_rating", std::int64_t(player->GetUInt32Value(PLAYER_FIELD_COMBAT_RATING_1 + CR_HASTE_SPELL))},
        {"hit_rating", std::int64_t(player->GetUInt32Value(PLAYER_FIELD_COMBAT_RATING_1 + CR_HIT_SPELL))},
        {"intellect", Hundredths(player->GetStat(STAT_INTELLECT))},
        {"resilience", std::int64_t(player->GetUInt32Value(PLAYER_FIELD_COMBAT_RATING_1 + CR_CRIT_TAKEN_SPELL))},
        {"resist_frost", std::int64_t(player->GetResistance(SPELL_SCHOOL_FROST))},
        {"spell_power_frost", std::int64_t(player->SpellBaseDamageBonusDone(SPELL_SCHOOL_MASK_FROST))},
        {"spirit", Hundredths(player->GetStat(STAT_SPIRIT))},
        {"stamina", Hundredths(player->GetStat(STAT_STAMINA))},
        {"strength", Hundredths(player->GetStat(STAT_STRENGTH))},
    };
    return o;
}

bool Director::OpenArena(std::string& error)
{
    Manifest const& m = Scenario();
    BattlegroundTypeId const type = BattlegroundTypeId(m.arena.battlegroundTypeId);
    PvPDifficultyEntry const* bracket = GetBattlegroundBracketByLevel(m.arena.mapId, m.loadout.level);
    if (!bracket)
    {
        error = "no battleground bracket for level " + std::to_string(m.loadout.level);
        return false;
    }
    // The training factory: an unrated instance of the stock arena with no queue, team, rating or reward behind it.
    // It is registered for updates but never put in the free-slot queue, so no public queue can fill it, and it
    // holds one invited actor per side; the stock size rules are not changed.
    Battleground* bg = sBattlegroundMgr->CreateNewBattleground(type, bracket, uint8(m.arena.arenaType), false);
    if (!bg)
    {
        error = "the core could not create a battleground of type " + std::to_string(m.arena.battlegroundTypeId);
        return false;
    }
    if (bg->GetMapId() != m.arena.mapId || !bg->isArena() || bg->isRated())
    {
        error = "the created battleground is not the manifest's unrated arena on map " + std::to_string(m.arena.mapId);
        delete bg;
        return false;
    }
    bg->SetStartTime(0);
    bg->SetLastResurrectTime(0);
    sBattlegroundMgr->AddBattleground(bg);
    _arenaInstance = bg->GetInstanceID();
    _arenaType = m.arena.battlegroundTypeId;
    for (std::size_t i = 0; i < kScenarioActors; ++i)
    {
        Player* player = Find(i);
        TeamId const team = Side(m.actors[i]);
        bg->IncreaseInvitedCount(team);
        player->SetEntryPoint();
        player->SetBattlegroundId(bg->GetInstanceID(), bg->GetBgTypeID(), PLAYER_MAX_BATTLEGROUND_QUEUES, true, false, team);
        if (!sBattlegroundMgr->SendToBattleground(player, bg->GetInstanceID(), type))
        {
            bg->DecreaseInvitedCount(team);
            player->SetBattlegroundId(0, BATTLEGROUND_TYPE_NONE, PLAYER_MAX_BATTLEGROUND_QUEUES, false, false, TEAM_NEUTRAL);
            error = "could not send " + m.actors[i].name + " to the arena";
            return false;
        }
    }
    LOG_INFO("module.dojo", "mod-dojo: opened training arena instance {} (map {}, unrated, one actor per side)",
        _arenaInstance, m.arena.mapId);
    return true;
}

bool Director::InspectArena(std::string& report)
{
    Manifest const& m = Scenario();
    Battleground* bg = Arena();
    if (!bg)
    {
        report = "the arena instance is gone";
        return false;
    }
    Map* map = bg->FindBgMap();
    std::vector<std::string> problems;
    std::array<Observation, kScenarioActors> seen;
    json actors = json::array();
    for (std::size_t i = 0; i < kScenarioActors; ++i)
    {
        Player* player = Find(i);
        ActorSpec const& spec = m.actors[i];
        if (!player)
        {
            problems.push_back(spec.name + " is offline");
            continue;
        }
        seen[i] = Observe(player);
        float const dx = player->GetPositionX() - float(spec.start.x);
        float const dy = player->GetPositionY() - float(spec.start.y);
        float const dz = player->GetPositionZ() - float(spec.start.z);
        float const away = std::sqrt(dx * dx + dy * dy + dz * dz);
        float const ground = map ? map->GetHeight(player->GetPhaseMask(), float(spec.start.x), float(spec.start.y),
            float(spec.start.z) + 2.0f) : INVALID_HEIGHT;
        bool const onGround = ground > INVALID_HEIGHT && std::fabs(ground - float(spec.start.z)) <= kGroundTolerance;
        Bounds const& b = m.arena.bounds;
        bool const inBounds = std::hypot(double(player->GetPositionX()) - b.x, double(player->GetPositionY()) - b.y) <= b.radius
            && player->GetPositionZ() >= b.zMin && player->GetPositionZ() <= b.zMax;
        bool const inArena = player->GetMapId() == m.arena.mapId && player->GetInstanceId() == bg->GetInstanceID()
            && player->GetBattleground() == bg && player->GetBgTeamId() == Side(spec);
        std::vector<std::string> loadout = CheckLoadout(m.loadout, seen[i]);
        if (!inArena)
            problems.push_back(spec.name + " is not in the training arena on its side");
        if (away > kArrivalTolerance)
            problems.push_back(spec.name + " is " + std::to_string(away) + " yd from its start");
        if (!onGround)
            problems.push_back(spec.name + "'s start is not on the ground (height " + std::to_string(ground) + ")");
        if (!inBounds)
            problems.push_back(spec.name + " is outside the arena bounds");
        for (std::string const& d : loadout)
            problems.push_back(spec.name + ": " + d);
        actors.push_back({{"slot", i}, {"name", spec.name}, {"guid", player->GetGUID().GetCounter()}, {"team", spec.team},
            {"map", player->GetMapId()}, {"instance", player->GetInstanceId()},
            {"position", {Hundredths(player->GetPositionX()), Hundredths(player->GetPositionY()),
                Hundredths(player->GetPositionZ()), Hundredths(player->GetOrientation())}},
            {"start_distance_cm", std::llround(double(away) * 100.0)}, {"ground_height_cm", Hundredths(ground)},
            {"in_arena", inArena}, {"in_bounds", inBounds}, {"on_ground", onGround},
            {"loadout_ok", loadout.empty()}, {"fingerprint", Fingerprint(seen[i])},
            {"observation", ObservationToJson(seen[i])}});
    }
    std::vector<std::string> const diff = CompareActors(seen[0], seen[1]);
    for (std::string const& d : diff)
        problems.push_back(d);
    bool los = false;
    if (map)
        los = map->isInLineOfSight(float(m.actors[0].start.x), float(m.actors[0].start.y), float(m.actors[0].start.z) + 2.0f,
            float(m.actors[1].start.x), float(m.actors[1].start.y), float(m.actors[1].start.z) + 2.0f,
            PHASEMASK_NORMAL, LINEOFSIGHT_ALL_CHECKS, VMAP::ModelIgnoreFlags::Nothing);
    if (_stockCasts)
        problems.push_back(std::to_string(_stockCasts) + " casts by fixtures that no executor command asked for");
    json evidence = {
        {"kind", "leeroy-dojo-arena-evidence"}, {"format", 1}, {"scenario", m.id}, {"manifest_sha256", m.sha256},
        {"instance", State().settings.instance}, {"arena_instance", bg->GetInstanceID()}, {"map", bg->GetMapId()},
        {"battleground_type", int(bg->GetBgTypeID())}, {"arena_type", bg->GetArenaType()}, {"rated", bg->isRated()},
        {"status", int(bg->GetStatus())}, {"players", bg->GetPlayersSize()},
        {"invited", {bg->GetInvitedCount(TEAM_ALLIANCE), bg->GetInvitedCount(TEAM_HORDE)}},
        {"arena_team_ids", {bg->GetArenaTeamIdForTeam(TEAM_ALLIANCE), bg->GetArenaTeamIdForTeam(TEAM_HORDE)}},
        {"start_line_of_sight", los}, {"identical", diff.empty()}, {"actors", actors},
        {"fixture_casts", {{"stock", _stockCasts}, {"executor", _executorCasts}, {"triggered", _triggeredCasts}}},
        {"problems", problems}, {"ok", problems.empty()}};
    _evidence = CanonicalJson(evidence);
    report = problems.empty() ? "both actors in the training arena with identical verified loadouts"
                              : std::to_string(problems.size()) + " problems: " + problems.front();
    return problems.empty();
}

void Director::Update(std::uint32_t diff)
{
    if (!State().scenarioReady)
        return;
    Control::Instance().Update();
    if (_job == Job::None)
        return;
    _stepMs += diff;
    bool const first = !_entered;
    _entered = true;
    Manifest const& m = Scenario();

    switch (_step)
    {
        case Step::Login:
        {
            if (first)
                for (std::size_t i = 0; i < kScenarioActors; ++i)
                    if (!Find(i))
                        sRandomPlayerbotMgr.AddPlayerBot(ObjectGuid(_actors[i].guid), 0);
            bool settled = AllOnline();
            for (std::size_t i = 0; settled && i < kScenarioActors; ++i)
                settled = !Find(i)->IsBeingTeleported() && Control::Instance().Owns(i);
            if (settled)
                Advance(_job == Job::Build || _job == Job::Restore ? Step::Stage : Step::Verify);
            else if (StepTimedOut(kLoginMs))
                Fail("the fixtures did not log in within " + std::to_string(kLoginMs / 1000) + " s");
            break;
        }
        case Step::Stage:
        {
            FixtureSpec const& f = m.fixture;
            bool arrived = true;
            for (std::size_t i = 0; i < kScenarioActors; ++i)
            {
                Player* player = Find(i);
                if (!player)
                {
                    Fail(m.actors[i].name + " went offline");
                    return;
                }
                bool const there = player->GetMapId() == f.stagingMap
                    && player->GetDistance(float(f.staging.x), float(f.staging.y), float(f.staging.z)) < 5.0f;
                if (player->IsBeingTeleported())
                    arrived = false;
                else if (!there)
                {
                    arrived = false;
                    if (first || _stepMs % 5000 < diff)
                        player->TeleportTo(f.stagingMap, float(f.staging.x), float(f.staging.y), float(f.staging.z), float(f.staging.o));
                }
            }
            if (arrived)
                Advance(Step::Apply);
            else if (StepTimedOut(kTeleportMs))
                Fail("the fixtures did not reach the staging point");
            break;
        }
        case Step::Apply:
        {
            for (std::size_t i = 0; i < kScenarioActors; ++i)
            {
                std::string error;
                if (_job == Job::Restore)
                {
                    Player* player = Find(i);
                    Control::RestoreBaseline(player);
                    player->SaveToDB(false, false);
                }
                else if (!ApplyLoadout(Find(i), error))
                {
                    Fail(m.actors[i].name + ": " + error);
                    return;
                }
            }
            json actors = json::array();
            std::vector<std::string> problems;
            std::array<Observation, kScenarioActors> seen;
            for (std::size_t i = 0; i < kScenarioActors; ++i)
            {
                seen[i] = Observe(Find(i));
                for (std::string const& d : CheckLoadout(m.loadout, seen[i]))
                    problems.push_back(m.actors[i].name + ": " + d);
                actors.push_back({{"name", m.actors[i].name}, {"fingerprint", Fingerprint(seen[i])},
                    {"observation", ObservationToJson(seen[i])}});
            }
            for (std::string const& d : CompareActors(seen[0], seen[1]))
                problems.push_back(d);
            _evidence = CanonicalJson(json{{"kind", "leeroy-dojo-build-evidence"}, {"format", 1}, {"scenario", m.id},
                {"manifest_sha256", m.sha256}, {"actors", actors}, {"problems", problems}, {"ok", problems.empty()}});
            LogoutAll();
            _jobOk = problems.empty();
            _log.push_back(problems.empty() ? "loadout matches the manifest" : std::to_string(problems.size())
                + " differences from the manifest: " + problems.front());
            Advance(Step::Logout);
            break;
        }
        case Step::Verify:
        {
            std::array<Observation, kScenarioActors> seen;
            for (std::size_t i = 0; i < kScenarioActors; ++i)
            {
                Player* player = Find(i);
                if (!player || player->InBattleground() || player->GetMapId() != m.fixture.stagingMap)
                {
                    Fail(m.actors[i].name + " is not at the staging point (dojo fixture build)");
                    return;
                }
                seen[i] = Observe(player);
                std::vector<std::string> d = CheckLoadout(m.loadout, seen[i]);
                if (!d.empty())
                {
                    Fail(m.actors[i].name + " differs from the manifest (dojo fixture build): " + d.front());
                    return;
                }
            }
            for (std::size_t i = 0; i < kScenarioActors; ++i)
                RecordBaseline(i, Find(i));
            std::vector<std::string> const d = CompareActors(seen[0], seen[1]);
            if (!d.empty())
            {
                Fail("the actors differ: " + d.front());
                return;
            }
            Advance(Step::Send);
            break;
        }
        case Step::Send:
        {
            std::string error;
            if (!OpenArena(error))
            {
                Fail(error);
                return;
            }
            Advance(Step::Arrive);
            break;
        }
        case Step::Arrive:
        {
            Battleground* bg = Arena();
            bool arrived = bg && bg->GetPlayersSize() == kScenarioActors;
            for (std::size_t i = 0; arrived && i < kScenarioActors; ++i)
            {
                Player* player = Find(i);
                arrived = player && !player->IsBeingTeleported() && player->GetBattleground() == bg;
            }
            if (arrived)
                Advance(Step::InspectArena);
            else if (!bg)
                Fail("the arena closed before the actors arrived");
            else if (StepTimedOut(kTeleportMs))
                Fail("the actors did not arrive in the arena");
            break;
        }
        case Step::InspectArena:
        {
            // One tick after arrival, so the arena's own join work (team markers, preparation) has run.
            if (_stepMs < 1000)
                break;
            std::string report;
            if (!InspectArena(report))
            {
                Fail(report);
                return;
            }
            Finish(true, report + " (arena instance " + std::to_string(_arenaInstance) + ")");
            break;
        }
        case Step::Leave:
        {
            bool left = true;
            for (std::size_t i = 0; i < kScenarioActors; ++i)
            {
                Player* player = Find(i);
                if (!player)
                    continue;
                if (player->IsBeingTeleported())
                    left = false;
                else if (Battleground* bg = player->GetBattleground(true))
                {
                    left = false;
                    if (first || _stepMs % 5000 < diff)
                        player->LeaveBattleground(bg);
                }
            }
            bool const timedOut = !left && StepTimedOut(kTeleportMs);
            if (left || timedOut)
            {
                if (timedOut)
                    _log.push_back("the actors did not leave the arena in time; logging them out there");
                std::uint32_t restored = 0;
                for (std::size_t i = 0; i < kScenarioActors; ++i)
                    if (Player* player = Find(i))
                        restored += RestoreBaseline(i, player);
                _log.push_back("removed " + std::to_string(restored) + " auras the arena left behind");
                LogoutAll();
                Advance(Step::Logout);
            }
            break;
        }
        case Step::Logout:
        {
            bool const offline = !ObjectAccessor::FindConnectedPlayer(ObjectGuid(_actors[0].guid))
                && !ObjectAccessor::FindConnectedPlayer(ObjectGuid(_actors[1].guid));
            if (offline)
            {
                if (_job == Job::Build || _job == Job::Restore)
                    Finish(_jobOk, _log.empty() ? "built" : _log.back());
                else
                    Advance(Step::ArenaGone);
            }
            else if (StepTimedOut(kLogoutMs))
                Fail("the fixtures did not log out");
            break;
        }
        case Step::ArenaGone:
        {
            if (!Arena() || StepTimedOut(kLogoutMs))
            {
                bool const gone = !Arena();
                std::uint32_t const instance = _arenaInstance;
                _arenaInstance = 0;
                _closing = false;
                std::string closed = gone ? "arena instance " + std::to_string(instance) + " closed empty, no outcome recorded"
                                          : "arena instance " + std::to_string(instance) + " still exists";
                for (std::string const& note : _log)
                    closed += "; " + note;
                if (!_failure.empty())
                    Finish(false, _failure + "; " + closed);
                else
                    Finish(gone, closed);
            }
            break;
        }
        default:
            break;
    }
}

std::string Director::Status() const
{
    WorkerState const& state = State();
    std::string out = "dojo: " + std::string(StateName(state.plan.state)) + " (" + state.plan.reason + ")\n";
    if (state.manifest)
        out += "scenario " + state.manifest->id + " sha256 " + state.manifest->sha256 + (state.scenarioReady ? ", ready\n" : ", NOT ready\n");
    else
        out += "no scenario loaded\n";
    for (std::size_t i = 0; i < kScenarioActors; ++i)
    {
        if (!state.manifest)
            break;
        Player* player = Find(i);
        out += "actor " + std::to_string(i) + " " + state.manifest->actors[i].name + ": "
            + (player ? "online, map " + std::to_string(player->GetMapId()) + ", instance " + std::to_string(player->GetInstanceId())
                + (Control::Instance().Owns(i) ? ", owned by the external controller" : ", not owned")
                      : std::string("offline")) + "\n";
    }
    out += "arena: " + (_arenaInstance ? "instance " + std::to_string(_arenaInstance) + (_closing ? " (closing)" : " open") : std::string("none")) + "\n";
    out += "job: " + (_job == Job::None ? std::string("none") : _jobName + " at " + StepName(int(_step))) + "\n";
    out += "result: " + _result + "\n";
    out += "fixture casts since open: " + std::to_string(_stockCasts) + " stock, " + std::to_string(_executorCasts) + " executor, "
        + std::to_string(_triggeredCasts) + " triggered\n";
    if (!_evidence.empty())
        out += "dojo-evidence " + _evidence;
    return out;
}
}

#endif
