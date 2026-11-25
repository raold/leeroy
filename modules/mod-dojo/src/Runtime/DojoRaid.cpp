// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.
#include "DojoRaid.h"

#if DOJO_TRAINING_BUILD
#if defined(DOJO_RAID_ACCEPTANCE) && DOJO_RAID_ACCEPTANCE
#include "DojoRuntime.h"
#include "AccountMgr.h"
#include "CharacterCache.h"
#include "Chat.h"
#include "CommandScript.h"
#include "Creature.h"
#include "CreatureAI.h"
#include "Engine.h"
#include "Group.h"
#include "GroupMgr.h"
#include "InstanceScript.h"
#include "Map.h"
#include "MotionMaster.h"
#include "ObjectAccessor.h"
#include "ObjectMgr.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "PlayerbotAIConfig.h"
#include "Playerbots.h"
#include "RandomPlayerbotMgr.h"
#include "UnitScript.h"
#include "World.h"
#include "WorldScript.h"
#include "WorldSession.h"
#include "Guild/HazardJob.h"
#include "DBCStores.h"
#include "InstanceSaveMgr.h"
#include "MapMgr.h"
#include "PlayerbotFactory.h"
#include "PlayerScript.h"
#include "Spell.h"
#include "SpellInfo.h"
#include <algorithm>
#include <array>
#include <mutex>
#include <sstream>
#include <string>
#include <vector>

using nlohmann::json;
using namespace Acore::ChatCommands;

namespace
{
// Fixed synthetic account and characters; no command accepts a live character GUID.
constexpr char const* Account = "DOJORAID";
constexpr std::array<char const*, 2> Names{"Dojopaladin", "Dojoreserve"};
constexpr std::array<uint8, 2> Classes{CLASS_PALADIN, CLASS_DRUID};
constexpr std::array<uint8, 2> Races{RACE_HUMAN, RACE_NIGHTELF};
constexpr uint32 Timeout = 120000;

struct RaidRun
{
    std::string scenario, token, state = "idle", failure;
    bool control = false;
    bool oldTwins = false;
    uint32 elapsed = 0, phaseMs = 0, map = 0, instance = 0, samples = 0, maxGap = 0;
    uint32 crossings = 0, hazardsSeen = 0, lightSamples = 0, darkSamples = 0, switchMs = 0;
    uint32 heals = 0, damageHits = 0;
    uint64 effectiveHealing = 0;
    uint64 ownerEpoch = 0;
    float minimumClearance = 1000.f;
    bool setup = false, active = false, cleanup = false, combatObserved = false, competition = false;
    ObjectGuid target;
    std::array<ObjectGuid, 2> actors{};
    std::recursive_mutex events;

    Player* Actor(std::size_t i) const
    {
        Player* p = ObjectAccessor::FindConnectedPlayer(actors[i]);
        return p && p->IsInWorld() && !p->IsDuringRemoveFromWorld() && GET_PLAYERBOT_AI(p) ? p : nullptr;
    }

    void Phase(std::string const& next) { state = next; phaseMs = 0; }
    void Fail(std::string const& why) { failure = why; Phase("cleanup"); }

    std::string Create()
    {
        uint32 const account = AccountMgr::GetId(Account);
        if (!account) return "create the isolated DOJORAID account with realm accounts --fixture-account DOJORAID";
        for (std::size_t i = 0; i < actors.size(); ++i)
        {
            ObjectGuid guid = sCharacterCache->GetCharacterGuidByName(Names[i]);
            if (!guid.IsEmpty())
            {
                if (sCharacterCache->GetCharacterAccountIdByGuid(guid) != account)
                    return "fixture name belongs to another account";
                if (ObjectAccessor::FindConnectedPlayer(guid)) return "fixture already online";
                actors[i] = guid;
                continue;
            }
            WorldSession* session = new WorldSession(account, std::string(), 0, nullptr, SEC_PLAYER,
                EXPANSION_WRATH_OF_THE_LICH_KING, time_t(0), sWorld->GetDefaultDbcLocale(), 0, false, false, 0);
            CharacterCreateInfo info(Names[i], Races[i], Classes[i], GENDER_MALE, 0, 0, 0, 0, 0);
            Player* p = new Player(session);
            p->GetMotionMaster()->Initialize();
            bool const created = p->Create(sObjectMgr->GetGenerator<HighGuid::Player>().Generate(), &info);
            if (created)
            {
                p->setCinematic(2);
                p->SetAtLoginFlag(AT_LOGIN_NONE);
                p->SaveToDB(true, false);
                sCharacterCache->AddCharacterCacheEntry(p->GetGUID(), account, p->GetName(), p->getGender(),
                    p->getRace(), p->getClass(), p->GetLevel());
                actors[i] = p->GetGUID();
            }
            p->CleanupsBeforeDelete();
            delete p;
            delete session;
            if (!created) return "core refused fixture creation";
        }
        return "";
    }

    std::string Start(std::string const& name, std::string const& id, bool negative)
    {
        if (active || state == "cleanup_failed") return "refused: a raid run or unproved cleanup is active";
        if (Dojo::State().plan.state != Dojo::RuntimeState::Armed || Dojo::State().manifest)
            return "refused: use an armed isolated training realm with no arena manifest";
        if (name != "valithria" && name != "twins" && name != "poison") return "refused: unknown scenario";
        if (id.empty() || id.size() > 64 || id.find_first_not_of("abcdefghijklmnopqrstuvwxyz0123456789-") != std::string::npos)
            return "refused: invalid run token";
        std::string error = Create();
        if (!error.empty()) return "refused: " + error;
        std::lock_guard<std::recursive_mutex> lock(events);
        scenario = name; token = id; control = negative; failure.clear();
        elapsed = phaseMs = instance = samples = maxGap = crossings = hazardsSeen = lightSamples = darkSamples = switchMs = 0;
        heals = damageHits = 0; effectiveHealing = ownerEpoch = 0; minimumClearance = 1000.f;
        setup = cleanup = combatObserved = competition = false; target.Clear();
        map = name == "valithria" ? 631 : name == "twins" ? 649 : 533;
        oldTwins = sPlayerbotAIConfig.TwinValkyrInteractions;
        sPlayerbotAIConfig.TwinValkyrInteractions = !negative;
        active = true;
        Phase("login");
        for (ObjectGuid guid : actors) sRandomPlayerbotMgr.AddPlayerBot(guid, 0);
        return "started";
    }

    Position StartPoint() const
    {
        if (scenario == "valithria") return Position(4203.5f, 2484.0f, 364.96f, 0.f);
        if (scenario == "twins") return Position(563.5f, 139.5f, 393.84f, 0.f);
        return Position(3108.0f, -3141.0f, 293.34f, 0.f);
    }

    bool Prepare()
    {
        Player* bot = Actor(0);
        Player* owner = Actor(1);
        for (std::size_t i = 0; i < actors.size(); ++i)
        {
            Player* p = Actor(i);
            if (p->GetGroup() || p->getClass() != Classes[i]) { Fail("fixture group or class is not clean"); return false; }
            if (!p->IsAlive()) { p->ResurrectPlayer(1.f); p->SpawnCorpseBones(); }
            p->GiveLevel(80);
            p->InitTalentForLevel();
            p->RemoveAllAuras();
            p->RemoveAllSpellCooldown();
            p->SetMaxHealth(100000);
            p->SetFullHealth();
            p->SetMaxPower(POWER_MANA, 30000);
            p->SetPower(POWER_MANA, 30000);
            p->SetDungeonDifficulty(DUNGEON_DIFFICULTY_NORMAL);
            p->SetRaidDifficulty(RAID_DIFFICULTY_10MAN_NORMAL);
            GET_PLAYERBOT_AI(p)->ResetStrategies(false);
        }
        // A legal first-tier Holy talent fixes the specialization. Ordinary stock
        // healing/reach strategies remain active, including the regression competitor.
        bot->resetTalents(true);
        bot->LearnTalent(1432, 4); // Spiritual Focus, 5/5 (Talent.dbc)
        if (!bot->HasTalent(20208, bot->GetActiveSpec())) { Fail("Holy fixture talent refused"); return false; }
        bot->learnSpell(48782, false); // Holy Light, rank 13
        bot->learnSpell(48785, false); // Flash of Light, rank 9
        // Stock Valithria allocation reserves the restoration druid for the
        // raid, leaving the measured paladin on the dragon even when the distant
        // raid member is hurt enough to qualify for stock heal-reach selection.
        owner->resetTalents(true);
        owner->LearnTalent(823, 2); // Nature's Focus, 3/3 (Talent.dbc, restoration)
        if (!owner->HasTalent(17066, owner->GetActiveSpec())) { Fail("restoration fixture talent refused"); return false; }
        GET_PLAYERBOT_AI(owner)->ResetStrategies(false);
        PlayerbotAI* ai = GET_PLAYERBOT_AI(bot);
        ai->ResetStrategies(false);
        ai->ChangeStrategy("+heal", BOT_STATE_COMBAT);
        ai->SetMaster(owner);
        // Learning/resetting talents recalculates stats. Apply the fixture
        // reserves afterwards, so the Touch stimulus does not kill an ungeared bot.
        bot->SetMaxHealth(100000);
        bot->SetFullHealth();
        bot->SetMaxPower(POWER_MANA, 30000);
        bot->SetPower(POWER_MANA, 30000);
        GET_PLAYERBOT_AI(owner)->ChangeStrategy("+passive,+stay", BOT_STATE_COMBAT);
        GET_PLAYERBOT_AI(owner)->ChangeStrategy("+passive,+stay", BOT_STATE_NON_COMBAT);
        Group* group = new Group();
        if (!group->Create(owner)) { delete group; Fail("raid group creation failed"); return false; }
        sGroupMgr->AddGroup(group);
        group->ConvertToRaid();
        if (!group->AddMember(bot)) { group->Disband(); Fail("fixture join failed"); return false; }
        Position const point = StartPoint();
        for (std::size_t i = 0; i < actors.size(); ++i)
            if (!Actor(i)->TeleportTo(map, point.GetPositionX(), point.GetPositionY() + float(i) * 2.f,
                    point.GetPositionZ(), 0.f)) { Fail("fixture teleport refused"); return false; }
        return true;
    }

    void Pull()
    {
        Player* bot = Actor(0);
        Player* owner = Actor(1);
        PlayerbotAI* ai = GET_PLAYERBOT_AI(bot);
        InstanceScript* script = bot->GetInstanceScript();
        if (!script) { Fail("no native instance script"); return; }
        instance = bot->GetInstanceId();
        // The leader is a scripted stimulus, not a second tested policy. Its
        // ordinary AI must not move it or cast while the measured bot stays stock.
        ownerEpoch = GET_PLAYERBOT_AI(owner)->AcquireExternalControl("dojo-raid-leader");
        if (!ownerEpoch) { Fail("could not hold the scripted leader"); return; }
        owner->StopMoving();
        // Far-teleport acknowledgement may have rebuilt the strategies.
        ai->SetMaster(owner);
        ai->ChangeStrategy("-stay,-passive,+heal", BOT_STATE_COMBAT);
        ai->ChangeStrategy("-stay,-passive,+follow", BOT_STATE_NON_COMBAT);
        GET_PLAYERBOT_AI(owner)->ChangeStrategy("+passive,+stay", BOT_STATE_COMBAT);
        GET_PLAYERBOT_AI(owner)->ChangeStrategy("+passive,+stay", BOT_STATE_NON_COMBAT);
        if (scenario == "valithria")
        {
            Creature* dragon = bot->FindNearestCreature(36789, 150.f);
            if (!dragon || !PlayerbotAI::IsHeal(bot) || !PlayerbotAI::IsHeal(owner))
            { Fail("Valithria or stock healer roles missing"); return; }
            target = dragon->GetGUID();
            dragon->SetHealth(dragon->GetMaxHealth() / 2); // setup only; never altered during the observation
            // The native encounter rejects a sequence break by teleporting the
            // raid out. Establish the completed lower-spire prerequisite only.
            for (uint32 boss = 0; boss < 4; ++boss) script->SetBossState(boss, DONE);
            Creature* trigger = bot->GetMap()->GetCreature(script->GetGuidData(35));
            if (!trigger) { Fail("native Valithria combat trigger missing"); return; }
            owner->NearTeleportTo(bot->GetPositionX() - 55.f, bot->GetPositionY(), bot->GetPositionZ(), 0.f);
            owner->SetHealth(owner->GetMaxHealth() * 65 / 100);
            ai->ChangeStrategy(control ? "-icc" : "+icc", BOT_STATE_COMBAT);
            ai->ChangeStrategy(control ? "-icc" : "+icc", BOT_STATE_NON_COMBAT);
            trigger->AI()->DoZoneInCombat(); // real encounter/archmage combat, not friendly fake combat
        }
        else if (scenario == "twins")
        {
            // Real core scripts register the twins with the instance and create
            // the real gossip essence NPCs. No aura is granted to a fixture.
            Creature* light = bot->SummonCreature(34497, 563.5f, 150.f, 393.84f, 0.f, TEMPSUMMON_TIMED_DESPAWN, 90000);
            Creature* dark = bot->SummonCreature(34496, 563.5f, 120.f, 393.84f, 0.f, TEMPSUMMON_TIMED_DESPAWN, 90000);
            if (!light || !dark) { Fail("Twin fixtures failed to summon"); return; }
            bot->SummonCreature(34568, 552.f, 139.5f, 393.84f, 0.f, TEMPSUMMON_TIMED_DESPAWN, 90000);
            bot->SummonCreature(34567, 575.f, 139.5f, 393.84f, 0.f, TEMPSUMMON_TIMED_DESPAWN, 90000);
            target = dark->GetGUID();
            // A fresh ToC instance starts with TO_BE_DECIDED until its announcer
            // progression prepares this fight. Establish that ready state.
            script->SetBossState(3, NOT_STARTED);
            ai->ChangeStrategy("+toc", BOT_STATE_NON_COMBAT);
            ai->ChangeStrategy("+toc", BOT_STATE_COMBAT);
        }
        else
        {
            Leeroy::Guild::InstallHazardFilter();
            Leeroy::Guild::DojoHazardOwner(control ? 0 : owner->GetGUID().GetCounter());
            // Under a game client master playerbots builds the combat engine with avoid aoe as well
            // (AutoAvoidAoe), the strategy that runs the hazard exit; these engines were built under a bot master.
            if (!control && sPlayerbotAIConfig.AutoAvoidAoe)
                ai->ChangeStrategy("+avoid aoe", BOT_STATE_COMBAT);
            owner->NearTeleportTo(3195.f, -3148.f, 293.34f, 3.14f);
        }
        setup = true;
        Phase("observe");
    }

    void Observe(uint32 diff)
    {
        if (scenario == "poison" && control && damageHits > 0) { Phase("cleanup"); return; }
        Player* bot = Actor(0);
        Player* owner = Actor(1);
        if (!bot || !owner || !bot->IsAlive() || !owner->IsAlive()) { Fail("fixture died or disconnected"); return; }
        // Stock ICC movement can acknowledge a same-instance floor correction.
        // Wait for the acknowledgement, then verify the actual destination.
        if (bot->IsBeingTeleported() || owner->IsBeingTeleported()) return;
        if (bot->GetMapId() != map || bot->GetInstanceId() != instance ||
            owner->GetMap() != bot->GetMap()) { Fail("fixture left the measured instance"); return; }
        ++samples;
        maxGap = std::max(maxGap, diff);
        combatObserved = combatObserved || bot->IsInCombat();
        // These actors are mechanic probes, not a geared raid. Keep their
        // reserves stable across instance buffs and spell stat recalculations.
        // Damage events still count, and Valithria's health is never written here.
        for (Player* p : {bot, owner})
        {
            p->SetMaxHealth(100000);
            p->SetFullHealth();
            p->SetMaxPower(POWER_MANA, 30000);
            p->SetPower(POWER_MANA, 30000);
        }
        if (scenario == "valithria")
        {
            // Keep the distant stock reach-to-heal competitor eligible, and
            // mana available. Never cast for the healer or write dragon HP here.
            // Stock target selection adds 30 points beyond HealDistance and
            // rejects scores >=100. A reserved raid healer at 65% is eligible;
            // the dragon-assigned paladin must still prioritize Valithria.
            owner->SetHealth(owner->GetMaxHealth() * 65 / 100);
            bot->SetPower(POWER_MANA, bot->GetMaxPower(POWER_MANA));
            Unit* healTarget = GET_PLAYERBOT_AI(bot)->GetAiObjectContext()->GetValue<Unit*>("party member to heal")->Get();
            competition = competition || (healTarget == owner && bot->GetDistance(owner) > 40.f &&
                owner->GetHealthPct() >= 60.f && owner->GetHealthPct() < 70.f && bot->IsWithinLOSInMap(owner));
            if (phaseMs >= 20000) Phase("cleanup");
        }
        else if (scenario == "twins")
        {
            if (bot->HasAura(65686)) ++lightSamples;
            if (switchMs && bot->HasAura(65684) && !bot->HasAura(65686)) ++darkSamples;
            if (lightSamples >= 5 && !switchMs)
            {
                // A real hostile spell is the stimulus. The stock strategy must
                // walk to gossip and acquire Dark Essence to answer Dark Touch.
                Creature* dark = bot->GetMap()->GetCreature(target);
                if (!dark) { Fail("dark twin disappeared"); return; }
                dark->CastSpell(bot, 66001, true);
                switchMs = phaseMs;
            }
            if (phaseMs >= 40000 || darkSamples >= 10) Phase("cleanup");
        }
        else
        {
            // The registered mod-leeroy world hook owns the production cadence.
            // A second tick here would scan twice as often as the live realm.
            // The filter serves only bots whose master is the owner, a game client, whose AI takes a turn every
            // ReactDelay (0.1 s); under a bot master playerbots waits 1-3 s out of combat (GetReactDelay). Between
            // teleports the held leader is a self-bot (its own master), which playerbots counts as a game client
            // master; a self-bot's teleport waits for a client's ack, so it is an ordinary bot while it moves.
            if (!control && GET_PLAYERBOT_AI(owner)->GetMaster() != owner)
                GET_PLAYERBOT_AI(owner)->SetMaster(owner);
            if (Creature* poison = bot->FindNearestCreature(16027, 70.f))
            {
                ++hazardsSeen;
                minimumClearance = std::min(minimumClearance, bot->GetExactDist2d(poison));
            }
            if (bot->GetExactDist2d(owner) < 6.f)
            {
                ++crossings;
                GET_PLAYERBOT_AI(owner)->SetMaster(nullptr);
                owner->NearTeleportTo(crossings % 2 ? 3108.f : 3195.f,
                    crossings % 2 ? -3141.f : -3148.f, 293.34f, 0.f);
            }
            if (phaseMs >= 70000) Phase("cleanup");
        }
    }

    void Update(uint32 diff)
    {
        std::lock_guard<std::recursive_mutex> lock(events);
        if (!active) return;
        elapsed += diff; phaseMs += diff;
        if (state != "cleanup" && state != "logout" && elapsed > Timeout) Fail("scenario deadline");
        if (state == "login")
        {
            if (Actor(0) && Actor(1) && Prepare()) Phase("arrive");
        }
        else if (state == "arrive")
        {
            Player* bot = Actor(0); Player* owner = Actor(1);
            if (bot && owner && !bot->IsBeingTeleported() && !owner->IsBeingTeleported() &&
                bot->GetMapId() == map && owner->GetMap() == bot->GetMap()) Pull();
        }
        else if (state == "observe") Observe(diff);
        else if (state == "cleanup")
        {
            Leeroy::Guild::DojoHazardOwner(0);
            Leeroy::Guild::TickHazards(500);
            sPlayerbotAIConfig.TwinValkyrInteractions = oldTwins;
            for (std::size_t i = 0; i < actors.size(); ++i)
                if (Player* p = Actor(i))
                {
                    p->CombatStop(true);
                    if (i == 1 && ownerEpoch) GET_PLAYERBOT_AI(p)->ReleaseExternalControl(ownerEpoch);
                    GET_PLAYERBOT_AI(p)->SetMaster(nullptr);
                    GET_PLAYERBOT_AI(p)->ChangeStrategy("+passive,+stay", BOT_STATE_NON_COMBAT);
                    if (Group* g = p->GetGroup()) g->Disband();
                    if (!p->IsAlive()) { p->ResurrectPlayer(1.f); p->SpawnCorpseBones(); }
                    p->TeleportTo(0, -8949.95f, -132.493f, 83.5312f, 0.f);
                }
            Phase("logout");
        }
        else if (state == "logout")
        {
            bool online = false;
            for (std::size_t i = 0; i < actors.size(); ++i)
                if (ObjectAccessor::FindConnectedPlayer(actors[i]))
                {
                    online = true;
                    Player* p = Actor(i);
                    if (p && !p->IsBeingTeleported() && p->GetMapId() == 0)
                        sRandomPlayerbotMgr.LogoutPlayerBot(actors[i]);
                }
            if (!online) { cleanup = true; active = false; Phase("done"); }
            else if (phaseMs > 60000) { failure += "; cleanup not proved"; active = false; Phase("cleanup_failed"); }
        }
    }

    json Status()
    {
        std::lock_guard<std::recursive_mutex> lock(events);
        json fixtures = json::array();
        for (std::size_t i = 0; i < actors.size(); ++i)
            if (Player* p = Actor(i))
            {
                PlayerbotAI* ai = GET_PLAYERBOT_AI(p);
                fixtures.push_back({{"slot", i}, {"map", p->GetMapId()}, {"instance", p->GetInstanceId()},
                    {"x", p->GetPositionX()}, {"y", p->GetPositionY()}, {"z", p->GetPositionZ()},
                    {"health", p->GetHealth()}, {"max_health", p->GetMaxHealth()}, {"mana", p->GetPower(POWER_MANA)},
                    {"teleporting", p->IsBeingTeleported()}, {"healer", PlayerbotAI::IsHeal(p)},
                    {"icc", ai->HasStrategy("icc", BOT_STATE_COMBAT)},
                    {"combat", p->IsInCombat()}, {"self_bot", IsSelfBot(p)},
                    {"controlled", ai->IsExternallyControlled()},
                    {"combat_strategies", ai->GetStrategies(BOT_STATE_COMBAT)},
                    {"idle_strategies", ai->GetStrategies(BOT_STATE_NON_COMBAT)},
                    {"last_action", ai->GetCurrentEngine() ? ai->GetCurrentEngine()->GetLastAction() : ""}});
            }
        json encounter = json::object();
        if (Player* p = Actor(0); p && p->GetInstanceScript())
        {
            InstanceScript* script = p->GetInstanceScript();
            encounter["boss_state"] = uint32(script->GetBossState(scenario == "valithria" ? 10 : scenario == "twins" ? 3 : 0));
            if (Player* owner = Actor(1))
            {
                encounter["leader_distance"] = p->GetDistance(owner);
                encounter["leader_los"] = p->IsWithinLOSInMap(owner);
            }
            if (scenario == "twins")
            {
                encounter["interactions"] = sPlayerbotAIConfig.TwinValkyrInteractions;
                for (uint32 data : {11u, 12u})
                {
                    Creature* c = p->GetMap()->GetCreature(script->GetGuidData(data));
                    encounter[std::to_string(data)] = {{"exists", c != nullptr},
                        {"alive", c && c->IsAlive()}, {"distance", c ? p->GetDistance(c) : -1.f}};
                }
            }
        }
        return {{"format", 1}, {"kind", "dojo-raid-observation"}, {"scenario", scenario}, {"token", token},
            {"state", state}, {"control", control}, {"setup", setup}, {"cleanup", cleanup}, {"failure", failure},
            {"map", map}, {"instance", instance}, {"elapsed_ms", elapsed}, {"samples", samples}, {"max_gap_ms", maxGap},
            {"combat_observed", combatObserved}, {"reach_competitor", competition}, {"effective_healing", effectiveHealing},
            {"heal_events", heals}, {"light_samples", lightSamples}, {"dark_samples", darkSamples},
            {"switch_ms", switchMs}, {"crossings", crossings}, {"hazard_samples", hazardsSeen},
            {"minimum_clearance", minimumClearance}, {"poison_hits", damageHits}, {"fixtures", fixtures}, {"encounter", encounter}};
    }
};
RaidRun run;

// A full Valithria encounter for one fixed ten-player bot raid: three healers (Discipline priest, Holy
// paladin, Restoration shaman: the 10-man healer template of the owner's guild fill), two tanks and five
// damage dealers, epic gear capped at item level 245 by playerbots' own factory. The mage leads as a
// self-bot, the owner's stand-in, so the others have a game-client master as in his raids. Nothing here
// heals, damages, moves or casts for a fixture during the pull; it records her health, deaths, portal
// trips, healing on her and mana spent. `before` pulls with Playerbots.ValithriaHealerFocus off, `after`
// with it on; the switch is restored at cleanup.
struct EncounterSlot
{
    char const* name;
    uint8 cls;
    uint8 race;
    char const* spec;
};

// Creation order fixes guid order: the priest is the lowest healer guid, so with no druid it is the
// reserved raid healer under either allocation, as the priest was in the 2026-11-25 10-heroic pull.
constexpr std::array<EncounterSlot, 10> Roster{{
    {"Dojovpriest", CLASS_PRIEST, RACE_HUMAN, "disc pve"},
    {"Dojovholy", CLASS_PALADIN, RACE_HUMAN, "holy pve"},
    {"Dojovshaman", CLASS_SHAMAN, RACE_DRAENEI, "resto pve"},
    {"Dojovwarrior", CLASS_WARRIOR, RACE_HUMAN, "prot pve"},
    {"Dojovguard", CLASS_PALADIN, RACE_HUMAN, "prot pve"},
    {"Dojovmage", CLASS_MAGE, RACE_HUMAN, "fire pve"},
    {"Dojovhunter", CLASS_HUNTER, RACE_DWARF, "mm pve"},
    {"Dojovlock", CLASS_WARLOCK, RACE_HUMAN, "affli pve"},
    {"Dojovrogue", CLASS_ROGUE, RACE_HUMAN, "combat pve"},
    {"Dojovstorm", CLASS_SHAMAN, RACE_DRAENEI, "ele pve"},
}};
constexpr std::size_t EncounterLeader = 5;
constexpr uint32 EncounterItemLevel = 245;
constexpr uint32 EncounterSetupMs = 240000;
constexpr uint32 EncounterPrePullMs = 15000;
constexpr uint32 EncounterCapMs = 15 * 60 * 1000;
constexpr uint32 DreamState = 70766;
constexpr uint32 EmeraldVigor = 70873;
constexpr uint32 TwistedNightmares = 71941;

int8 PremadeSpec(uint8 cls, char const* name)
{
    for (int8 spec = 0; spec < MAX_SPECNO; ++spec)
    {
        std::string const& specName = sPlayerbotAIConfig.PremadeSpecName[cls][spec];
        if (specName.empty())
            break;
        if (specName == name)
            return spec;
    }
    return -1;
}

bool IsManaCooldown(uint32 spell)
{
    switch (spell)
    {
        case 54428:  // Divine Plea
        case 16190:  // Mana Tide Totem
        case 29166:  // Innervate
        case 34433:  // Shadowfiend
        case 64901:  // Hymn of Hope
            return true;
        default:
            return false;
    }
}

struct EncounterMember
{
    uint32 deaths = 0, dreamEntries = 0, dreamMs = 0, inReachMs = 0, aliveMs = 0, lowManaMs = 0;
    uint32 heals = 0, manaCooldowns = 0, potions = 0, maxVigor = 0, maxTwisted = 0;
    uint64 healing = 0;
    float minMana = 100.f;
    bool alive = true, dreaming = false;
    // The raid as pulled, kept for the report after the fixtures log out.
    uint32 level = 0, itemLevel = 0, maxHealth = 0, maxMana = 0;
    bool isHeal = false;
};

struct EncounterRun
{
    std::string token, difficulty, mode, state = "idle", failure, outcome;
    bool active = false, setup = false, cleanup = false, engaged = false, earlyPull = false, oldFocus = true;
    uint32 elapsed = 0, phaseMs = 0, pullMs = 0, sampleMs = 0, instance = 0, mapDifficulty = 0, samples = 0;
    std::size_t prepared = 0;
    float startHp = 0.f, hp = 0.f, peakHp = 0.f;
    uint64 damageToHer = 0;
    ObjectGuid valithria;
    std::array<ObjectGuid, 10> actors{};
    std::array<EncounterMember, 10> members{};
    std::array<bool, 10> forcedLogout{};
    std::vector<std::pair<uint32, float>> timeline;
    std::recursive_mutex events;

    Player* Actor(std::size_t i) const
    {
        Player* p = ObjectAccessor::FindConnectedPlayer(actors[i]);
        return p && p->IsInWorld() && !p->IsDuringRemoveFromWorld() && GET_PLAYERBOT_AI(p) ? p : nullptr;
    }
    static bool Healer(std::size_t i) { return i < 3; }
    void Phase(std::string const& next) { state = next; phaseMs = 0; }
    void Fail(std::string const& why) { failure = why; Phase("cleanup"); }
    Difficulty Wanted() const
    {
        return difficulty == "10h" ? RAID_DIFFICULTY_10MAN_HEROIC : RAID_DIFFICULTY_10MAN_NORMAL;
    }

    std::string Create()
    {
        uint32 const account = AccountMgr::GetId(Account);
        if (!account) return "create the isolated DOJORAID account with realm accounts --fixture-account DOJORAID";
        for (std::size_t i = 0; i < Roster.size(); ++i)
        {
            ObjectGuid guid = sCharacterCache->GetCharacterGuidByName(Roster[i].name);
            if (!guid.IsEmpty())
            {
                if (sCharacterCache->GetCharacterAccountIdByGuid(guid) != account)
                    return "fixture name belongs to another account";
                if (ObjectAccessor::FindConnectedPlayer(guid)) return "fixture already online";
                actors[i] = guid;
                continue;
            }
            WorldSession* session = new WorldSession(account, std::string(), 0, nullptr, SEC_PLAYER,
                EXPANSION_WRATH_OF_THE_LICH_KING, time_t(0), sWorld->GetDefaultDbcLocale(), 0, false, false, 0);
            CharacterCreateInfo info(Roster[i].name, Roster[i].race, Roster[i].cls, GENDER_MALE, 0, 0, 0, 0, 0);
            Player* p = new Player(session);
            p->GetMotionMaster()->Initialize();
            bool const created = p->Create(sObjectMgr->GetGenerator<HighGuid::Player>().Generate(), &info);
            if (created)
            {
                p->setCinematic(2);
                p->SetAtLoginFlag(AT_LOGIN_NONE);
                p->SaveToDB(true, false);
                sCharacterCache->AddCharacterCacheEntry(p->GetGUID(), account, p->GetName(), p->getGender(),
                    p->getRace(), p->getClass(), p->GetLevel());
                actors[i] = p->GetGUID();
            }
            p->CleanupsBeforeDelete();
            delete p;
            delete session;
            if (!created) return std::string("core refused fixture creation: ") + Roster[i].name;
        }
        return "";
    }

    std::string Start(std::string const& id, std::string const& diff, std::string const& how, bool checkRunning)
    {
        if (active || state == "cleanup_failed") return "refused: an encounter run or unproved cleanup is active";
        if (checkRunning) return "refused: a raid check is running";
        if (Dojo::State().plan.state != Dojo::RuntimeState::Armed || Dojo::State().manifest)
            return "refused: use an armed isolated training realm with no arena manifest";
        if (diff != "10n" && diff != "10h") return "refused: difficulty is 10n or 10h";
        if (how != "before" && how != "after") return "refused: mode is before or after";
        if (id.empty() || id.size() > 64 ||
            id.find_first_not_of("abcdefghijklmnopqrstuvwxyz0123456789-") != std::string::npos)
            return "refused: invalid run token";
        std::string error = Create();
        if (!error.empty()) return "refused: " + error;
        std::lock_guard<std::recursive_mutex> lock(events);
        token = id; difficulty = diff; mode = how; failure.clear(); outcome.clear();
        setup = cleanup = engaged = earlyPull = false;
        elapsed = phaseMs = pullMs = sampleMs = instance = mapDifficulty = samples = 0;
        prepared = 0; startHp = hp = peakHp = 0.f; damageToHer = 0; valithria.Clear();
        members = {}; forcedLogout = {}; timeline.clear();
        oldFocus = sPlayerbotAIConfig.ValithriaHealerFocus;
        active = true;
        Phase("login");
        for (ObjectGuid guid : actors) sRandomPlayerbotMgr.AddPlayerBot(guid, 0);
        return "started";
    }

    // One fixture per tick. The first run gears it with playerbots' own factory (the premade spec, its
    // glyphs and epic gear capped at item level 245); later runs keep that gear, so every pull fields the
    // same raid. Each run refills potions and reagents as the guild's top-up does.
    bool PrepareOne(std::size_t i)
    {
        Player* p = Actor(i);
        EncounterSlot const& slot = Roster[i];
        if (p->GetGroup()) p->RemoveFromGroup();
        if (!p->IsAlive()) { p->ResurrectPlayer(1.f); p->SpawnCorpseBones(); }
        int8 const spec = PremadeSpec(slot.cls, slot.spec);
        if (spec < 0) { Fail(std::string("premade spec missing: ") + slot.spec); return false; }
        uint32 const gearScore = PlayerbotFactory::CalcMixedGearScore(EncounterItemLevel, ITEM_QUALITY_EPIC);
        if (p->GetLevel() < 80)
        {
            PlayerbotFactory randomizer(p, 80, ITEM_QUALITY_EPIC, gearScore);
            randomizer.Randomize(false);
            PlayerbotFactory::InitTalentsBySpecNo(p, spec, true);
            PlayerbotFactory factory(p, 80, ITEM_QUALITY_EPIC, gearScore);
            factory.InitGlyphs(false);
            factory.InitEquipment(false);
            p->SaveToDB(false, false);
        }
        PlayerbotFactory refill(p, 80, ITEM_QUALITY_EPIC, gearScore);
        refill.InitPotions();
        refill.InitReagents();
        if (slot.cls == CLASS_HUNTER) refill.InitAmmo();
        // The 10-player heroic door asks every member for The Frozen Throne (10 player): a fixture
        // prerequisite like the lower-spire boss states, never credit for this encounter.
        if (AchievementEntry const* frozenThrone = sAchievementStore.LookupEntry(4530))
            if (!p->HasAchieved(4530)) p->CompletedAchievement(frozenThrone);
        for (Difficulty d : {RAID_DIFFICULTY_10MAN_NORMAL, RAID_DIFFICULTY_10MAN_HEROIC})
            sInstanceSaveMgr->PlayerUnbindInstance(p->GetGUID(), 631, d, true, p);
        p->RemoveAllSpellCooldown();
        p->SetFullHealth();
        if (p->getPowerType() == POWER_MANA) p->SetPower(POWER_MANA, p->GetMaxPower(POWER_MANA));
        p->SetDungeonDifficulty(DUNGEON_DIFFICULTY_NORMAL);
        p->SetRaidDifficulty(Wanted());
        PlayerbotAI* ai = GET_PLAYERBOT_AI(p);
        ai->SetMaster(nullptr);
        ai->ResetStrategies(false);
        if (PlayerbotAI::IsHeal(p) != Healer(i) || p->getClass() != slot.cls)
        { Fail(std::string("fixture role or class is wrong: ") + slot.name); return false; }
        return true;
    }

    bool Form()
    {
        Player* leader = Actor(EncounterLeader);
        Group* group = new Group();
        if (!group->Create(leader)) { delete group; Fail("raid group creation failed"); return false; }
        sGroupMgr->AddGroup(group);
        group->ConvertToRaid();
        group->SetRaidDifficulty(Wanted());
        for (std::size_t i = 0; i < actors.size(); ++i)
            if (i != EncounterLeader && !group->AddMember(Actor(i)))
            { group->Disband(); Fail("fixture join failed"); return false; }
        // Into the instance at its entrance first: an archmage that sees a raider in her room before the
        // lower-spire states are set starts the encounter as a sequence break and ports everyone out.
        for (std::size_t i = 0; i < actors.size(); ++i)
            if (!Actor(i)->TeleportTo(631, 76.8638f - float(i % 5) * 2.f, 2211.37f + float(i / 5) * 2.f, 30.f, 3.14965f))
            { Fail("fixture teleport refused"); return false; }
        return true;
    }

    // The raid is in at the entrance: establish the lower-spire prerequisite, then walk-free into her room.
    bool Stage()
    {
        Player* leader = Actor(EncounterLeader);
        InstanceScript* script = leader->GetInstanceScript();
        if (!script) { Fail("no native instance script"); return false; }
        for (uint32 boss = 0; boss < 4; ++boss) script->SetBossState(boss, DONE);
        for (std::size_t i = 0; i < actors.size(); ++i)
            Actor(i)->NearTeleportTo(4203.5f + float(i % 5) * 2.f, 2484.0f + float(i / 5) * 2.f, 364.96f, 0.f);
        return true;
    }

    bool InRoom() const
    {
        for (std::size_t i = 0; i < actors.size(); ++i)
        {
            Player* p = Actor(i);
            if (!p || p->IsBeingTeleported() || p->GetInstanceId() != Actor(EncounterLeader)->GetInstanceId() ||
                p->GetExactDist2d(4203.5f, 2484.0f) > 30.f)
                return false;
        }
        return true;
    }

    bool Arrived() const
    {
        Player* leader = Actor(EncounterLeader);
        if (!leader || leader->IsBeingTeleported() || leader->GetMapId() != 631) return false;
        for (std::size_t i = 0; i < actors.size(); ++i)
        {
            Player* p = Actor(i);
            if (!p || p->IsBeingTeleported() || p->GetMap() != leader->GetMap()) return false;
        }
        return true;
    }

    // The native encounter refuses a sequence break; Stage() set only the lower-spire prerequisite.
    // The raid stands in her room out of combat for up to 15 s (buffs, as before any pull); then the real
    // combat trigger pulls the encounter, as engaging the archmages does. The first heal on her ends the
    // wait: healing her is how a raid starts this fight.
    // False until the instance has registered her (a fresh map may still be loading her grid).
    bool Assemble()
    {
        Player* leader = Actor(EncounterLeader);
        InstanceScript* script = leader->GetInstanceScript();
        if (!script) { Fail("no native instance script"); return false; }
        instance = leader->GetInstanceId();
        mapDifficulty = uint32(leader->GetMap()->GetDifficulty());
        if (leader->GetMap()->GetDifficulty() != Wanted()) { Fail("instance difficulty differs from the request"); return false; }
        Creature* dragon = leader->GetMap()->GetCreature(script->GetGuidData(10));
        if (!dragon || !dragon->IsAlive() || !dragon->IsInWorld()) return false;
        valithria = dragon->GetGUID();
        GET_PLAYERBOT_AI(leader)->SetMaster(leader);
        for (std::size_t i = 0; i < actors.size(); ++i)
        {
            PlayerbotAI* ai = GET_PLAYERBOT_AI(Actor(i));
            if (i != EncounterLeader) ai->SetMaster(leader);
            ai->ResetStrategies(false);
            ai->ChangeStrategy("+icc", BOT_STATE_COMBAT);
            ai->ChangeStrategy("+icc", BOT_STATE_NON_COMBAT);
        }
        sPlayerbotAIConfig.ValithriaHealerFocus = mode == "after";
        setup = true;
        Phase("prepull");
        return true;
    }

    // The measured instance, whoever is still in it: a dead member may release to the graveyard outside.
    InstanceMap* Instance() const
    {
        Map* map = instance ? sMapMgr->FindMap(631, instance) : nullptr;
        return map ? map->ToInstanceMap() : nullptr;
    }

    void Pull()
    {
        InstanceMap* map = Instance();
        InstanceScript* script = map ? map->GetInstanceScript() : nullptr;
        Creature* dragon = map ? map->GetCreature(valithria) : nullptr;
        Creature* trigger = script ? map->GetCreature(script->GetGuidData(35)) : nullptr;
        if (!dragon || !trigger) { Fail("Valithria or her combat trigger missing"); return; }
        startHp = hp = peakHp = dragon->GetHealthPct();
        // She resets to 50%. A healer topping her up before the pull is the pull, as in a real raid; the
        // run starts there, and her own health records what that heal gave.
        if (startHp < 49.f || startHp > 60.f) { Fail("Valithria did not start near 50%"); return; }
        timeline.emplace_back(0, startHp);
        for (std::size_t i = 0; i < actors.size(); ++i)
            if (Player* p = Actor(i))
            {
                EncounterMember& m = members[i];
                m.level = p->GetLevel();
                m.itemLevel = uint32(p->GetAverageItemLevel());
                m.maxHealth = p->GetMaxHealth();
                m.maxMana = p->getPowerType() == POWER_MANA ? p->GetMaxPower(POWER_MANA) : 0;
                m.isHeal = PlayerbotAI::IsHeal(p);
            }
        // A member close to an archmage may already have started it; then the core is pulling it.
        earlyPull = script->GetBossState(10) == IN_PROGRESS;
        if (!earlyPull) trigger->AI()->DoZoneInCombat();
        Phase("observe");
    }

    void Sample(uint32 step, Creature* dragon)
    {
        ++samples;
        if (dragon && dragon->IsAlive())
        {
            hp = dragon->GetHealthPct();
            peakHp = std::max(peakHp, hp);
        }
        if (timeline.empty() || pullMs - timeline.back().first >= 10000) timeline.emplace_back(pullMs, hp);
        uint32 alive = 0;
        for (std::size_t i = 0; i < actors.size(); ++i)
        {
            Player* p = Actor(i);
            EncounterMember& m = members[i];
            if (!p) continue;
            bool const living = p->IsAlive();
            if (m.alive && !living) ++m.deaths;
            m.alive = living;
            if (!living || p->GetInstanceId() != instance) continue;
            ++alive;
            m.aliveMs += step;
            if (!Healer(i)) continue;
            bool const dreaming = p->HasAura(DreamState);
            if (dreaming && !m.dreaming) ++m.dreamEntries;
            m.dreaming = dreaming;
            if (dreaming) m.dreamMs += step;
            if (Aura* a = p->GetAura(EmeraldVigor)) m.maxVigor = std::max<uint32>(m.maxVigor, a->GetStackAmount());
            if (Aura* a = p->GetAura(TwistedNightmares))
                m.maxTwisted = std::max<uint32>(m.maxTwisted, a->GetStackAmount());
            float const mana = p->GetPowerPct(POWER_MANA);
            m.minMana = std::min(m.minMana, mana);
            if (mana < 10.f) m.lowManaMs += step;
            if (!dreaming && dragon && p->IsWithinDistInMap(dragon, 40.f) && p->IsWithinLOSInMap(dragon))
                m.inReachMs += step;
        }
        if (!alive && engaged && outcome.empty()) outcome = "wipe";
    }

    void Observe(uint32 diff)
    {
        InstanceMap* map = Instance();
        InstanceScript* script = map ? map->GetInstanceScript() : nullptr;
        if (!script) { Fail("the measured instance unloaded"); return; }
        Creature* dragon = map->GetCreature(valithria);
        EncounterState const boss = script->GetBossState(10);
        pullMs += diff;
        if (boss == IN_PROGRESS) engaged = true;
        sampleMs += diff;
        if (sampleMs >= 250)
        {
            Sample(sampleMs, dragon);
            sampleMs = 0;
        }
        if (!engaged && pullMs > 60000) { Fail("the encounter never engaged"); return; }
        if (outcome.empty())
        {
            if (boss == DONE) outcome = "done";
            else if (engaged && boss == NOT_STARTED) outcome = "reset";
            else if (pullMs >= EncounterCapMs) outcome = "timeout";
        }
        if (!outcome.empty())
        {
            timeline.emplace_back(pullMs, hp);
            Phase("cleanup");
        }
    }

    void Update(uint32 diff)
    {
        std::lock_guard<std::recursive_mutex> lock(events);
        if (!active) return;
        elapsed += diff; phaseMs += diff;
        if ((state == "login" || state == "prepare" || state == "arrive" || state == "enter") &&
            elapsed > EncounterSetupMs)
            Fail("setup deadline");
        if (state == "login")
        {
            bool all = true;
            for (std::size_t i = 0; i < actors.size(); ++i) all = all && Actor(i);
            if (all) Phase("prepare");
        }
        else if (state == "prepare")
        {
            for (std::size_t i = 0; i < actors.size(); ++i)
                if (!Actor(i)) { Fail("fixture disconnected"); return; }
            if (prepared < actors.size())
            {
                if (PrepareOne(prepared)) ++prepared;
                return;
            }
            if (Form()) Phase("arrive");
        }
        else if (state == "arrive")
        {
            if (Arrived() && Stage()) Phase("enter");
        }
        else if (state == "enter")
        {
            if (InRoom() && !Assemble() && state == "enter" && phaseMs > 60000)
                Fail("Valithria missing from the instance");
            else if (state == "enter" && phaseMs > 90000)
                Fail("the raid did not reach her room");
        }
        else if (state == "prepull")
        {
            for (std::size_t i = 0; i < actors.size(); ++i)
                if (!Actor(i)) { Fail("fixture disconnected"); return; }
            InstanceMap* map = Instance();
            if (!map || !map->GetInstanceScript()) { Fail("the measured instance unloaded"); return; }
            Creature* dragon = map->GetCreature(valithria);
            if (phaseMs >= EncounterPrePullMs || map->GetInstanceScript()->GetBossState(10) == IN_PROGRESS ||
                (dragon && dragon->GetHealthPct() > 50.5f))
                Pull();
        }
        else if (state == "observe") Observe(diff);
        else if (state == "cleanup")
        {
            sPlayerbotAIConfig.ValithriaHealerFocus = oldFocus;
            for (std::size_t i = 0; i < actors.size(); ++i)
                if (Player* p = Actor(i))
                {
                    p->CombatStop(true);
                    GET_PLAYERBOT_AI(p)->SetMaster(nullptr);
                    GET_PLAYERBOT_AI(p)->ChangeStrategy("+passive,+stay", BOT_STATE_NON_COMBAT);
                    if (Group* g = p->GetGroup()) g->Disband();
                    if (!p->IsAlive()) { p->ResurrectPlayer(1.f); p->SpawnCorpseBones(); }
                    p->TeleportTo(0, -8949.95f, -132.493f, 83.5312f, 0.f);
                }
            Phase("logout");
        }
        else if (state == "logout")
        {
            bool online = false;
            for (std::size_t i = 0; i < actors.size(); ++i)
                if (ObjectAccessor::FindConnectedPlayer(actors[i]))
                {
                    online = true;
                    Player* p = Actor(i);
                    if (p && !p->IsBeingTeleported() && p->GetMapId() == 0)
                        sRandomPlayerbotMgr.LogoutPlayerBot(actors[i]);
                    // A worldport still unacknowledged after 20 s (a fixture that was the self-bot leader, or
                    // a released ghost) logs out where it is, once; its next login starts it outside.
                    else if (phaseMs > 20000 && !forcedLogout[i])
                    {
                        forcedLogout[i] = true;
                        sRandomPlayerbotMgr.LogoutPlayerBot(actors[i]);
                    }
                }
            if (!online) { cleanup = true; active = false; Phase("done"); }
            else if (phaseMs > 60000) { failure += "; cleanup not proved"; active = false; Phase("cleanup_failed"); }
        }
    }

    json Status(bool full)
    {
        std::lock_guard<std::recursive_mutex> lock(events);
        json roster = json::array();
        for (std::size_t i = 0; i < actors.size(); ++i)
        {
            EncounterMember const& m = members[i];
            json row = {{"slot", i}, {"name", Roster[i].name}, {"spec", Roster[i].spec}, {"healer", Healer(i)},
                {"deaths", m.deaths}, {"alive_ms", m.aliveMs}, {"level", m.level}, {"item_level", m.itemLevel},
                {"max_health", m.maxHealth}, {"max_mana", m.maxMana}, {"is_heal", m.isHeal}};
            if (Healer(i))
            {
                row["dream_entries"] = m.dreamEntries;
                row["dream_ms"] = m.dreamMs;
                row["in_reach_ms"] = m.inReachMs;
                row["heals_on_her"] = m.heals;
                row["healing_on_her"] = m.healing;
                row["mana_cooldowns"] = m.manaCooldowns;
                row["mana_potions"] = m.potions;
                row["min_mana"] = m.minMana;
                row["low_mana_ms"] = m.lowManaMs;
                row["max_emerald_vigor"] = m.maxVigor;
                row["max_twisted_nightmares"] = m.maxTwisted;
            }
            if (Player* p = Actor(i))
            {
                PlayerbotAI* ai = GET_PLAYERBOT_AI(p);
                row["map"] = p->GetMapId();
                row["in_instance"] = instance && p->GetInstanceId() == instance;
                row["x"] = p->GetPositionX();
                row["y"] = p->GetPositionY();
                row["z"] = p->GetPositionZ();
                row["combat"] = p->IsInCombat();
                row["alive"] = p->IsAlive();
                row["health_pct"] = p->GetHealthPct();
                if (p->getPowerType() == POWER_MANA) row["mana_pct"] = p->GetPowerPct(POWER_MANA);
                Player* master = ai->GetMaster();
                row["master"] = master ? (master == p ? "self" : master->GetName()) : "-";
                if (InstanceMap* map = Instance())
                    if (Creature* dragon = map->GetCreature(valithria); dragon && p->GetMap() == map)
                        row["distance_to_her"] = p->GetDistance(dragon);
                if (Engine* engine = ai->GetCurrentEngine())
                    row["last_action"] = std::string(engine->GetLastAction()).substr(0, 160);
                row["icc"] = ai->HasStrategy("icc", BOT_STATE_COMBAT);
            }
            roster.push_back(row);
        }
        json out = {{"format", 1}, {"kind", "dojo-valithria-encounter"}, {"token", token},
            {"difficulty", difficulty}, {"mode", mode}, {"focus", mode == "after"}, {"state", state},
            {"setup", setup}, {"cleanup", cleanup}, {"failure", failure}, {"outcome", outcome},
            {"instance", instance}, {"map_difficulty", mapDifficulty}, {"engaged", engaged},
            {"early_pull", earlyPull}, {"pull_ms", pullMs},
            {"samples", samples}, {"start_hp", startHp}, {"hp", hp}, {"peak_hp", peakHp},
            {"damage_to_her", damageToHer}, {"item_level_cap", EncounterItemLevel}, {"members", roster}};
        if (InstanceMap* map = Instance())
            if (InstanceScript* script = map->GetInstanceScript())
            {
                out["boss_state"] = uint32(script->GetBossState(10));
                if (Creature* trigger = map->GetCreature(script->GetGuidData(35)))
                    out["trigger_combat"] = trigger->IsInCombat();
                if (Creature* dragon = map->GetCreature(valithria))
                    out["her_position"] = {dragon->GetPositionX(), dragon->GetPositionY(), dragon->GetPositionZ()};
            }
        if (full)
        {
            json line = json::array();
            for (auto const& [ms, pct] : timeline) line.push_back({ms, pct});
            out["timeline"] = line;
        }
        return out;
    }

    int Slot(Unit const* unit) const
    {
        if (!unit) return -1;
        for (std::size_t i = 0; i < actors.size(); ++i)
            if (unit->GetGUID() == actors[i]) return int(i);
        return -1;
    }
};
EncounterRun encounter;

class EncounterEvents : public UnitScript
{
public:
    EncounterEvents() : UnitScript("DojoEncounterEvents", true, {UNITHOOK_ON_HEAL, UNITHOOK_ON_DAMAGE}) { }
    void OnHeal(Unit* healer, Unit* receiver, uint32& gain) override
    {
        std::lock_guard<std::recursive_mutex> lock(encounter.events);
        if (encounter.state != "observe" || !receiver || receiver->GetGUID() != encounter.valithria || !gain) return;
        int const slot = encounter.Slot(healer);
        if (slot < 0) return;
        ++encounter.members[slot].heals;
        encounter.members[slot].healing += gain;
    }
    void OnDamage(Unit* /*attacker*/, Unit* victim, uint32& damage) override
    {
        std::lock_guard<std::recursive_mutex> lock(encounter.events);
        if (encounter.state == "observe" && victim && victim->GetGUID() == encounter.valithria)
            encounter.damageToHer += damage;
    }
};

class EncounterCasts : public PlayerScript
{
public:
    EncounterCasts() : PlayerScript("DojoEncounterCasts", {PLAYERHOOK_ON_SPELL_CAST}) { }
    void OnPlayerSpellCast(Player* player, Spell* spell, bool /*skipCheck*/) override
    {
        std::lock_guard<std::recursive_mutex> lock(encounter.events);
        if (encounter.state != "observe" || !spell) return;
        int const slot = encounter.Slot(player);
        if (slot < 0) return;
        SpellInfo const* info = spell->GetSpellInfo();
        if (IsManaCooldown(info->Id))
            ++encounter.members[slot].manaCooldowns;
        else if (spell->m_CastItem && info->Effects[0].Effect == SPELL_EFFECT_ENERGIZE &&
                 info->Effects[0].MiscValue == POWER_MANA)
            ++encounter.members[slot].potions;
    }
};

class EncounterTicks : public WorldScript
{
public:
    EncounterTicks() : WorldScript("DojoEncounterTicks", {WORLDHOOK_ON_UPDATE}) { }
    void OnUpdate(uint32 diff) override { encounter.Update(diff); }
};

class RaidCommands : public CommandScript
{
public:
    RaidCommands() : CommandScript("DojoRaidCommands") { }
    ChatCommandTable GetCommands() const override
    {
        static ChatCommandTable encounterCommands{{"start", EncounterStart, SEC_CONSOLE, Console::Yes},
            {"status", EncounterStatus, SEC_CONSOLE, Console::Yes},
            {"abort", EncounterAbort, SEC_CONSOLE, Console::Yes}};
        static ChatCommandTable commands{{"start", Start, SEC_CONSOLE, Console::Yes},
            {"status", Status, SEC_CONSOLE, Console::Yes}, {"abort", Abort, SEC_CONSOLE, Console::Yes},
            {"encounter", encounterCommands}};
        static ChatCommandTable dojo{{"raid", commands}};
        static ChatCommandTable root{{"dojo", dojo}};
        return root;
    }
    static bool Start(ChatHandler* handler, Tail args)
    {
        std::istringstream in{std::string(args)};
        std::string scenario, token, mode, extra;
        in >> scenario >> token >> mode >> extra;
        if (!extra.empty() || (mode != "normal" && mode != "negative"))
            handler->SendSysMessage("usage: dojo raid start valithria|twins|poison <token> normal|negative");
        else if (encounter.active) handler->SendSysMessage("dojo raid: refused: an encounter run is active");
        else handler->SendSysMessage("dojo raid: " + run.Start(scenario, token, mode == "negative"));
        return true;
    }
    static bool EncounterStart(ChatHandler* handler, Tail args)
    {
        std::istringstream in{std::string(args)};
        std::string token, difficulty, mode, extra;
        in >> token >> difficulty >> mode >> extra;
        if (!extra.empty() || mode.empty())
            handler->SendSysMessage("usage: dojo raid encounter start <token> 10n|10h before|after");
        else handler->SendSysMessage("dojo encounter: " + encounter.Start(token, difficulty, mode, run.active));
        return true;
    }
    static bool EncounterStatus(ChatHandler* handler, Tail args)
    {
        handler->SendSysMessage("dojo-encounter " + encounter.Status(std::string(args) == "full").dump());
        return true;
    }
    static bool EncounterAbort(ChatHandler* handler)
    {
        {
            std::lock_guard<std::recursive_mutex> lock(encounter.events);
            if (encounter.active && encounter.state != "cleanup" && encounter.state != "logout")
                encounter.Fail("aborted");
        }
        handler->SendSysMessage("dojo encounter: cleanup requested");
        return true;
    }
    static bool Status(ChatHandler* handler) { handler->SendSysMessage("dojo-raid " + run.Status().dump()); return true; }
    static bool Abort(ChatHandler* handler)
    {
        if (run.active) run.Fail("aborted");
        handler->SendSysMessage("dojo raid: cleanup requested"); return true;
    }
};

class RaidTicks : public WorldScript
{
public:
    RaidTicks() : WorldScript("DojoRaidTicks", {WORLDHOOK_ON_UPDATE}) { }
    void OnUpdate(uint32 diff) override { run.Update(diff); }
};

class RaidEvents : public UnitScript
{
public:
    RaidEvents() : UnitScript("DojoRaidEvents", true, {UNITHOOK_ON_HEAL, UNITHOOK_ON_DAMAGE}) { }
    void OnHeal(Unit* healer, Unit* receiver, uint32& gain) override
    {
        std::lock_guard<std::recursive_mutex> lock(run.events);
        if (run.state == "observe" && healer && receiver && healer->GetGUID() == run.actors[0] &&
            receiver->GetGUID() == run.target && gain > 0)
        { ++run.heals; run.effectiveHealing += gain; }
    }
    void OnDamage(Unit* attacker, Unit* victim, uint32& damage) override
    {
        std::lock_guard<std::recursive_mutex> lock(run.events);
        if (run.state == "observe" && run.scenario == "poison" && attacker && victim &&
            attacker->GetEntry() == 16027 && victim->GetGUID() == run.actors[0] && damage > 0) ++run.damageHits;
    }
};
}

void AddSC_dojo_raid()
{
    new RaidCommands();
    new RaidTicks();
    new RaidEvents();
    new EncounterEvents();
    new EncounterCasts();
    new EncounterTicks();
}
#else
void AddSC_dojo_raid() { }
#endif
#else
void AddSC_dojo_raid() { }
#endif
