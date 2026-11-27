// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.
#include "DojoRaid.h"
#include "DojoOffline.h"
#include "DojoHuman.h"
#include "DojoLead.h"
#include "DojoVehicle.h"
#include "DojoYogg.h"

#if DOJO_TRAINING_BUILD
#if defined(DOJO_RAID_ACCEPTANCE) && DOJO_RAID_ACCEPTANCE
#include "DojoRuntime.h"
#include "AccountMgr.h"
#include "AllSpellScript.h"
#include "DatabaseEnv.h"
#include "RBAC.h"
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
#include "PathGenerator.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "PlayerbotsDatabase.h"
#include "PlayerbotAIConfig.h"
#include "Playerbots.h"
#include "RandomPlayerbotMgr.h"
#include "UnitScript.h"
#include "World.h"
#include "WorldScript.h"
#include "WorldPacket.h"
#include "WorldSession.h"
#include "Guild/HazardJob.h"
#include "LeeroyIntegrations.h"
#include "DBCStores.h"
#include "InstanceSaveMgr.h"
#include "MapMgr.h"
#include "PlayerbotFactory.h"
#include "PlayerScript.h"
#include "Spell.h"
#include "SpellAuraEffects.h"
#include "SpellInfo.h"
#include <algorithm>
#include <array>
#include <cmath>
#include <list>
#include <map>
#include <mutex>
#include <set>
#include <sstream>
#include <string>
#include <vector>

using nlohmann::json;
using namespace Acore::ChatCommands;

namespace Dojo
{
namespace
{
bool FixtureAccount(ObjectGuid guid)
{
    uint32 const account = sCharacterCache->GetCharacterAccountIdByGuid(guid);
    if (account && account == AccountMgr::GetId("DOJORAID")) return true;
#if defined(DOJO_OFFLINE_ACCEPTANCE) && DOJO_OFFLINE_ACCEPTANCE
    if (account && account == AccountMgr::GetId("RNDBOTDOJO")) return true;
#endif
    return false;
}
}
bool LoginRaidFixture(ObjectGuid guid)
{
    if (!FixtureAccount(guid)) return false;
#if defined(DOJO_OFFLINE_ACCEPTANCE) && DOJO_OFFLINE_ACCEPTANCE
    uint32 const account = sCharacterCache->GetCharacterAccountIdByGuid(guid);
    if (account == AccountMgr::GetId("RNDBOTDOJO") && !sPlayerbotAIConfig.IsInRandomAccountList(account))
    {
        // MaxRandomBots=0 deliberately creates no population and clears the
        // factory's type-1 assignments at startup. Register only our fixed
        // synthetic account after startup, through the ordinary account-type
        // loader and pin composer. Never exempt actors from ownership checks.
        if (State().plan.state != RuntimeState::Armed || State().manifest ||
            sPlayerbotAIConfig.RandomBotAutologin || sPlayerbotAIConfig.MinRandomBots ||
            sPlayerbotAIConfig.MaxRandomBots) return false;
        PlayerbotsDatabase.DirectExecute("INSERT INTO playerbots_account_type (account_id, account_type) "
            "VALUES ({}, 1) ON DUPLICATE KEY UPDATE account_type=1", account);
        sRandomPlayerbotMgr.AssignAccountTypes();
        if (!sRandomPlayerbotMgr.IsAccountType(account, 1)) return false;
        sPlayerbotAIConfig.RandomBotAccounts.push_back(account);
        LeeroyApplyPins();
    }
#endif
    sRandomPlayerbotMgr.AddPlayerBot(guid, 0);
    return true;
}
bool LogoutRaidFixture(ObjectGuid guid)
{
    if (!FixtureAccount(guid)) return false;
    sRandomPlayerbotMgr.LogoutPlayerBot(guid);
    return true;
}
}

namespace
{
// Fixed synthetic account and characters; no command accepts a live character GUID.
constexpr char const* Account = "DOJORAID";
constexpr std::array<char const*, 2> Names{"Dojopaladin", "Dojoreserve"};
constexpr std::array<uint8, 2> Classes{CLASS_PALADIN, CLASS_DRUID};
constexpr std::array<uint8, 2> Races{RACE_HUMAN, RACE_NIGHTELF};
constexpr uint32 Timeout = 120000;
int8 PremadeSpec(uint8 cls, char const* name);

struct RaidRun
{
    std::string scenario, token, state = "idle", failure;
    bool control = false;
    bool oldTwins = false, oldLichKing = false;
    uint32 elapsed = 0, phaseMs = 0, map = 0, instance = 0, samples = 0, maxGap = 0;
    uint32 crossings = 0, hazardsSeen = 0, lightSamples = 0, darkSamples = 0, switchMs = 0;
    uint32 heals = 0, damageHits = 0;
    uint32 roomSamples = 0, escapeMs = 0, outsideSamples = 0, hazardStartMs = 0;
    uint32 insideSamples = 0, lateHits = 0, encounterSamples = 0;
    uint32 harvestSamples = 0, strangulateSamples = 0;
    uint32 anchorCombatSamples = 0, anchorArmor = 0;
    float anchorItemLevel = 0.f;
    float finalHazardClearance = 0.f;
    uint64 effectiveHealing = 0;
    uint64 ownerEpoch = 0;
    float minimumClearance = 1000.f;
    bool setup = false, active = false, cleanup = false, combatObserved = false, competition = false;
    ObjectGuid target;
    json defilePaths = json::array();
    json damageEvidence = json::array(), deaths = json::array();
    json fixtureFailure = json::object();
    uint64 damageSequence = 0;
    std::array<ObjectGuid, 2> actors{};
    std::recursive_mutex events;

    Player* Actor(std::size_t i) const
    {
        Player* p = ObjectAccessor::FindConnectedPlayer(actors[i]);
        return p && p->IsInWorld() && !p->IsDuringRemoveFromWorld() && GET_PLAYERBOT_AI(p) ? p : nullptr;
    }

    json LichKingState(Player* p) const
    {
        json result = json::object();
        if ((scenario != "lk-terenas" && scenario != "lk-defile") || !p || !p->IsInWorld() ||
            p->IsDuringRemoveFromWorld() || p->GetMapId() != map || p->GetInstanceId() != instance)
            return result;
        InstanceScript* script = p->GetInstanceScript();
        if (!script) return result;
        result["boss_state"] = uint32(script->GetBossState(12));
        for (uint32 data : {12u, 39u}) // native Lich King and Terenas
        {
            Creature* c = p->GetMap()->GetCreature(script->GetGuidData(data));
            json npc = {{"exists", c != nullptr}};
            if (c)
            {
                Unit* victim = c->GetVictim();
                uint32 spell = 0;
                if (Spell* current = c->GetCurrentSpell(CURRENT_GENERIC_SPELL)) spell = current->GetSpellInfo()->Id;
                else if (Spell* channel = c->GetCurrentSpell(CURRENT_CHANNELED_SPELL)) spell = channel->GetSpellInfo()->Id;
                npc.update({{"entry", c->GetEntry()}, {"alive", c->IsAlive()}, {"combat", c->IsInCombat()},
                    {"engaged", c->IsEngaged()}, {"evading", c->IsInEvadeMode()},
                    {"immune_to_pc", c->IsImmuneToPC()}, {"react_state", uint32(c->GetReactState())},
                    {"health", c->GetHealth()}, {"max_health", c->GetMaxHealth()}, {"spell", spell},
                    {"x", c->GetPositionX()}, {"y", c->GetPositionY()}, {"z", c->GetPositionZ()},
                    {"victim_slot", victim ? std::size_t(std::find(actors.begin(), actors.end(), victim->GetGUID()) - actors.begin()) : actors.size()}});
            }
            result[std::to_string(data)] = std::move(npc);
        }
        return result;
    }

    void Phase(std::string const& next) { state = next; phaseMs = 0; }
    void Fail(std::string const& why)
    {
        // Actor() deliberately hides actors leaving the world. Preserve raw
        // connectivity and life state before cleanup, which resurrects and logs
        // them out before the next status poll can distinguish these failures.
        if (fixtureFailure.empty() && (scenario == "lk-terenas" || scenario == "lk-defile") &&
            (state == "arrive" || state == "harvest" || state == "observe"))
        {
            json fixtures = json::array();
            for (std::size_t i = 0; i < actors.size(); ++i)
            {
                Player* p = ObjectAccessor::FindConnectedPlayer(actors[i]);
                json actor = {{"slot", i}, {"connected", p != nullptr}};
                if (p)
                {
                    actor.update({{"alive", p->IsAlive()}, {"in_world", p->IsInWorld()},
                        {"removing", p->IsDuringRemoveFromWorld()}, {"ai_present", GET_PLAYERBOT_AI(p) != nullptr},
                        {"teleport_near", p->IsBeingTeleportedNear()}, {"teleport_far", p->IsBeingTeleportedFar()},
                        {"health", p->GetHealth()}, {"max_health", p->GetMaxHealth()},
                        {"armor", p->GetArmor()},
                        {"map", p->GetMapId()}, {"instance", p->GetInstanceId()},
                        {"x", p->GetPositionX()}, {"y", p->GetPositionY()}, {"z", p->GetPositionZ()},
                        {"combat", p->IsInCombat()}});
                    json auras = json::array();
                    for (auto const& aura : p->GetAppliedAuras())
                    {
                        if (auras.size() == 64) break;
                        auras.push_back(aura.second->GetBase()->GetId());
                    }
                    actor["auras"] = std::move(auras);
                    actor["aura_count"] = p->GetAppliedAuras().size();
                    actor["native_encounter"] = LichKingState(p);
                }
                fixtures.push_back(std::move(actor));
            }
            fixtureFailure = {{"elapsed_ms", elapsed}, {"phase_ms", phaseMs}, {"state", state},
                {"damage_sequence", damageSequence}, {"fixtures", std::move(fixtures)}};
        }
        failure = why;
        Phase("cleanup");
    }

    std::size_t DiagnosticSlot(Unit* unit) const
    {
        if (!active || (state != "harvest" && state != "observe") ||
            (scenario != "lk-terenas" && scenario != "lk-defile") || !unit ||
            unit->GetMapId() != map || unit->GetInstanceId() != instance) return actors.size();
        auto const found = std::find(actors.begin(), actors.end(), unit->GetGUID());
        return std::size_t(found - actors.begin());
    }

    void RecordDamage(Unit* victim, Unit* attacker, uint32 damage, uint32 spell, uint32 stage)
    {
        std::size_t const slot = DiagnosticSlot(victim);
        if (slot == actors.size() || !damage) return;
        // Calculations and OnDamage precede final application. Keep their stage
        // separate from the actual death callback; proximity is not lethal-hit proof.
        if (damageEvidence.size() == 32) damageEvidence.erase(damageEvidence.begin());
        damageEvidence.push_back({{"sequence", ++damageSequence}, {"elapsed_ms", elapsed},
            {"slot", slot}, {"stage", stage}, {"spell", spell}, {"damage", damage},
            {"health", victim->GetHealth()}, {"max_health", victim->GetMaxHealth()},
            {"armor", victim->GetArmor()}, {"attacker_enraged", attacker && attacker->HasAura(72143)},
            {"attacker_entry", attacker ? attacker->GetEntry() : 0}});
    }

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
        if (name != "valithria" && name != "twins" && name != "poison" &&
            name != "lk-terenas" && name != "lk-defile") return "refused: unknown scenario";
        if (id.empty() || id.size() > 64 || id.find_first_not_of("abcdefghijklmnopqrstuvwxyz0123456789-") != std::string::npos)
            return "refused: invalid run token";
        std::string error = Create();
        if (!error.empty()) return "refused: " + error;
        std::lock_guard<std::recursive_mutex> lock(events);
        scenario = name; token = id; control = negative; failure.clear();
        elapsed = phaseMs = instance = samples = maxGap = crossings = hazardsSeen = lightSamples = darkSamples = switchMs = 0;
        heals = damageHits = 0; effectiveHealing = ownerEpoch = 0; minimumClearance = 1000.f;
        roomSamples = escapeMs = outsideSamples = hazardStartMs = 0; finalHazardClearance = 0.f;
        insideSamples = lateHits = encounterSamples = 0;
        harvestSamples = strangulateSamples = 0;
        anchorCombatSamples = anchorArmor = 0; anchorItemLevel = 0.f;
        setup = cleanup = combatObserved = competition = false; target.Clear();
        defilePaths = json::array();
        damageEvidence = json::array(); deaths = json::array(); damageSequence = 0;
        fixtureFailure = json::object();
        map = name == "valithria" || name == "lk-terenas" || name == "lk-defile" ? 631 : name == "twins" ? 649 : 533;
        oldTwins = sPlayerbotAIConfig.TwinValkyrInteractions;
        sPlayerbotAIConfig.TwinValkyrInteractions = !negative;
        oldLichKing = sPlayerbotAIConfig.LichKingMechanics;
        if (name == "lk-terenas" || name == "lk-defile") sPlayerbotAIConfig.LichKingMechanics = !negative;
        active = true;
        Phase("login");
        for (ObjectGuid guid : actors) Dojo::LoginRaidFixture(guid);
        return "started";
    }

    Position StartPoint() const
    {
        if (scenario == "valithria") return Position(4203.5f, 2484.0f, 364.96f, 0.f);
        if (scenario == "twins") return Position(563.5f, 139.5f, 393.84f, 0.f);
        if (scenario == "lk-terenas") return Position(495.708f, -2523.76f, 1049.95f, 0.f);
        if (scenario == "lk-defile") return Position(503.62f, -2124.65f, 840.857f, 0.f);
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
        if (scenario == "lk-terenas")
        {
            // The outside reference holds the native encounter while the tested
            // healer is in Frostmourne. An unarmoured, frozen restoration druid
            // was killed by a real 159644-damage Horror hit despite the existing
            // 100000-HP reserve. Give this support actor the stock raid tank
            // spec/equipment; the tested healer and all damage checks stay intact.
            int8 const spec = PremadeSpec(CLASS_DRUID, "bear pve");
            if (spec < 0) { Fail("lk_anchor_tank_spec_missing"); return false; }
            PlayerbotFactory::InitTalentsBySpecNo(owner, spec, true);
            GET_PLAYERBOT_AI(owner)->ResetStrategies(false);
            PlayerbotFactory factory(owner, 80, ITEM_QUALITY_EPIC,
                PlayerbotFactory::CalcMixedGearScore(245, ITEM_QUALITY_EPIC));
            factory.InitSkills();
            factory.InitAvailableSpells();
            // Keep one real equipment set across the disabled/enabled pair.
            if (std::abs(owner->GetAverageItemLevel() - 245.f) > 8.f) factory.InitEquipment(false);
        }
        else
        {
            owner->resetTalents(true);
            owner->LearnTalent(823, 2); // Nature's Focus, 3/3 (Talent.dbc, restoration)
            if (!owner->HasTalent(17066, owner->GetActiveSpec())) { Fail("restoration fixture talent refused"); return false; }
        }
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
        {
            // The native intro's lower platform point is within Harvest Soul's
            // 60-yard range of the seated boss; the platform center is not.
            Position const destination = scenario == "lk-terenas"
                ? Position(465.073f, -2123.470f, 840.8569f, 0.f) : point;
            if (!Actor(i)->TeleportTo(map, destination.GetPositionX(), destination.GetPositionY() + float(i) * 2.f,
                    destination.GetPositionZ(), 0.f)) { Fail("fixture teleport refused"); return false; }
        }
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
        if (scenario == "lk-terenas")
        {
            // Apply the native form after teleport/strategy reconstruction and
            // under the existing external hold, so the support actor stays a tank.
            owner->CastSpell(owner, 9634, true); // Dire Bear Form
            anchorArmor = owner->GetArmor();
            anchorItemLevel = owner->GetAverageItemLevel();
            if (!owner->HasAura(9634) || anchorArmor < 20000 || anchorItemLevel < 230.f)
            { Fail("lk_anchor_tank_preparation_failed"); return; }
            owner->SetMaxHealth(100000);
            owner->SetFullHealth();
        }
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
        else if (scenario == "lk-terenas" || scenario == "lk-defile")
        {
            bot->GetMap()->LoadGrid(503.62f, -2124.65f);
            Creature* lichKing = bot->GetMap()->GetCreature(script->GetGuidData(12));
            if (!lichKing) { Fail("native Lich King missing"); return; }
            // Exercise the shipped ICC strategy with its ordinary follow/heal
            // competitors intact. Only the feature switch differs in the control.
            for (uint32 boss = 0; boss < 12; ++boss) script->SetBossState(boss, DONE);
            // The core also requires the separately indexed Sindragosa
            // gauntlet, not only her boss state. Otherwise JustEngagedWith
            // evades and teleports the outside anchor to Light's Hammer.
            script->SetBossState(13, DONE);
            if (!script->CheckRequiredBosses(12, owner) || !lichKing->IsVisible())
            { Fail("native_lich_king_prerequisites_missing"); return; }
            script->SetBossState(12, IN_PROGRESS);
            ai->ChangeStrategy("+icc", BOT_STATE_COMBAT);
            ai->ChangeStrategy("+icc", BOT_STATE_NON_COMBAT);
            if (scenario == "lk-terenas")
            {
                if (!PlayerbotAI::IsHeal(bot)) { Fail("Frostmourne healer role missing"); return; }
                // Hold a real active encounter outside while the healer is inside.
                // The stock control may leave the room; that must not reset the
                // encounter and invalidate the comparison before it is measured.
                // Use the native intro's final platform position, rather than
                // asking a seated boss on the throne to chase down its steps.
                lichKing->NearTeleportTo(465.073f, -2123.470f, 840.8569f, 0.f);
                lichKing->SetStandState(UNIT_STAND_STATE_STAND);
                lichKing->SetImmuneToPC(false);
                lichKing->SetReactState(REACT_AGGRESSIVE); // native post-intro state
                lichKing->EngageWithTarget(owner);
                lichKing->AI()->AttackStart(owner);
                if (!lichKing->IsEngaged() || !owner->IsInCombat())
                { Fail("native_lk_anchor_not_engaged"); return; }
                bot->GetMap()->LoadGrid(495.708f, -2523.76f);
                // Use the actual hostile Harvest Soul, including its damage,
                // native strangulate vehicle and delayed teleport. Calling only
                // the last teleport leaves the control out of combat and lets
                // stock follow undo the entry before it can be observed.
                auto const cast = lichKing->CastSpell(bot, 68980, true);
                if (cast != SPELL_CAST_OK) { Fail("native_frostmourne_entry_refused:" + std::to_string(uint32(cast))); return; }
                Phase("harvest");
                return;
            }
            else
            {
                // Tirion's native intro removes this flag before combat. The
                // core also prevents an immune-to-PC caster attacking players,
                // so a focused post-intro Defile probe must establish it too.
                lichKing->SetImmuneToPC(false);
                // Cast the real core spell: its summon/aura/growth are the stimulus.
                auto const cast = lichKing->CastSpell(bot, 72762, true);
                if (cast != SPELL_CAST_OK) { Fail("native_defile_cast_refused:" + std::to_string(uint32(cast))); return; }
                // Read-only path evidence: identify which core geometry check
                // rejects escape without moving the measured actor for it.
                for (auto const& offset : std::array<std::array<float, 2>, 4>{{{16.f, 0.f}, {-16.f, 0.f}, {0.f, 16.f}, {0.f, -16.f}}})
                {
                    float const x = bot->GetPositionX() + offset[0], y = bot->GetPositionY() + offset[1];
                    float z = bot->GetPositionZ();
                    bot->UpdateAllowedPositionZ(x, y, z);
                    PathGenerator path(bot);
                    path.SetPathLengthLimit(96.f);
                    bool const built = path.CalculatePath(x, y, z);
                    json vertices = json::array();
                    for (auto const& point : path.GetPath()) vertices.push_back({point.x, point.y, point.z});
                    defilePaths.push_back({{"destination", {x, y, z}}, {"los", bot->IsWithinLOS(x, y, z)},
                        {"built", built}, {"type", uint32(path.GetPathType())}, {"vertices", vertices}});
                }
            }
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
        if (scenario == "lk-terenas")
        {
            Creature* lichKing = bot->GetMap()->GetCreature(bot->GetInstanceScript()->GetGuidData(12));
            if (!lichKing || !lichKing->IsEngaged() || !owner->IsInCombat())
            { Fail("native_lk_anchor_engagement_lost"); return; }
            ++anchorCombatSamples;
        }
        ++samples;
        maxGap = std::max(maxGap, diff);
        combatObserved = combatObserved || bot->IsInCombat();
        if ((scenario == "lk-terenas" || scenario == "lk-defile") &&
            bot->GetInstanceScript()->GetBossState(12) == IN_PROGRESS) ++encounterSamples;
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
        else if (scenario == "lk-terenas")
        {
            if (bot->GetExactDist2d(495.708f, -2523.76f) < 45.f &&
                std::abs(bot->GetPositionZ() - 1049.95f) < 35.f) ++roomSamples;
            competition = competition || (GET_PLAYERBOT_AI(bot)->HasStrategy("follow", BOT_STATE_NON_COMBAT) &&
                bot->GetDistance(owner) > 100.f);
            if (phaseMs >= 25000) Phase("cleanup");
        }
        else if (scenario == "lk-defile")
        {
            if (Creature* defile = bot->FindNearestCreature(38757, 100.f))
            {
                ++hazardsSeen;
                if (!hazardStartMs) hazardStartMs = phaseMs;
                finalHazardClearance = bot->GetExactDist2d(defile) - (10.f * defile->GetObjectScale() + 2.f);
                if (finalHazardClearance > 0.f)
                {
                    ++outsideSamples;
                    if (!escapeMs) escapeMs = phaseMs - hazardStartMs;
                }
                else { ++insideSamples; outsideSamples = 0; }
            }
            if (phaseMs >= 20000) Phase("cleanup");
        }
        else
        {
            // The registered mod-leeroy world hook owns the production cadence.
            // A second tick here would scan twice as often as the live realm.
            // The filter serves only bots whose master is the owner, a game client, whose AI takes a turn every
            // ReactDelay (0.1 s); under a bot master playerbots waits 1-3 s out of combat (GetReactDelay). Between
            // teleports the held leader is a self-bot (its own master), which playerbots counts as a game client
            // master; a self-bot's teleport waits for a client's ack, so it is an ordinary bot while it moves.
            // Both cases need the same client-master reaction cadence. Leaving
            // the control under a bot master adds a random 1-3 second AI delay
            // and changes which poison wave it crosses independently of avoidance.
            if (GET_PLAYERBOT_AI(owner)->GetMaster() != owner)
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
            // Let the native throne grid and its boss finish loading before
            // selecting the encounter phase; a deferred Reset clears it.
            if ((scenario == "lk-terenas" || scenario == "lk-defile") && phaseMs < 2000) return;
            Player* bot = Actor(0); Player* owner = Actor(1);
            if (bot && owner && !bot->IsBeingTeleported() && !owner->IsBeingTeleported() &&
                bot->GetMapId() == map && owner->GetMap() == bot->GetMap()) Pull();
        }
        else if (state == "harvest")
        {
            Player* bot = Actor(0); Player* owner = Actor(1);
            if (!bot || !owner || !bot->IsAlive() || !owner->IsAlive())
            { Fail("harvest_fixture_lost"); return; }
            // Same probe reserves as observation; the native spell and vehicle
            // alone control the entry. No movement or combat flag is supplied.
            for (Player* p : {bot, owner})
            {
                p->SetMaxHealth(100000);
                p->SetFullHealth();
                p->SetMaxPower(POWER_MANA, 30000);
                p->SetPower(POWER_MANA, 30000);
            }
            if (bot->HasAura(68980)) ++harvestSamples;
            if (auto* base = bot->GetVehicleBase(); base && base->GetEntry() == 36598) ++strangulateSamples;
            if (bot->IsBeingTeleported() || bot->GetMapId() != map || bot->GetInstanceId() != instance ||
                bot->GetExactDist2d(495.708f, -2523.76f) >= 45.f ||
                std::abs(bot->GetPositionZ() - 1049.95f) >= 35.f) return;
            // The native teleport sends event 23617 and creates these actors.
            auto* script = bot->GetInstanceScript();
            auto* terenas = script ? bot->GetMap()->GetCreature(script->GetGuidData(39)) : nullptr;
            if (!terenas || terenas->GetEntry() != 36823 || !terenas->IsAlive() ||
                !harvestSamples || !strangulateSamples)
            { Fail("native_frostmourne_sequence_missing"); return; }
            target = terenas->GetGUID();
            setup = true;
            Phase("observe");
        }
        else if (state == "observe") Observe(diff);
        else if (state == "cleanup")
        {
            Leeroy::Guild::DojoHazardOwner(0);
            Leeroy::Guild::TickHazards(500);
            sPlayerbotAIConfig.TwinValkyrInteractions = oldTwins;
            sPlayerbotAIConfig.LichKingMechanics = oldLichKing;
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
                        Dojo::LogoutRaidFixture(actors[i]);
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
                json auras = json::array();
                for (auto const& aura : p->GetAppliedAuras())
                {
                    if (auras.size() == 64) break;
                    auras.push_back(aura.second->GetBase()->GetId());
                }
                fixtures.push_back({{"slot", i}, {"map", p->GetMapId()}, {"instance", p->GetInstanceId()},
                    {"x", p->GetPositionX()}, {"y", p->GetPositionY()}, {"z", p->GetPositionZ()},
                    {"health", p->GetHealth()}, {"max_health", p->GetMaxHealth()}, {"mana", p->GetPower(POWER_MANA)},
                    {"armor", p->GetArmor()},
                    {"teleporting", p->IsBeingTeleported()}, {"healer", PlayerbotAI::IsHeal(p)},
                    {"icc", ai->HasStrategy("icc", BOT_STATE_COMBAT)},
                    {"combat", p->IsInCombat()}, {"self_bot", IsSelfBot(p)},
                    {"auras", auras}, {"aura_count", p->GetAppliedAuras().size()},
                    {"controlled", ai->IsExternallyControlled()},
                    {"combat_strategies", ai->GetStrategies(BOT_STATE_COMBAT)},
                    {"idle_strategies", ai->GetStrategies(BOT_STATE_NON_COMBAT)},
                    {"last_action", ai->GetCurrentEngine() ? ai->GetCurrentEngine()->GetLastAction() : ""}});
            }
        json encounter = json::object();
        if (Player* p = Actor(0); p && p->GetInstanceScript())
        {
            InstanceScript* script = p->GetInstanceScript();
            encounter["boss_state"] = uint32(script->GetBossState(scenario == "valithria" ? 10 : scenario == "twins" ? 3 :
                scenario == "lk-terenas" || scenario == "lk-defile" ? 12 : 0));
            if (scenario == "lk-terenas" || scenario == "lk-defile")
                encounter["native_encounter"] = LichKingState(p);
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
            {"room_samples", roomSamples}, {"escape_ms", escapeMs}, {"outside_samples", outsideSamples},
            {"harvest_samples", harvestSamples}, {"strangulate_samples", strangulateSamples},
            {"anchor_combat_samples", anchorCombatSamples}, {"anchor_armor", anchorArmor}, {"anchor_item_level", anchorItemLevel},
            {"inside_samples", insideSamples}, {"late_hits", lateHits}, {"encounter_samples", encounterSamples},
            {"final_hazard_clearance", finalHazardClearance},
            {"switch_ms", switchMs}, {"crossings", crossings}, {"hazard_samples", hazardsSeen},
            {"minimum_clearance", minimumClearance}, {"poison_hits", scenario == "poison" ? damageHits : 0},
            {"defile_hits", scenario == "lk-defile" ? damageHits : 0}, {"defile_paths", defilePaths},
            {"damage_sequence", damageSequence}, {"damage_evidence", damageEvidence}, {"deaths", deaths},
            {"fixture_failure", fixtureFailure},
            {"fixtures", fixtures}, {"encounter", encounter}};
    }
};
RaidRun run;

// A full Valithria encounter for one fixed ten-player bot raid: three healers (Discipline priest, Holy
// paladin, Restoration shaman: the 10-man healer template of the owner's guild fill), two tanks and five
// damage dealers, epic gear capped at item level 245 by playerbots' own factory. The mage leads as a
// self-bot, the owner's stand-in, so the others have a game-client master as in his raids. Nothing here
// heals, damages, moves or casts for a fixture during the pull; it records her health, deaths, portal
// trips, healing on her and mana spent. `before` pulls with Playerbots.ValithriaHealerFocus off, `after`
// with it on, `noportals` with it on and Playerbots.ValithriaPortals off; both are restored at cleanup.
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
constexpr uint32 EncounterItemLevel = 245;  // the guild's raiders average 238 (tg_member, 2026-11)
constexpr uint32 EncounterSetupMs = 240000;
constexpr uint32 EncounterPrePullMs = 15000;
constexpr uint32 EncounterCapMs = 15 * 60 * 1000;
constexpr uint32 DreamState = 70766;
constexpr uint32 EmeraldVigor = 70873;
constexpr uint32 TwistedNightmares = 71941;
constexpr std::size_t TankSlots[] = {3, 4};
// The owner's own auras on his fire mage (`owner_buffs`): 200% haste, +2000 and +1000 spell power, +50%
// spell crit. They approximate his raid; they never replace the plain runs.
constexpr std::array<uint32, 4> OwnerAuras{6560, 51040, 31750, 23434};

// Her adds by base entry (a heroic add keeps its base entry; its stats come from the difficulty template).
struct AddKind
{
    uint32 entry;
    char const* name;
};
constexpr std::array<AddKind, 6> AddKinds{{{37886, "abomination"}, {37934, "zombie"}, {37907, "worm"},
    {37868, "archmage"}, {36791, "skeleton"}, {37863, "suppresser"}}};

int AddKindOf(uint32 entry)
{
    for (std::size_t i = 0; i < AddKinds.size(); ++i)
        if (AddKinds[i].entry == entry) return int(i);
    return -1;
}

std::string KindName(uint32 entry)
{
    int const kind = AddKindOf(entry);
    if (kind >= 0) return AddKinds[kind].name;
    return entry ? "c" + std::to_string(entry) : "other";
}

// Where an add's victim was, sampled every 250 ms.
enum AddVictim : std::size_t { OnTank, OnHealer, OnDps, OnHer, OnNobody, AddVictims };
constexpr std::array<char const*, AddVictims> AddVictimNames{"tank", "healer", "dps", "her", "none"};

struct EncounterAdd
{
    uint32 entry = 0, seenMs = 0, lastMs = 0, tankedMs = 0;
    bool alive = true;
    std::array<uint32, AddVictims> onMs{};
    std::array<uint32, 10> onSlotMs{};
    uint64 dealt = 0, taken = 0;
};

// One hit on a fixture: final damage after mitigation and absorbs, its source and her health before it.
struct EncounterHit
{
    uint32 ms = 0, entry = 0, spell = 0, amount = 0;
    float hpBefore = 0.f;
};

struct EncounterDeath
{
    uint32 ms = 0, slot = 0, killer = 0, load = 0;
    std::vector<EncounterHit> recap;
};

struct AddCasts
{
    uint32 prepared = 0, cast = 0, cancelled = 0;
};

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
    // What hurt this fixture (final damage by source entry and by spell, 0 = melee), who healed it (by
    // fixture slot), how many adds were on it, what it cast and its damage on each kind of add.
    std::map<uint32, uint64> takenBy, takenBySpell;
    std::array<uint64, 10> healedBy{};
    uint32 load = 0, maxLoad = 0, multiMs = 0;
    std::map<uint32, uint32> casts;
    std::map<std::string, uint32> castsOnAdds;
    std::array<uint64, AddKinds.size()> doneToAdds{};
    std::vector<EncounterHit> recent;
};

struct EncounterRun
{
    std::string token, difficulty, mode, state = "idle", failure, outcome;
    bool active = false, setup = false, cleanup = false, engaged = false, earlyPull = false, oldFocus = true,
        oldPortals = true, oldAdds = true, oldTurns = true, ownerBuffs = false;
    uint32 elapsed = 0, phaseMs = 0, pullMs = 0, sampleMs = 0, instance = 0, mapDifficulty = 0, samples = 0;
    uint32 itemLevel = EncounterItemLevel;
    std::size_t prepared = 0;
    float startHp = 0.f, hp = 0.f, peakHp = 0.f;
    uint64 damageToHer = 0;
    uint32 leaderTeleportAcks = 0;
    // Healer time with two or more of them in the dream, and with none of them out of it in reach of her.
    uint32 twoInDreamMs = 0, noneOnHerMs = 0;
    ObjectGuid valithria;
    std::array<ObjectGuid, 10> actors{};
    std::array<EncounterMember, 10> members{};
    std::array<bool, 10> forcedLogout{};
    // What hurt the raid: damage taken by spell (0 = melee or untagged) and each death's killer entry.
    std::map<uint32, uint64> damageTaken;
    std::vector<std::pair<uint32, uint32>> killers;  // (pull ms, killer creature entry; 0 = not a creature)
    std::vector<std::pair<uint32, float>> timeline;
    // Her adds as they came and went, deaths with the hits before them, the adds' own casts, the raid's
    // final damage taken by spell and a 10 s timeline of living adds by kind.
    std::map<ObjectGuid, EncounterAdd> adds;
    std::vector<EncounterDeath> deaths;
    std::map<uint32, AddCasts> addCasts;
    std::map<uint32, uint64> finalBySpell;
    std::map<ObjectGuid, uint32> pendingSpell;
    std::vector<json> addTimeline;
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
        // In place of clearing the lower spire: the core's own permission to skip an instance's required
        // bosses, for this synthetic account in the isolated realm only. Without it an archmage that sees
        // a raider before the pull starts the fight as a sequence break and ports the raid out.
        LoginDatabase.DirectExecute(Acore::StringFormat("REPLACE INTO rbac_account_permissions (accountId, "
            "permissionId, granted, realmId) VALUES ({}, {}, 1, -1)", account,
            uint32(rbac::RBAC_PERM_SKIP_CHECK_INSTANCE_REQUIRED_BOSSES)));
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

    std::string Start(std::string const& id, std::string const& diff, std::string const& how, uint32 gear,
                      bool buffs, bool checkRunning)
    {
        if (gear < 200 || gear > 284) return "refused: item level is 200-284";
        if (active || state == "cleanup_failed") return "refused: an encounter run or unproved cleanup is active";
        if (checkRunning) return "refused: a raid check is running";
        if (Dojo::State().plan.state != Dojo::RuntimeState::Armed || Dojo::State().manifest)
            return "refused: use an armed isolated training realm with no arena manifest";
        if (diff != "10n" && diff != "10h") return "refused: difficulty is 10n or 10h";
        if (how != "before" && how != "after" && how != "noportals" && how != "adds" && how != "turns")
            return "refused: mode is before, after, noportals, adds or turns";
        if (id.empty() || id.size() > 64 ||
            id.find_first_not_of("abcdefghijklmnopqrstuvwxyz0123456789-") != std::string::npos)
            return "refused: invalid run token";
        std::string error = Create();
        if (!error.empty()) return "refused: " + error;
        std::lock_guard<std::recursive_mutex> lock(events);
        token = id; difficulty = diff; mode = how; itemLevel = gear; ownerBuffs = buffs; failure.clear();
        outcome.clear();
        setup = cleanup = engaged = earlyPull = false;
        elapsed = phaseMs = pullMs = sampleMs = instance = mapDifficulty = samples = 0;
        prepared = 0; startHp = hp = peakHp = 0.f; damageToHer = 0; leaderTeleportAcks = 0; valithria.Clear();
        twoInDreamMs = noneOnHerMs = 0;
        members = {}; forcedLogout = {}; timeline.clear(); damageTaken.clear(); killers.clear();
        adds.clear(); deaths.clear(); addCasts.clear(); finalBySpell.clear(); pendingSpell.clear();
        addTimeline.clear();
        oldFocus = sPlayerbotAIConfig.ValithriaHealerFocus;
        oldPortals = sPlayerbotAIConfig.ValithriaPortals;
        oldAdds = sPlayerbotAIConfig.ValithriaAdds;
        oldTurns = sPlayerbotAIConfig.ValithriaPortalTurns;
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
        // The owner's auras are permanent and saved with the character: only a buffed run may start with them.
        ClearOwnerBuffs(p);
        if (!p->IsAlive()) { p->ResurrectPlayer(1.f); p->SpawnCorpseBones(); }
        int8 const spec = PremadeSpec(slot.cls, slot.spec);
        if (spec < 0) { Fail(std::string("premade spec missing: ") + slot.spec); return false; }
        uint32 const gearScore = PlayerbotFactory::CalcMixedGearScore(itemLevel, ITEM_QUALITY_EPIC);
        if (p->GetLevel() >= 80 && std::abs(p->GetAverageItemLevel() - float(itemLevel)) > 8.f)
        {
            // Another item level was asked for: regear once; later runs at it keep this gear.
            PlayerbotFactory factory(p, 80, ITEM_QUALITY_EPIC, gearScore);
            factory.InitEquipment(false);
            p->SaveToDB(false, false);
        }
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
        for (std::size_t i = 0; i < actors.size(); ++i)
            if (!Actor(i)->TeleportTo(631, 4203.5f + float(i % 5) * 2.f, 2484.0f + float(i / 5) * 2.f, 364.96f, 0.f))
            { Fail("fixture teleport refused"); return false; }
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
        if (leader->GetMap()->GetDifficulty() != Wanted())
        { Fail("instance difficulty differs from the request"); return false; }
        Creature* dragon = leader->GetMap()->GetCreature(script->GetGuidData(10));
        if (!dragon || !dragon->IsAlive() || !dragon->IsInWorld()) return false;
        valithria = dragon->GetGUID();
        GET_PLAYERBOT_AI(leader)->SetMaster(leader);
        for (std::size_t i = 0; i < actors.size(); ++i)
        {
            PlayerbotAI* ai = GET_PLAYERBOT_AI(Actor(i));
            if (i != EncounterLeader) ai->SetMaster(leader);
            // The live install runs Playerbots.BotCheats = "food,taxi,raid"; the dojo's config keeps stock
            // rules, so the pull gives these ten the live raid cheat (a taunt fixes the add on the tank).
            // It touches no healer's mana, health, cast or target.
            ai->SetCheat(BotCheatMask::raid);
            ai->ResetStrategies(false);
            ai->ChangeStrategy("+icc", BOT_STATE_COMBAT);
            ai->ChangeStrategy("+icc", BOT_STATE_NON_COMBAT);
        }
        sPlayerbotAIConfig.ValithriaHealerFocus = mode != "before";
        sPlayerbotAIConfig.ValithriaPortals = mode != "noportals";
        sPlayerbotAIConfig.ValithriaAdds = mode == "adds" || mode == "turns";
        sPlayerbotAIConfig.ValithriaPortalTurns = mode == "turns";
        if (ownerBuffs) OwnerBuffs(leader);
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
        // A member close to an archmage may already have started it; the trigger still zones the whole
        // raid in, or a lone engagement can drop out of combat and reset her encounter.
        earlyPull = script->GetBossState(10) == IN_PROGRESS;
        // Belt and braces with the account's skip permission: the lower-spire states, set in the same tick
        // as the pull (a lower-spire boss that loads later resets its own state, as raid acceptance found).
        for (uint32 boss = 0; boss < 4; ++boss) script->SetBossState(boss, DONE);
        trigger->AI()->DoZoneInCombat();
        Phase("observe");
    }

    // The owner's client acknowledges his own near teleports (his Blink). The self-bot leader has no client: playerbots
    // leaves a self-bot's acknowledgement to its client, and while the leader waits every bot with him as master skips
    // its turn (PlayerbotAI::DoNextAction), so one Blink froze the whole raid for 30 s or to the wipe. This is the
    // acknowledgement his client sends; it moves nobody.
    void AckLeaderTeleport()
    {
        Player* leader = Actor(EncounterLeader);
        if (!leader || !leader->IsBeingTeleportedNear()) return;
        Player* mover = leader->m_mover ? leader->m_mover->ToPlayer() : nullptr;
        if (!mover) return;
        WorldPacket ack(MSG_MOVE_TELEPORT_ACK, 20);
        ack << mover->GetPackGUID();
        ack << uint32(0);
        ack << uint32(0);
        leader->GetSession()->HandleMoveTeleportAck(ack);
        ++leaderTeleportAcks;
    }

    // The owner's auras on the leader, again whenever one is gone (none survives a death).
    static void OwnerBuffs(Player* leader)
    {
        if (!leader || !leader->IsAlive()) return;
        for (uint32 spell : OwnerAuras)
            if (!leader->HasAura(spell)) leader->AddAura(spell, leader);
    }

    static void ClearOwnerBuffs(Player* p)
    {
        for (uint32 spell : OwnerAuras)
            p->RemoveAurasDueToSpell(spell);
    }

    static AddVictim VictimOf(int slot)
    {
        if (slot < 0) return OnNobody;
        if (std::find(std::begin(TankSlots), std::end(TankSlots), std::size_t(slot)) != std::end(TankSlots))
            return OnTank;
        return Healer(std::size_t(slot)) ? OnHealer : OnDps;
    }

    // Her adds in this sample: when each came and went, whom it was on, and how many were on each fixture.
    void SampleAdds(uint32 step, Creature* dragon)
    {
        for (EncounterMember& m : members) m.load = 0;
        std::array<uint32, AddKinds.size()> living{};
        std::set<ObjectGuid> seen;
        if (dragon)
            for (AddKind const& kind : AddKinds)
            {
                std::list<Creature*> found;
                dragon->GetCreatureListWithEntryInGrid(found, kind.entry, 150.f);
                for (Creature* c : found)
                {
                    // A blistering zombie never dies: it keeps 1 health, bursts, turns unselectable and despawns.
                    if (!c || !c->IsAlive() || c->HasUnitFlag(UNIT_FLAG_NOT_SELECTABLE)) continue;
                    EncounterAdd& a = adds[c->GetGUID()];
                    if (!a.entry) { a.entry = kind.entry; a.seenMs = pullMs; }
                    a.lastMs = pullMs;
                    seen.insert(c->GetGUID());
                    ++living[AddKindOf(kind.entry)];
                    Unit* victim = c->GetVictim();
                    int const slot = Slot(victim);
                    AddVictim const on = victim && victim == dragon ? OnHer : VictimOf(slot);
                    a.onMs[on] += step;
                    if (slot >= 0) { a.onSlotMs[slot] += step; ++members[slot].load; }
                    if (on == OnTank && !a.tankedMs) a.tankedMs = pullMs;
                }
            }
        for (auto& [guid, a] : adds)
            if (a.alive && !seen.count(guid)) a.alive = false;
        for (EncounterMember& m : members)
        {
            m.maxLoad = std::max(m.maxLoad, m.load);
            if (m.load >= 2) m.multiMs += step;
        }
        if (addTimeline.empty() || pullMs - addTimeline.back()[0].get<uint32>() >= 10000)
        {
            json counts = json::object();
            for (std::size_t k = 0; k < AddKinds.size(); ++k)
                if (living[k]) counts[AddKinds[k].name] = living[k];
            json tanks = json::array();
            for (std::size_t t : TankSlots)
            {
                Player* p = Actor(t);
                tanks.push_back(p && p->IsAlive() ? std::round(p->GetHealthPct()) : 0.f);
            }
            uint32 up = 0;
            for (std::size_t i = 0; i < actors.size(); ++i)
                if (Player* p = Actor(i); p && p->IsAlive()) ++up;
            addTimeline.push_back(json::array({pullMs, std::round(hp * 10.f) / 10.f, up, tanks, counts}));
        }
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
        SampleAdds(step, dragon);
        if (ownerBuffs) OwnerBuffs(Actor(EncounterLeader));
        uint32 alive = 0, dreamers = 0, onHer = 0;
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
            if (dreaming) { m.dreamMs += step; ++dreamers; }
            if (Aura* a = p->GetAura(EmeraldVigor)) m.maxVigor = std::max<uint32>(m.maxVigor, a->GetStackAmount());
            if (Aura* a = p->GetAura(TwistedNightmares))
                m.maxTwisted = std::max<uint32>(m.maxTwisted, a->GetStackAmount());
            float const mana = p->GetPowerPct(POWER_MANA);
            m.minMana = std::min(m.minMana, mana);
            if (mana < 10.f) m.lowManaMs += step;
            if (!dreaming && dragon && p->IsWithinDistInMap(dragon, 40.f) && p->IsWithinLOSInMap(dragon))
            {
                m.inReachMs += step;
                ++onHer;
            }
        }
        if (dreamers >= 2) twoInDreamMs += step;
        if (alive && !onHer) noneOnHerMs += step;
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
        // The core may refuse the first zone-in (a member still between maps); repeat it every 3 s.
        if (!engaged && pullMs / 3000 != (pullMs - diff) / 3000)
            if (Creature* trigger = map->GetCreature(script->GetGuidData(35)))
                trigger->AI()->DoZoneInCombat();
        if (!engaged && pullMs > 60000) { Fail("the encounter never engaged"); return; }
        if (outcome.empty())
        {
            if (boss == DONE) outcome = "done";
            else if (engaged && boss == NOT_STARTED) outcome = "reset";
            else if (pullMs >= EncounterCapMs) outcome = "timeout";
        }
        if (!outcome.empty())
        {
            // She can reach DONE between samples: take her health once more before leaving.
            if (dragon && dragon->IsAlive())
            {
                hp = dragon->GetHealthPct();
                peakHp = std::max(peakHp, hp);
            }
            timeline.emplace_back(pullMs, hp);
            Phase("cleanup");
        }
    }

    void Update(uint32 diff)
    {
        std::lock_guard<std::recursive_mutex> lock(events);
        if (!active) return;
        elapsed += diff; phaseMs += diff;
        if ((state == "login" || state == "prepare" || state == "arrive") && elapsed > EncounterSetupMs)
            Fail("setup deadline");
        if (state == "prepull" || state == "observe") AckLeaderTeleport();
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
            if (Arrived() && !Assemble() && state == "arrive" && phaseMs > 60000)
                Fail("Valithria missing from the instance");
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
            sPlayerbotAIConfig.ValithriaPortals = oldPortals;
            sPlayerbotAIConfig.ValithriaAdds = oldAdds;
            sPlayerbotAIConfig.ValithriaPortalTurns = oldTurns;
            for (std::size_t i = 0; i < actors.size(); ++i)
                if (Player* p = Actor(i))
                {
                    p->CombatStop(true);
                    ClearOwnerBuffs(p);
                    GET_PLAYERBOT_AI(p)->SetCheat(BotCheatMask::none);
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
                if (Unit* victim = p->GetVictim()) row["target"] = Label(victim);
            }
            roster.push_back(row);
        }
        json out = {{"format", 1}, {"kind", "dojo-valithria-encounter"}, {"token", token},
            {"difficulty", difficulty}, {"mode", mode}, {"focus", mode != "before"}, {"portals", mode != "noportals"},
            {"adds", mode == "adds" || mode == "turns"}, {"portal_turns", mode == "turns"},
            {"owner_buffs", ownerBuffs}, {"leader_teleport_acks", leaderTeleportAcks},
            {"two_in_dream_ms", twoInDreamMs}, {"none_on_her_ms", noneOnHerMs},
            {"raid_cheat", true},
            {"state", state},
            {"setup", setup}, {"cleanup", cleanup}, {"failure", failure}, {"outcome", outcome},
            {"instance", instance}, {"map_difficulty", mapDifficulty}, {"engaged", engaged},
            {"early_pull", earlyPull}, {"pull_ms", pullMs},
            {"samples", samples}, {"start_hp", startHp}, {"hp", hp}, {"peak_hp", peakHp},
            {"damage_to_her", damageToHer}, {"item_level_cap", itemLevel}, {"members", roster}};
        if (InstanceMap* map = Instance())
            if (InstanceScript* script = map->GetInstanceScript())
            {
                out["boss_state"] = uint32(script->GetBossState(10));
                if (Creature* trigger = map->GetCreature(script->GetGuidData(35)))
                    out["trigger_combat"] = trigger->IsInCombat();
                if (Creature* dragon = map->GetCreature(valithria))
                {
                    out["her_position"] = {dragon->GetPositionX(), dragon->GetPositionY(), dragon->GetPositionZ()};
                    out["live_adds"] = LiveAdds(dragon);
                }
                if (Player* leader = Actor(EncounterLeader))
                    if (Group* group = leader->GetGroup())
                    {
                        json marks = json::object();
                        for (auto const& [icon, name] : {std::pair<uint8, char const*>{7, "skull"}, {6, "cross"}})
                            if (Unit* marked = ObjectAccessor::GetUnit(*leader, group->GetTargetIcon(icon)))
                                marks[name] = Label(marked);
                        out["marks"] = marks;
                    }
            }
        if (full)
        {
            json line = json::array();
            for (auto const& [ms, pct] : timeline) line.push_back({ms, pct});
            out["timeline"] = line;
            std::vector<std::pair<uint64, uint32>> top;
            for (auto const& [spell, amount] : damageTaken) top.emplace_back(amount, spell);
            std::sort(top.rbegin(), top.rend());
            json hurt = json::array();
            for (std::size_t i = 0; i < top.size() && i < 12; ++i) hurt.push_back({top[i].second, top[i].first});
            out["damage_taken_by_spell"] = hurt;
            json deaths = json::array();
            for (auto const& [ms, entry] : killers) deaths.push_back({ms, entry});
            out["killers"] = deaths;
            AddDetail(out);
        }
        return out;
    }

    // "<kind>#<counter>" for an add, "<name>" for a fixture, "her", or "c<entry>" for another creature.
    std::string Label(Unit const* unit) const
    {
        if (!unit) return "-";
        if (unit->GetGUID() == valithria) return "her";
        if (int const slot = Slot(unit); slot >= 0) return Roster[slot].name;
        if (unit->IsCreature())
            return KindName(unit->GetEntry()) + "#" + std::to_string(unit->GetGUID().GetCounter());
        return unit->IsPlayer() ? "player" : "unit";
    }

    // Every living add now: label, position, health and victim (the trace's per-poll positions).
    json LiveAdds(Creature* dragon) const
    {
        json rows = json::array();
        for (AddKind const& kind : AddKinds)
        {
            std::list<Creature*> found;
            dragon->GetCreatureListWithEntryInGrid(found, kind.entry, 150.f);
            for (Creature* c : found)
                if (c && c->IsAlive() && !c->HasUnitFlag(UNIT_FLAG_NOT_SELECTABLE))
                    rows.push_back(json::array({Label(c), std::round(c->GetPositionX() * 10.f) / 10.f,
                        std::round(c->GetPositionY() * 10.f) / 10.f, std::round(c->GetHealthPct()),
                        Label(c->GetVictim())}));
        }
        return rows;
    }

    template <typename Map, typename Name>
    static json Top(Map const& values, std::size_t limit, Name name)
    {
        std::vector<std::pair<uint64, typename Map::key_type>> top;
        for (auto const& [key, amount] : values) top.emplace_back(uint64(amount), key);
        std::sort(top.rbegin(), top.rend());
        json rows = json::object();
        for (std::size_t i = 0; i < top.size() && i < limit; ++i) rows[name(top[i].second)] = top[i].first;
        return rows;
    }

    static json Hits(std::vector<EncounterHit> const& hits)
    {
        json rows = json::array();
        for (EncounterHit const& h : hits)
            rows.push_back({h.ms, KindName(h.entry), h.spell, h.amount, std::round(h.hpBefore)});
        return rows;
    }

    // The add detail of a full status: per fixture, per add and per death.
    void AddDetail(json& out)
    {
        auto spellName = [](uint32 spell) { return std::to_string(spell); };
        auto keyName = [](std::string const& key) { return key; };
        for (std::size_t i = 0; i < actors.size(); ++i)
        {
            EncounterMember const& m = members[i];
            json& row = out["members"][i];
            row["taken_by"] = Top(m.takenBy, 8, KindName);
            row["taken_by_spell"] = Top(m.takenBySpell, 10, spellName);
            json healed = json::object();
            for (std::size_t h = 0; h < actors.size(); ++h)
                if (m.healedBy[h]) healed[Roster[h].name] = m.healedBy[h];
            row["healed_by"] = healed;
            row["max_adds_on"] = m.maxLoad;
            row["two_plus_adds_ms"] = m.multiMs;
            row["casts"] = Top(m.casts, 25, spellName);
            row["casts_on_adds"] = Top(m.castsOnAdds, 25, keyName);
            json done = json::object();
            for (std::size_t k = 0; k < AddKinds.size(); ++k)
                if (m.doneToAdds[k]) done[AddKinds[k].name] = m.doneToAdds[k];
            row["damage_to_adds"] = done;
        }
        json addRows = json::array();
        for (auto const& [guid, a] : adds)
        {
            json on = json::object();
            for (std::size_t v = 0; v < AddVictims; ++v)
                if (a.onMs[v]) on[AddVictimNames[v]] = a.onMs[v];
            json onSlot = json::object();
            for (std::size_t s = 0; s < actors.size(); ++s)
                if (a.onSlotMs[s]) onSlot[Roster[s].name] = a.onSlotMs[s];
            addRows.push_back({{"kind", KindName(a.entry)}, {"seen_ms", a.seenMs}, {"last_ms", a.lastMs},
                {"alive", a.alive}, {"tanked_ms", a.tankedMs}, {"on_ms", on}, {"on_member_ms", onSlot},
                {"dealt", a.dealt}, {"taken", a.taken}});
        }
        out["add_list"] = addRows;
        json deathRows = json::array();
        for (EncounterDeath const& d : deaths)
            deathRows.push_back({{"ms", d.ms}, {"name", Roster[d.slot].name}, {"killer", KindName(d.killer)},
                {"adds_on", d.load}, {"recap", Hits(d.recap)}});
        out["deaths"] = deathRows;
        json casts = json::object();
        for (auto const& [spell, c] : addCasts)
            casts[std::to_string(spell)] = {{"prepared", c.prepared}, {"cast", c.cast}, {"cancelled", c.cancelled}};
        out["add_casts"] = casts;
        out["final_damage_by_spell"] = Top(finalBySpell, 16, spellName);
        out["add_timeline"] = addTimeline;
    }

    // Damage on a fixture, after mitigation and absorbs, with the spell its own hook named (0 = melee).
    void Hurt(Unit* attacker, Unit* victim, uint32 damage)
    {
        int const slot = Slot(victim);
        if (slot >= 0)
        {
            auto pending = pendingSpell.find(victim->GetGUID());
            uint32 const spell = pending == pendingSpell.end() ? 0 : pending->second;
            if (pending != pendingSpell.end()) pendingSpell.erase(pending);
            uint32 const entry = attacker && attacker->IsCreature() ? attacker->GetEntry() : 0;
            EncounterMember& m = members[slot];
            m.takenBy[entry] += damage;
            m.takenBySpell[spell] += damage;
            finalBySpell[spell] += damage;
            m.recent.push_back({pullMs, entry, spell, damage, victim->GetHealthPct()});
            if (m.recent.size() > 10) m.recent.erase(m.recent.begin());
            if (attacker)
                if (auto add = adds.find(attacker->GetGUID()); add != adds.end()) add->second.dealt += damage;
            return;
        }
        Unit* const owner = attacker ? attacker->GetCharmerOrOwnerOrSelf() : nullptr;
        int const from = Slot(owner);
        if (from < 0 || !victim) return;
        if (auto add = adds.find(victim->GetGUID()); add != adds.end())
        {
            add->second.taken += damage;
            members[from].doneToAdds[AddKindOf(add->second.entry)] += damage;
        }
    }

    void Died(Unit* unit, Unit* killer)
    {
        int const slot = Slot(unit);
        if (slot < 0) return;
        uint32 const entry = killer && killer->IsCreature() ? killer->GetEntry() : 0;
        killers.emplace_back(pullMs, entry);
        deaths.push_back({pullMs, uint32(slot), entry, members[slot].load, members[slot].recent});
    }

    void Cast(Player* player, Spell* spell)
    {
        int const slot = Slot(player);
        if (slot < 0) return;
        SpellInfo const* info = spell->GetSpellInfo();
        EncounterMember& m = members[slot];
        ++m.casts[info->Id];
        if (Unit* target = spell->m_targets.GetUnitTarget())
            if (auto add = adds.find(target->GetGUID()); add != adds.end())
                ++m.castsOnAdds[std::to_string(info->Id) + ">" + KindName(add->second.entry)];
        if (IsManaCooldown(info->Id))
            ++m.manaCooldowns;
        else if (spell->m_CastItem && info->Effects[0].Effect == SPELL_EFFECT_ENERGIZE &&
                 info->Effects[0].MiscValue == POWER_MANA)
            ++m.potions;
    }

    // An add's own cast in the measured instance: begun, finished or cancelled (an interrupt cancels it).
    AddCasts* AddCast(Unit* caster, SpellInfo const* info)
    {
        if (state != "observe" || !caster || !info || !caster->IsCreature() || caster->GetInstanceId() != instance ||
            AddKindOf(caster->GetEntry()) < 0)
            return nullptr;
        return &addCasts[info->Id];
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
        if (encounter.state != "observe" || !receiver || !gain) return;
        // A fixture's healing on another, its totems' included; on her, its own (as 0021 counted it).
        if (int const to = encounter.Slot(receiver); to >= 0)
        {
            if (int const from = encounter.Slot(healer ? healer->GetCharmerOrOwnerOrSelf() : nullptr); from >= 0)
                encounter.members[to].healedBy[from] += gain;
            return;
        }
        int const slot = encounter.Slot(healer);
        if (slot < 0 || receiver->GetGUID() != encounter.valithria) return;
        ++encounter.members[slot].heals;
        encounter.members[slot].healing += gain;
    }
    void OnDamage(Unit* attacker, Unit* victim, uint32& damage) override
    {
        std::lock_guard<std::recursive_mutex> lock(encounter.events);
        if (encounter.state != "observe" || !victim) return;
        if (victim->GetGUID() == encounter.valithria)
            encounter.damageToHer += damage;
        else if (damage)
            encounter.Hurt(attacker, victim, damage);
    }
};

class EncounterHurt : public UnitScript
{
public:
    EncounterHurt() : UnitScript("DojoEncounterHurt", true,
        {UNITHOOK_MODIFY_SPELL_DAMAGE_TAKEN, UNITHOOK_MODIFY_MELEE_DAMAGE, UNITHOOK_MODIFY_PERIODIC_DAMAGE_AURAS_TICK,
         UNITHOOK_ON_UNIT_DEATH}) { }
    // Before mitigation (kept as damage_taken_by_spell); each also names the spell of the hit that follows.
    void ModifySpellDamageTaken(Unit* target, Unit* /*attacker*/, int32& damage, SpellInfo const* spellInfo) override
    {
        std::lock_guard<std::recursive_mutex> lock(encounter.events);
        if (encounter.state != "observe" || encounter.Slot(target) < 0) return;
        encounter.pendingSpell[target->GetGUID()] = spellInfo ? spellInfo->Id : 0;
        if (damage > 0) encounter.damageTaken[spellInfo ? spellInfo->Id : 0] += uint64(damage);
    }
    void ModifyMeleeDamage(Unit* target, Unit* /*attacker*/, uint32& damage) override
    {
        std::lock_guard<std::recursive_mutex> lock(encounter.events);
        if (encounter.state != "observe" || encounter.Slot(target) < 0) return;
        encounter.pendingSpell[target->GetGUID()] = 0;
        if (damage) encounter.damageTaken[0] += damage;
    }
    void ModifyPeriodicDamageAurasTick(Unit* target, Unit* /*attacker*/, uint32& /*damage*/,
                                       SpellInfo const* spellInfo) override
    {
        std::lock_guard<std::recursive_mutex> lock(encounter.events);
        if (encounter.state == "observe" && encounter.Slot(target) >= 0)
            encounter.pendingSpell[target->GetGUID()] = spellInfo ? spellInfo->Id : 0;
    }
    void OnUnitDeath(Unit* unit, Unit* killer) override
    {
        std::lock_guard<std::recursive_mutex> lock(encounter.events);
        if (encounter.state == "observe") encounter.Died(unit, killer);
    }
};

class EncounterAddCasts : public AllSpellScript
{
public:
    EncounterAddCasts() : AllSpellScript("DojoEncounterAddCasts",
        {ALLSPELLHOOK_ON_PREPARE, ALLSPELLHOOK_ON_CAST, ALLSPELLHOOK_ON_CAST_CANCEL}) { }
    void OnSpellPrepare(Spell* /*spell*/, Unit* caster, SpellInfo const* spellInfo) override
    {
        std::lock_guard<std::recursive_mutex> lock(encounter.events);
        if (AddCasts* c = encounter.AddCast(caster, spellInfo)) ++c->prepared;
    }
    void OnSpellCast(Spell* /*spell*/, Unit* caster, SpellInfo const* spellInfo, bool /*skipCheck*/) override
    {
        std::lock_guard<std::recursive_mutex> lock(encounter.events);
        if (AddCasts* c = encounter.AddCast(caster, spellInfo)) ++c->cast;
    }
    void OnSpellCastCancel(Spell* /*spell*/, Unit* caster, SpellInfo const* spellInfo, bool /*bySelf*/) override
    {
        std::lock_guard<std::recursive_mutex> lock(encounter.events);
        if (AddCasts* c = encounter.AddCast(caster, spellInfo)) ++c->cancelled;
    }
};

class EncounterCasts : public PlayerScript
{
public:
    EncounterCasts() : PlayerScript("DojoEncounterCasts", {PLAYERHOOK_ON_SPELL_CAST}) { }
    void OnPlayerSpellCast(Player* player, Spell* spell, bool /*skipCheck*/) override
    {
        std::lock_guard<std::recursive_mutex> lock(encounter.events);
        if (encounter.state == "observe" && spell) encounter.Cast(player, spell);
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
            handler->SendSysMessage("usage: dojo raid start valithria|twins|poison|lk-terenas|lk-defile <token> normal|negative");
        else if (encounter.active) handler->SendSysMessage("dojo raid: refused: an encounter run is active");
        else handler->SendSysMessage("dojo raid: " + run.Start(scenario, token, mode == "negative"));
        return true;
    }
    static bool EncounterStart(ChatHandler* handler, Tail args)
    {
        std::istringstream in{std::string(args)};
        std::string token, difficulty, mode, buffs, extra;
        uint32 gear = EncounterItemLevel;
        in >> token >> difficulty >> mode >> gear >> buffs >> extra;
        if (!extra.empty() || mode.empty() || (!buffs.empty() && buffs != "buffed"))
            handler->SendSysMessage("usage: dojo raid encounter start <token> 10n|10h before|after|noportals|adds "
                                    "<ilvl> [buffed]");
        else
            handler->SendSysMessage("dojo encounter: " +
                                    encounter.Start(token, difficulty, mode, gear, buffs == "buffed", run.active));
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
    RaidEvents() : UnitScript("DojoRaidEvents", true, {UNITHOOK_ON_HEAL, UNITHOOK_ON_DAMAGE,
        UNITHOOK_MODIFY_SPELL_DAMAGE_TAKEN, UNITHOOK_MODIFY_PERIODIC_DAMAGE_AURAS_TICK,
        UNITHOOK_ON_UNIT_DEATH}) { }
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
        run.RecordDamage(victim, attacker, damage, 0, 0);
        if (run.state == "observe" && run.scenario == "poison" && attacker && victim &&
            attacker->GetEntry() == 16027 && victim->GetGUID() == run.actors[0] && damage > 0) ++run.damageHits;
        if (run.state == "observe" && run.scenario == "lk-defile" && attacker && victim &&
            attacker->GetEntry() == 38757 && victim->GetGUID() == run.actors[0] && damage > 0)
        {
            ++run.damageHits;
            if (run.escapeMs && run.phaseMs > run.hazardStartMs + run.escapeMs + 1000) ++run.lateHits;
        }
    }
    void ModifySpellDamageTaken(Unit* victim, Unit* attacker, int32& damage, SpellInfo const* info) override
    {
        if (!info || damage <= 0) return;
        std::lock_guard<std::recursive_mutex> lock(run.events);
        run.RecordDamage(victim, attacker, uint32(damage), info->Id, 1);
    }
    void ModifyPeriodicDamageAurasTick(Unit* victim, Unit* attacker, uint32& damage, SpellInfo const* info) override
    {
        if (!info || !damage) return;
        std::lock_guard<std::recursive_mutex> lock(run.events);
        run.RecordDamage(victim, attacker, damage, info->Id, 2);
    }
    void OnUnitDeath(Unit* victim, Unit* killer) override
    {
        std::lock_guard<std::recursive_mutex> lock(run.events);
        std::size_t const slot = run.DiagnosticSlot(victim);
        if (slot == run.actors.size()) return;
        if (run.deaths.size() == 8) run.deaths.erase(run.deaths.begin());
        run.deaths.push_back({{"elapsed_ms", run.elapsed}, {"slot", slot},
            {"killer_entry", killer ? killer->GetEntry() : 0}, {"health", victim->GetHealth()},
            {"max_health", victim->GetMaxHealth()}, {"x", victim->GetPositionX()},
            {"y", victim->GetPositionY()}, {"z", victim->GetPositionZ()},
            {"damage_sequence", run.damageSequence}});
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
    new EncounterHurt();
    new EncounterAddCasts();
    new EncounterTicks();
    AddSC_dojo_offline();
    AddSC_dojo_human();
    AddSC_dojo_lead();
    AddSC_dojo_vehicle();
    AddSC_dojo_yogg();
}
#else
void AddSC_dojo_raid() { }
#endif
#else
void AddSC_dojo_raid() { }
#endif
