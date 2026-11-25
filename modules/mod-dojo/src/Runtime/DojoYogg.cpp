// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.
#include "DojoYogg.h"
#if DOJO_TRAINING_BUILD
#if defined(DOJO_OFFLINE_ACCEPTANCE) && DOJO_OFFLINE_ACCEPTANCE
#include "DojoHuman.h"
#include "DojoRaid.h"
#include "DojoRuntime.h"
#include "AccountMgr.h"
#include "CharacterCache.h"
#include "Chat.h"
#include "CommandScript.h"
#include "Creature.h"
#include "CreatureAI.h"
#include "Group.h"
#include "GroupMgr.h"
#include "InstanceScript.h"
#include "Map.h"
#include "MotionMaster.h"
#include "ObjectAccessor.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "PlayerbotAIConfig.h"
#include "Playerbots.h"
#include "PlayerScript.h"
#include "Spell.h"
#include "SpellAuraEffects.h"
#include "UnitScript.h"
#include "WorldScript.h"
#include <algorithm>
#include <array>
#include <list>
#include <mutex>
#include <set>
#include <sstream>

using nlohmann::json;
using namespace Acore::ChatCommands;
namespace
{
constexpr std::array<char const*, 9> Names{"Dojotank", "Dojoguard", "Dojoheala", "Dojohealb", "Dojohealc",
    "Dojodpsa", "Dojodpsb", "Dojodpsd", "Dojodpse"};
bool Below(Player* p)
{
    return p && p->GetMapId() == 603 && p->GetPositionX() > 1830.f && p->GetPositionX() < 2160.f &&
        p->GetPositionY() > -190.f && p->GetPositionY() < 125.f && p->GetPositionZ() > 225.f && p->GetPositionZ() < 285.f;
}
bool Influence(uint32 entry) { return entry == 33943 || (entry >= 33716 && entry <= 33720) || entry == 33567 || entry == 33433; }
struct Trial
{
    std::recursive_mutex mutex;
    std::array<ObjectGuid, Names.size()> actors{};
    std::array<bool, Names.size()> entered{}, exited{}, healerEntrants{};
    std::array<bool, Names.size()> positioned{}, wasBelow{};
    std::array<Position, Names.size()> previous{};
    json failureFacts = json::object();
    std::array<uint32, Names.size()> portalCasts{}, exitCasts{}, minimumSanity{};
    std::set<ObjectGuid> influenceDeaths;
    ObjectGuid group, brainGuid, saraGuid;
    std::string token, state = "idle", failure;
    uint32 elapsed = 0, phaseMs = 0, samples = 0, maxGap = 0, illusion = 0, portals = 0;
    uint32 brainStart = 0, brainLeast = 0, brainHits = 0, exitRemaining = 0, initialSanity = 0;
    uint32 maxBelow = 0, maxHealersBelow = 0, minHealersAbove = 100, tanksBelow = 0;
    bool active = false, negative = false, ready = false, cleanup = false, humanGone = false, oldSupport = false;
    Player* Actor(std::size_t i) const
    {
        auto* p = ObjectAccessor::FindConnectedPlayer(actors[i]);
        return p && p->IsInWorld() && GET_PLAYERBOT_AI(p) ? p : nullptr;
    }
    void Phase(std::string const& next) { state = next; phaseMs = 0; }
    void Fail(std::string const& why) { failure = why; Phase("cleanup"); }
    std::string Start(std::string const& id, bool control)
    {
        std::lock_guard<std::recursive_mutex> lock(mutex);
        if (active || state == "cleanup_failed") return "fixture_busy";
        if (Dojo::State().plan.state != Dojo::RuntimeState::Armed || Dojo::State().manifest) return "fixture_not_armed";
        if (id.empty() || id.size() > 64 || id.find_first_not_of("abcdefghijklmnopqrstuvwxyz0123456789-") != std::string::npos)
            return "invalid_token";
        uint32 const account = AccountMgr::GetId("RNDBOTDOJO");
        for (std::size_t i = 0; i < actors.size(); ++i)
        {
            actors[i] = sCharacterCache->GetCharacterGuidByName(Names[i]);
            if (!account || !actors[i] || sCharacterCache->GetCharacterAccountIdByGuid(actors[i]) != account)
                return "fixture_account_mismatch";
            if (ObjectAccessor::FindConnectedPlayer(actors[i])) return "fixture_already_online";
        }
        auto why = Dojo::BeginHumanFixture(id);
        if (!why.empty()) return why;
        token = id; negative = control; failure.clear(); group.Clear(); brainGuid.Clear(); saraGuid.Clear();
        elapsed = samples = maxGap = illusion = portals = brainStart = brainLeast = brainHits = exitRemaining = initialSanity = 0;
        maxBelow = maxHealersBelow = tanksBelow = 0; minHealersAbove = 100;
        entered = {}; exited = {}; healerEntrants = {}; positioned = {}; wasBelow = {};
        portalCasts = {}; exitCasts = {}; minimumSanity.fill(100); influenceDeaths.clear();
        failureFacts = json::object();
        ready = cleanup = humanGone = false; active = true;
        oldSupport = sPlayerbotAIConfig.YoggBrainSupport;
        sPlayerbotAIConfig.YoggBrainSupport = true;
        for (auto guid : actors) Dojo::LoginRaidFixture(guid);
        Phase("login");
        return "started";
    }
    void Update(uint32 diff)
    {
        std::lock_guard<std::recursive_mutex> lock(mutex);
        if (!active) return;
        elapsed += diff; phaseMs += diff;
        if (elapsed > 270000 && state != "cleanup" && state != "logout") Fail("yogg_fixture_deadline");
        auto* human = Dojo::HumanFixture(token);
        if (state == "login")
        {
            auto why = Dojo::HumanFixtureError(token);
            if (!why.empty()) { Fail(why); return; }
            if (!human) return;
            for (std::size_t i = 0; i < actors.size(); ++i) if (!Actor(i)) return;
            if (human->GetGroup() || human->GetLevel() != 80 || !human->IsAlive()) { Fail("unclean_human_fixture"); return; }
            for (std::size_t i = 0; i < actors.size(); ++i)
                if (Actor(i)->GetGroup() || Actor(i)->GetLevel() != 80 || !Actor(i)->IsAlive())
                { Fail("unclean_yogg_fixture"); return; }
            auto* party = new Group();
            if (!party->Create(human)) { delete party; Fail("fixture_group_creation"); return; }
            sGroupMgr->AddGroup(party); party->ConvertToRaid(); group = party->GetGUID();
            for (std::size_t i = 0; i < actors.size(); ++i)
            {
                auto* p = Actor(i);
                if (!party->AddMember(p)) { Fail("fixture_group_join"); return; }
                auto* ai = GET_PLAYERBOT_AI(p); ai->SetMaster(human);
                ai->ChangeStrategy(controlStrategy(), BOT_STATE_COMBAT);
                ai->ChangeStrategy(controlStrategy(), BOT_STATE_NON_COMBAT);
                p->SetRaidDifficulty(RAID_DIFFICULTY_10MAN_NORMAL);
                p->SetMaxHealth(1000000); p->SetFullHealth(); // Mechanic fixture reserve, setup only.
                if (!p->TeleportTo(603, 1970.f + float(i % 3) * 2.f, -15.f - float(i / 3) * 2.f, 329.397f, 0.f))
                { Fail("fixture_teleport_refused"); return; }
            }
            human->SetRaidDifficulty(RAID_DIFFICULTY_10MAN_NORMAL);
            // Remain in the instance but outside the portal roster's 150-yard
            // availability radius, so all four portal slots belong to bots.
            if (!human->TeleportTo(603, 1805.f, -25.f, 330.f, 0.f)) { Fail("human_teleport_refused"); return; }
            Phase("arrive");
        }
        else if (state == "arrive")
        {
            if (!human || human->IsBeingTeleported()) return;
            for (std::size_t i = 0; i < actors.size(); ++i)
                if (!Actor(i) || Actor(i)->IsBeingTeleported()) return;
            auto* instance = human->GetInstanceScript();
            if (!instance || human->GetMapId() != 603) { Fail("yogg_instance_missing"); return; }
            for (uint32 b = 0; b < 12; ++b) instance->SetBossState(b, DONE);
            for (std::size_t i = 0; i < actors.size(); ++i)
            {
                auto* p = Actor(i);
                if (p->GetMap() != human->GetMap()) { Fail("fixture_instance_mismatch"); return; }
                auto* ai = GET_PLAYERBOT_AI(p); ai->SetMaster(human);
                ai->ChangeStrategy(controlStrategy(), BOT_STATE_COMBAT);
                ai->ChangeStrategy(controlStrategy(), BOT_STATE_NON_COMBAT);
            }
            auto* sara = instance->GetCreature(760);
            if (!sara) { Fail("native_sara_missing"); return; }
            saraGuid = sara->GetGUID();
            Phase("phase1");
        }
        else if (state == "phase1")
        {
            auto* leader = Actor(0);
            if (!leader) { Fail("yogg_actor_lost"); return; }
            auto* instance = leader->GetInstanceScript();
            auto* sara = instance ? instance->GetCreature(760) : nullptr;
            if (!sara || instance->GetBossState(12) != IN_PROGRESS) return;
            initialSanity = 0;
            for (std::size_t i = 0; i < actors.size(); ++i)
                if (auto* p = Actor(i); p && p->HasAura(63050)) ++initialSanity;
            if (initialSanity != actors.size()) return;
            // Select the phase boundary, then use a real guardian's native death
            // nova. The ensuing transformation, brain, portals and sanity are core
            // encounter behaviour. No measured bot is moved after arrival.
            auto* guardian = sara->SummonCreature(33136, sara->GetPosition(), TEMPSUMMON_TIMED_DESPAWN, 15000);
            if (!guardian) { Fail("guardian_stimulus_missing"); return; }
            sara->SetHealth(1);
            Unit::Kill(leader, guardian);
            Phase("portals");
        }
        else if (state == "portals")
        {
            auto* leader = Actor(0);
            auto* instance = leader ? leader->GetInstanceScript() : nullptr;
            auto* brain = instance ? instance->GetCreature(761) : nullptr;
            auto* yogg = instance ? instance->GetCreature(12) : nullptr;
            if (!leader || !instance || instance->GetBossState(12) != IN_PROGRESS) { Fail("yogg_encounter_lost"); return; }
            if (!brain || !yogg) return;
            std::list<Creature*> nearby;
            leader->GetCreatureListWithEntryInGrid(nearby, 34072, 150.f);
            portals = uint32(std::count_if(nearby.begin(), nearby.end(), [](Creature* p)
                { return p && p->IsAlive() && p->HasNpcFlag(UNIT_NPC_FLAG_SPELLCLICK); })) +
                uint32(std::count_if(portalCasts.begin(), portalCasts.end(), [](uint32 n) { return n > 0; }));
            auto* cast = brain->GetCurrentSpell(CURRENT_GENERIC_SPELL);
            if (portals != 4 || !cast || cast->GetSpellInfo()->Id != 64059 || cast->GetCastTimeRemaining() < 35000) return;
            if (!human || human->GetExactDist(yogg) <= 150.f) { Fail("human_reserved_a_portal_slot"); return; }
            brainGuid = brain->GetGUID(); illusion = brain->AI()->GetData(2);
            brainStart = brainLeast = brain->GetHealth(); ready = true;
            Phase("observe");
        }
        else if (state == "observe")
        {
            auto* leader = Actor(0);
            auto* instance = leader ? leader->GetInstanceScript() : nullptr;
            auto* brain = instance ? instance->GetCreature(761) : nullptr;
            if (!instance || instance->GetBossState(12) != IN_PROGRESS || !brain || brain->GetGUID() != brainGuid)
            { Fail("yogg_encounter_lost"); return; }
            ++samples; maxGap = std::max(maxGap, diff); brainLeast = std::min(brainLeast, brain->GetHealth());
            uint32 below = 0, healingBelow = 0, healingAbove = 0;
            for (std::size_t i = 0; i < actors.size(); ++i)
            {
                auto* p = Actor(i);
                if (!p || !p->IsAlive() || p->GetMap() != leader->GetMap() || p->IsCharmed())
                { Fail("yogg_actor_dead_lost_or_charmed"); return; }
                auto* sanity = p->GetAura(63050);
                uint32 const stacks = sanity ? sanity->GetStackAmount() : 0;
                minimumSanity[i] = std::min(minimumSanity[i], stacks);
                if (!stacks)
                {
                    failureFacts = {{"slot", i}, {"aura_present", sanity != nullptr},
                        {"health", p->GetHealth()}, {"charmed", p->IsCharmed()},
                        {"x", p->GetPositionX()}, {"y", p->GetPositionY()}, {"z", p->GetPositionZ()}};
                    Fail("sanity_lost"); return;
                }
                bool const downstairs = Below(p);
                if (!p->IsBeingTeleported())
                {
                    bool const portalTransition = downstairs != wasBelow[i] &&
                        (downstairs ? portalCasts[i] > 0 : exitCasts[i] > 0);
                    if (positioned[i] && !portalTransition &&
                        p->GetExactDist(previous[i]) > 1.f + p->GetSpeed(MOVE_RUN) * float(diff) * 0.0015f)
                    {
                        failureFacts = {{"slot", i}, {"distance", p->GetExactDist(previous[i])},
                            {"delta_ms", diff}, {"run_speed", p->GetSpeed(MOVE_RUN)},
                            {"motion", uint32(p->GetMotionMaster()->GetCurrentMovementGeneratorType())},
                            {"from", {previous[i].GetPositionX(), previous[i].GetPositionY(), previous[i].GetPositionZ()}},
                            {"to", {p->GetPositionX(), p->GetPositionY(), p->GetPositionZ()}},
                            {"downstairs", downstairs}, {"portal_casts", portalCasts[i]}, {"exit_casts", exitCasts[i]}};
                        Fail("measured_yogg_actor_jumped"); return;
                    }
                    previous[i] = p->GetPosition(); positioned[i] = true; wasBelow[i] = downstairs;
                }
                if (downstairs)
                {
                    ++below; entered[i] = true;
                    if (PlayerbotAI::IsHeal(p)) { ++healingBelow; healerEntrants[i] = true; }
                    if (PlayerbotAI::IsTank(p)) ++tanksBelow;
                }
                else if (p->GetPositionZ() > 300.f)
                {
                    if (PlayerbotAI::IsHeal(p)) ++healingAbove;
                    if (entered[i] && exitCasts[i]) exited[i] = true;
                }
            }
            maxBelow = std::max(maxBelow, below); maxHealersBelow = std::max(maxHealersBelow, healingBelow);
            minHealersAbove = std::min(minHealersAbove, healingAbove);
            auto* cast = brain->GetCurrentSpell(CURRENT_GENERIC_SPELL);
            uint32 const remaining = cast && cast->GetSpellInfo()->Id == 64059 ? std::max(0, cast->GetCastTimeRemaining()) : 0;
            if (!negative && std::count(entered.begin(), entered.end(), true) == 4 &&
                std::count(exited.begin(), exited.end(), true) == 4)
            { exitRemaining = remaining; Phase("cleanup"); }
            else if (negative && phaseMs >= 30000) Phase("cleanup");
            else if (!remaining) Fail("induce_madness_finished_before_team_returned");
        }
        else if (state == "cleanup")
        {
            if (human) if (auto* instance = human->GetInstanceScript())
                if (auto* sara = instance->GetCreature(760)) sara->AI()->EnterEvadeMode();
            for (std::size_t i = 0; i < actors.size(); ++i) if (auto* p = Actor(i))
            { p->CombatStop(true); GET_PLAYERBOT_AI(p)->SetMaster(nullptr); }
            if (auto* party = sGroupMgr->GetGroupByGUID(group.GetCounter()); party && party->GetGUID() == group) party->Disband();
            for (auto guid : actors) Dojo::LogoutRaidFixture(guid);
            humanGone = Dojo::EndHumanFixture(token);
            sPlayerbotAIConfig.YoggBrainSupport = oldSupport;
            Phase("logout");
        }
        else if (state == "logout")
        {
            bool online = false;
            for (auto guid : actors) online = online || ObjectAccessor::FindConnectedPlayer(guid) != nullptr;
            if (!online && humanGone) { cleanup = true; active = false; Phase("done"); }
            else if (phaseMs >= 60000) { failure += "; fixture_cleanup_failed"; active = false; Phase("cleanup_failed"); }
        }
    }
    json Status()
    {
        std::lock_guard<std::recursive_mutex> lock(mutex);
        return {{"kind", "dojo-yogg-observation"}, {"format", 1}, {"token", token}, {"state", state}, {"failure", failure},
            {"negative", negative}, {"ready", ready}, {"cleanup", cleanup}, {"samples", samples}, {"max_gap_ms", maxGap},
            {"illusion", illusion}, {"portals", portals}, {"initial_sanity_members", initialSanity},
            {"entered", entered}, {"exited", exited}, {"portal_casts", portalCasts}, {"exit_casts", exitCasts},
            {"minimum_sanity", minimumSanity}, {"max_below", maxBelow}, {"max_healers_below", maxHealersBelow},
            {"healer_entrants", std::count(healerEntrants.begin(), healerEntrants.end(), true)},
            {"min_healers_above", minHealersAbove}, {"tank_below_samples", tanksBelow},
            {"influence_deaths", influenceDeaths.size()}, {"brain_hits", brainHits},
            {"brain_start_health", brainStart}, {"brain_least_health", brainLeast}, {"exit_remaining_ms", exitRemaining},
            {"failure_facts", failureFacts}};
    }
    char const* controlStrategy() const
    { return negative ? "-stay,-passive,-follow,-ulduar" : "-stay,-passive,-follow,+ulduar"; }
};
Trial trial;
class Commands : public CommandScript
{
public:
    Commands() : CommandScript("DojoYoggCommands") { }
    ChatCommandTable GetCommands() const override
    {
        static ChatCommandTable commands{{"start", Start, SEC_CONSOLE, Console::Yes},
            {"status", Status, SEC_CONSOLE, Console::Yes}, {"abort", Abort, SEC_CONSOLE, Console::Yes}};
        static ChatCommandTable dojo{{"yogg", commands}};
        return {{"dojo", dojo}};
    }
    static bool Start(ChatHandler* h, Tail args)
    {
        std::istringstream in{std::string(args)};
        std::string token, mode, extra; in >> token >> mode >> extra;
        if (!extra.empty() || (mode != "normal" && mode != "negative")) h->SendSysMessage("dojo yogg: expected token normal|negative");
        else h->SendSysMessage("dojo yogg: " + trial.Start(token, mode == "negative"));
        return true;
    }
    static bool Status(ChatHandler* h) { h->SendSysMessage("dojo-yogg " + trial.Status().dump()); return true; }
    static bool Abort(ChatHandler* h) { if (trial.active) trial.Fail("aborted"); h->SendSysMessage("dojo yogg: cleanup requested"); return true; }
};
class Ticks : public WorldScript
{
public:
    Ticks() : WorldScript("DojoYoggTicks", {WORLDHOOK_ON_UPDATE}) { }
    void OnUpdate(uint32 diff) override { trial.Update(diff); }
};
class Effects : public UnitScript
{
public:
    Effects() : UnitScript("DojoYoggEffects", true, {UNITHOOK_ON_DAMAGE, UNITHOOK_ON_UNIT_DEATH}) { }
    void OnDamage(Unit* attacker, Unit* victim, uint32& damage) override
    {
        std::lock_guard<std::recursive_mutex> lock(trial.mutex);
        if (trial.state != "observe" || !attacker || !victim || !damage || victim->GetGUID() != trial.brainGuid) return;
        auto* player = attacker->GetCharmerOrOwnerPlayerOrPlayerItself();
        if (player && std::find(trial.actors.begin(), trial.actors.end(), player->GetGUID()) != trial.actors.end()) ++trial.brainHits;
    }
    void OnUnitDeath(Unit* unit, Unit* killer) override
    {
        std::lock_guard<std::recursive_mutex> lock(trial.mutex);
        if (trial.state != "observe" || !unit || !killer || !Influence(unit->GetEntry()) || unit->GetPositionZ() >= 300.f) return;
        auto* player = killer->GetCharmerOrOwnerPlayerOrPlayerItself();
        if (player && std::find(trial.actors.begin(), trial.actors.end(), player->GetGUID()) != trial.actors.end()) trial.influenceDeaths.insert(unit->GetGUID());
    }
};
class Casts : public PlayerScript
{
public:
    Casts() : PlayerScript("DojoYoggCasts", {PLAYERHOOK_ON_SPELL_CAST}) { }
    void OnPlayerSpellCast(Player* player, Spell* spell, bool) override
    {
        std::lock_guard<std::recursive_mutex> lock(trial.mutex);
        if ((trial.state != "observe" && trial.state != "portals") || !player || !spell) return;
        auto found = std::find(trial.actors.begin(), trial.actors.end(), player->GetGUID());
        if (found == trial.actors.end()) return;
        auto const i = std::size_t(found - trial.actors.begin());
        uint32 const id = spell->GetSpellInfo()->Id;
        if (id == 63997 || id == 63998 || id == 63989) ++trial.portalCasts[i];
        if (id == 63992) ++trial.exitCasts[i];
    }
};
}
void AddSC_dojo_yogg() { new Commands(); new Ticks(); new Effects(); new Casts(); }
#else
void AddSC_dojo_yogg() { }
#endif
#else
void AddSC_dojo_yogg() { }
#endif
