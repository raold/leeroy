// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.
#include "DojoRaid.h"
#include "DojoOffline.h"
#include "DojoHuman.h"
#include "DojoLead.h"

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
#include <algorithm>
#include <array>
#include <mutex>
#include <sstream>

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
    float finalHazardClearance = 0.f;
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
        setup = cleanup = combatObserved = competition = false; target.Clear();
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
        else if (scenario == "lk-terenas" || scenario == "lk-defile")
        {
            // Exercise the shipped ICC strategy with its ordinary follow/heal
            // competitors intact. Only the feature switch differs in the control.
            for (uint32 boss = 0; boss < 12; ++boss) script->SetBossState(boss, DONE);
            script->SetBossState(12, IN_PROGRESS);
            ai->ChangeStrategy("+icc", BOT_STATE_COMBAT);
            ai->ChangeStrategy("+icc", BOT_STATE_NON_COMBAT);
            if (scenario == "lk-terenas")
            {
                Creature* terenas = bot->SummonCreature(36823, 496.f, -2512.f, 1049.95f, 0.f,
                    TEMPSUMMON_TIMED_DESPAWN, 45000);
                Creature* warden = bot->SummonCreature(36824, 505.f, -2512.f, 1049.95f, 3.14f,
                    TEMPSUMMON_TIMED_DESPAWN, 45000);
                if (!terenas || !warden || !PlayerbotAI::IsHeal(bot))
                { Fail("Frostmourne actors or healer role missing"); return; }
                target = terenas->GetGUID();
                terenas->AI()->DoAction(-36823); // Core ACTION_FROSTMOURNE_INTRO sets initial half health.
                terenas->AI()->AttackStart(warden);
                warden->AI()->AttackStart(terenas);
                // The ordinary follow target stays outside Frostmourne. Never
                // relocate the measured actor, cast for it or write Terenas HP.
                owner->NearTeleportTo(503.62f, -2124.65f, 840.857f, 0.f);
            }
            else
            {
                Creature* lichKing = bot->FindNearestCreature(36597, 150.f);
                if (!lichKing) { Fail("native Lich King missing"); return; }
                // Cast the real core spell: its summon/aura/growth are the stimulus.
                lichKing->CastSpell(bot, 72762, true);
            }
        }
        else
        {
            Leeroy::Guild::InstallHazardFilter();
            Leeroy::Guild::DojoHazardOwner(control ? 0 : owner->GetGUID().GetCounter());
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
            if (Creature* poison = bot->FindNearestCreature(16027, 70.f))
            {
                ++hazardsSeen;
                minimumClearance = std::min(minimumClearance, bot->GetExactDist2d(poison));
            }
            if (bot->GetExactDist2d(owner) < 6.f)
            {
                ++crossings;
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
            encounter["boss_state"] = uint32(script->GetBossState(scenario == "valithria" ? 10 : scenario == "twins" ? 3 :
                scenario == "lk-terenas" || scenario == "lk-defile" ? 12 : 0));
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
            {"inside_samples", insideSamples}, {"late_hits", lateHits}, {"encounter_samples", encounterSamples},
            {"final_hazard_clearance", finalHazardClearance},
            {"switch_ms", switchMs}, {"crossings", crossings}, {"hazard_samples", hazardsSeen},
            {"minimum_clearance", minimumClearance}, {"poison_hits", scenario == "poison" ? damageHits : 0},
            {"defile_hits", scenario == "lk-defile" ? damageHits : 0}, {"fixtures", fixtures}, {"encounter", encounter}};
    }
};
RaidRun run;

class RaidCommands : public CommandScript
{
public:
    RaidCommands() : CommandScript("DojoRaidCommands") { }
    ChatCommandTable GetCommands() const override
    {
        static ChatCommandTable commands{{"start", Start, SEC_CONSOLE, Console::Yes},
            {"status", Status, SEC_CONSOLE, Console::Yes}, {"abort", Abort, SEC_CONSOLE, Console::Yes}};
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
        else handler->SendSysMessage("dojo raid: " + run.Start(scenario, token, mode == "negative"));
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
        if (run.state == "observe" && run.scenario == "lk-defile" && attacker && victim &&
            attacker->GetEntry() == 38757 && victim->GetGUID() == run.actors[0] && damage > 0)
        {
            ++run.damageHits;
            if (run.escapeMs && run.phaseMs > run.hazardStartMs + run.escapeMs + 1000) ++run.lateHits;
        }
    }
};
}

void AddSC_dojo_raid()
{
    new RaidCommands(); new RaidTicks(); new RaidEvents();
    AddSC_dojo_offline(); AddSC_dojo_human(); AddSC_dojo_lead();
}
#else
void AddSC_dojo_raid() { }
#endif
#else
void AddSC_dojo_raid() { }
#endif
