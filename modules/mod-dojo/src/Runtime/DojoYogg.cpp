// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.
#include "DojoYogg.h"
#if DOJO_TRAINING_BUILD
#if defined(DOJO_OFFLINE_ACCEPTANCE) && DOJO_OFFLINE_ACCEPTANCE
#include "DojoHuman.h"
#include "DojoRaid.h"
#include "DojoRuntime.h"
#include "DojoMotion.h"
#include "AccountMgr.h"
#include "AllMapScript.h"
#include "CharacterCache.h"
#include "Chat.h"
#include "CommandScript.h"
#include "Creature.h"
#include "CreatureAI.h"
#include "Group.h"
#include "GameObject.h"
#include "GroupMgr.h"
#include "InstanceScript.h"
#include "Map.h"
#include "MotionMaster.h"
#include "MoveSpline.h"
#include "ObjectAccessor.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "PlayerbotAIConfig.h"
#include "Playerbots.h"
#include "PlayerScript.h"
#include "Spell.h"
#include "SpellMgr.h"
#include "SpellAuras.h"
#include "SpellAuraEffects.h"
#include "TemporarySummon.h"
#include "UnitScript.h"
#include "Vehicle.h"
#include "WorldScript.h"
#include <algorithm>
#include <array>
#include <cmath>
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
Position SeatPosition(Unit* passenger, Unit* base)
{
    float const angle = base->GetOrientation();
    float const x = base->GetPositionX() + passenger->GetTransOffsetX() * std::cos(angle)
        - passenger->GetTransOffsetY() * std::sin(angle);
    float const y = base->GetPositionY() + passenger->GetTransOffsetY() * std::cos(angle)
        + passenger->GetTransOffsetX() * std::sin(angle);
    return Position(x, y, base->GetPositionZ() + passenger->GetTransOffsetZ());
}
struct Trial
{
    std::recursive_mutex mutex;
    std::array<ObjectGuid, Names.size()> actors{};
    std::array<bool, Names.size()> entered{}, exited{}, healerEntrants{};
    std::array<bool, Names.size()> positioned{}, wasBelow{};
    std::array<Position, Names.size()> previous{};
    std::array<uint32, Names.size()> previousVehicle{}, previousMotion{};
    std::array<float, Names.size()> previousSpeed{}, chargeSpeed{};
    std::array<uint32, Names.size()> chargeMs{};
    uint32 nativeChargeSamples = 0;
    std::array<ObjectGuid, Names.size()> observedConstrictors{};
    std::array<Position, Names.size()> constrictorOrigins{};
    std::array<uint32, Names.size()> constrictorCastMs{};
    struct SeatArrival
    {
        ObjectGuid base;
        Position destination;
        uint32 boardedMs = 0;
    };
    std::array<SeatArrival, Names.size()> constrictorSeats{};
    uint32 constrictorAttachments = 0;
    std::array<bool, Names.size()> previousMoving{};
    // A native grab emits spawn, lunge, squeeze and duplicate ride effects in
    // one update. Keep the spawn evidence until the seat position is sampled.
    std::array<std::array<uint32, 8>, Names.size()> recentSpells{}, recentSpellMs{};
    struct SpellTeleport
    {
        ObjectGuid target;
        Position origin, destination;
        uint32 spell = 0, castMs = 0, teleportMs = 0;
        float blinkRadius = 0.f;
        bool pending = false, observed = false, castAura = false, teleportAura = false;
    };
    std::array<SpellTeleport, Names.size()> spellTeleports{};
    uint32 nativeSpellTeleports = 0;
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
    json CastFacts(std::size_t i) const
    {
        json facts = json::array();
        for (std::size_t n = 0; n < recentSpells[i].size(); ++n)
            if (recentSpells[i][n]) facts.push_back({{"spell", recentSpells[i][n]}, {"age_ms", elapsed - recentSpellMs[i][n]}});
        return facts;
    }
    json ConstrictorFacts(Player* p, std::size_t i) const
    {
        auto* base = p->GetVehicleBase();
        if (!base) return json::object();
        auto* creature = base->ToCreature();
        auto* summon = creature ? creature->ToTempSummon() : nullptr;
        auto const* spell = sSpellMgr->GetSpellInfo(64133);
        return {{"summoned_by_actor", summon && summon->GetSummonerGUID() == p->GetGUID()},
            {"already_observed", observedConstrictors[i] == base->GetGUID()},
            {"lunge_aura", base->HasAura(64123, p->GetGUID())}, {"squeeze_aura", p->HasAura(64125)},
            {"spawn_radius", spell ? spell->Effects[EFFECT_0].CalcRadius(p) : 0.f},
            {"spawn_distance", base->GetExactDist(previous[i])},
            {"spawn_distance_2d", base->GetExactDist2d(previous[i])},
            {"seat_error", p->GetExactDist(SeatPosition(p, base))},
            {"boarding_witness", constrictorSeats[i].base == base->GetGUID()},
            {"boarding_seat_error", p->GetExactDist(constrictorSeats[i].destination)},
            {"base_orientation", base->GetOrientation()},
            {"seat_offset", {p->GetTransOffsetX(), p->GetTransOffsetY(), p->GetTransOffsetZ()}},
            {"base_position", {base->GetPositionX(), base->GetPositionY(), base->GetPositionZ()}}};
    }
    bool NativeConstrictorAttachment(Player* p, std::size_t i, uint32 diff, float speed)
    {
        auto* base = p->GetVehicleBase();
        auto* creature = base ? base->ToCreature() : nullptr;
        auto* summon = creature ? creature->ToTempSummon() : nullptr;
        auto* kit = p->GetVehicle();
        if (!kit || !summon || base->GetEntry() != 33983 || base->GetMap() != p->GetMap()
            || summon->GetSummonerGUID() != p->GetGUID() || observedConstrictors[i] == base->GetGUID()
            || !base->HasAura(64123, p->GetGUID()) || !p->HasAura(64125) || Below(p)) return false;
        bool lunge = false, spawn = false;
        for (std::size_t n = 0; n < recentSpells[i].size(); ++n)
            if (elapsed - recentSpellMs[i][n] <= 250)
            {
                lunge = lunge || recentSpells[i][n] == 64123;
                spawn = spawn || recentSpells[i][n] == 64133;
            }
        auto const* spell = sSpellMgr->GetSpellInfo(64133);
        // TARGET_DEST_CASTER_RADIUS offsets X/Y, then collision adjusts Z.
        // Use the witnessed cast origin, not a previous tick's walking position.
        if (!lunge || !spawn || !spell || elapsed - constrictorCastMs[i] > 250
            || !Dojo::ContinuousMovement(constrictorOrigins[i].GetExactDist(previous[i]), previousSpeed[i], speed, diff)
            || base->GetExactDist2d(constrictorOrigins[i]) >
                std::max(spell->Effects[EFFECT_0].CalcRadius(p), p->GetCombatReach()) + 0.1f)
            return false;
        // Require the occupied seat witnessed after the native ride aura's
        // effects applied. SetInFront can rotate the base without relocating
        // its passenger, so retain the seat destination witnessed when boarding
        // instead of assuming the later base orientation was used for arrival.
        auto const& seat = constrictorSeats[i];
        if (seat.base != base->GetGUID() || elapsed - seat.boardedMs > 250) return false;
        if (p->GetExactDist(SeatPosition(p, base)) > 0.1f && p->GetExactDist(seat.destination) > 0.1f) return false;
        observedConstrictors[i] = base->GetGUID();
        ++constrictorAttachments;
        return true;
    }
    bool NativeSpellTeleport(Player* p, std::size_t i, uint32 diff)
    {
        auto& proof = spellTeleports[i];
        // The last native pulse can finish as the aura expires. Its presence
        // must be witnessed at the cast and teleport, not at a later map sample.
        bool const nativeSpell = (proof.spell == 57840 && proof.castAura && proof.teleportAura)
            || (proof.spell == 1953 && proof.blinkRadius > 0.f);
        if (!proof.observed || !nativeSpell || elapsed - proof.teleportMs > 200
            || p->GetMapId() != 603 || Below(p) != wasBelow[i]
            || proof.origin.GetExactDist(previous[i]) > 1.f + p->GetSpeed(MOVE_RUN) * float(diff) * 0.0015f
            || p->GetExactDist(proof.destination) > 0.1f) return false;
        proof.observed = false;
        ++nativeSpellTeleports;
        return true;
    }
    float MotionSpeed(Player* p, std::size_t i)
    {
        float const running = p->GetSpeed(MOVE_RUN);
        // Only the core's actual charge state and spline may use the speed
        // recorded from its real spell. A recent spell ID alone is insufficient.
        if (chargeSpeed[i] > 0 && elapsed - chargeMs[i] <= 2000 && p->HasUnitState(UNIT_STATE_CHARGING)
            && p->GetMotionMaster()->GetCurrentMovementGeneratorType() == POINT_MOTION_TYPE
            && p->movespline && !p->movespline->Finalized()
            && std::abs(p->movespline->Velocity() - chargeSpeed[i]) <= 0.1f)
        {
            ++nativeChargeSamples;
            return std::max(running, chargeSpeed[i]);
        }
        return running;
    }
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
        previousVehicle = {}; previousMotion = {}; previousMoving = {}; recentSpells = {}; recentSpellMs = {};
        previousSpeed = {}; chargeSpeed = {}; chargeMs = {}; nativeChargeSamples = 0;
        observedConstrictors = {}; constrictorAttachments = 0;
        constrictorOrigins = {}; constrictorCastMs = {}; constrictorSeats = {};
        spellTeleports = {}; nativeSpellTeleports = 0;
        portalCasts = {}; exitCasts = {}; minimumSanity.fill(100); influenceDeaths.clear();
        failureFacts = json::object();
        ready = cleanup = humanGone = false; active = true;
        oldSupport = sPlayerbotAIConfig.YoggBrainSupport;
        sPlayerbotAIConfig.YoggBrainSupport = true;
        for (auto guid : actors) Dojo::LoginRaidFixture(guid);
        Phase("login");
        return "started";
    }
    void WorldUpdate(uint32 diff)
    {
        std::lock_guard<std::recursive_mutex> lock(mutex);
        if (!active) return;
        elapsed += diff;
        if (elapsed > 270000 && state != "cleanup" && state != "logout") Fail("yogg_fixture_deadline");
        if (state == "observe")
        {
            if (auto* p = Actor(0); p && p->GetMapId() == 603) return;
            Fail("yogg_measurement_map_lost");
        }
        Update(diff);
    }
    void MapUpdate(Map* map, uint32 diff)
    {
        std::lock_guard<std::recursive_mutex> lock(mutex);
        auto* p = active && state == "observe" ? Actor(0) : nullptr;
        if (diff && p && p->GetMap() == map) Update(diff);
    }
    void Update(uint32 diff)
    {
        phaseMs += diff;
        auto* human = Dojo::HumanFixture(token);
        // Teleport acknowledgement and normal raid buffs recalculate the reserves
        // assigned at login. This probe starts at a portal wave, so maintain its
        // explicit setup reserves until that boundary; never heal or resurrect a
        // measured actor after observation starts.
        if (state == "phase1" || state == "portals")
            for (std::size_t i = 0; i < actors.size(); ++i)
                if (auto* p = Actor(i); p && p->IsAlive())
                { p->SetMaxHealth(1000000); p->SetFullHealth(); }
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
                {
                    failureFacts = {{"slot", i}, {"present", p != nullptr}, {"recent_casts", CastFacts(i)}};
                    if (p)
                    {
                        failureFacts["alive"] = p->IsAlive(); failureFacts["health"] = p->GetHealth();
                        failureFacts["charmed"] = p->IsCharmed(); failureFacts["map"] = p->GetMapId();
                        failureFacts["instance"] = p->GetInstanceId();
                    }
                    Fail("yogg_actor_dead_lost_or_charmed"); return;
                }
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
                    float const speed = MotionSpeed(p, i);
                    bool const portalTransition = downstairs != wasBelow[i] &&
                        (downstairs ? portalCasts[i] > 0 : exitCasts[i] > 0);
                    if (positioned[i] && !portalTransition &&
                        !Dojo::ContinuousMovement(p->GetExactDist(previous[i]), previousSpeed[i], speed, diff)
                        && !NativeConstrictorAttachment(p, i, diff, speed) && !NativeSpellTeleport(p, i, diff))
                    {
                        failureFacts = {{"slot", i}, {"distance", p->GetExactDist(previous[i])},
                            {"delta_ms", diff}, {"run_speed", p->GetSpeed(MOVE_RUN)},
                            {"motion_speed", speed}, {"previous_speed", previousSpeed[i]},
                            {"spline_speed", p->movespline ? p->movespline->Velocity() : 0.f},
                            {"charging", p->HasUnitState(UNIT_STATE_CHARGING)},
                            {"motion", uint32(p->GetMotionMaster()->GetCurrentMovementGeneratorType())},
                            {"previous_motion", previousMotion[i]}, {"moving", p->isMoving()},
                            {"charge_expected_speed", chargeSpeed[i]}, {"charge_age_ms", elapsed - chargeMs[i]},
                            {"previous_moving", previousMoving[i]}, {"previous_vehicle_entry", previousVehicle[i]},
                            {"constrictor", ConstrictorFacts(p, i)},
                            {"spell_teleport", {{"spell", spellTeleports[i].spell},
                                {"blink_radius", spellTeleports[i].blinkRadius}, {"observed", spellTeleports[i].observed},
                                {"pending", spellTeleports[i].pending}, {"cast_aura", spellTeleports[i].castAura},
                                {"teleport_aura", spellTeleports[i].teleportAura}, {"current_aura", p->HasAura(51690)},
                                {"cast_age_ms", elapsed - spellTeleports[i].castMs},
                                {"teleport_age_ms", elapsed - spellTeleports[i].teleportMs},
                                {"origin_gap", spellTeleports[i].origin.GetExactDist(previous[i])},
                                {"arrival_gap", p->GetExactDist(spellTeleports[i].destination)}}},
                            {"vehicle_entry", p->GetVehicleBase() ? p->GetVehicleBase()->GetEntry() : 0u},
                            {"recent_casts", CastFacts(i)},
                            {"from", {previous[i].GetPositionX(), previous[i].GetPositionY(), previous[i].GetPositionZ()}},
                            {"to", {p->GetPositionX(), p->GetPositionY(), p->GetPositionZ()}},
                            {"downstairs", downstairs}, {"portal_casts", portalCasts[i]}, {"exit_casts", exitCasts[i]}};
                        Fail("measured_yogg_actor_jumped"); return;
                    }
                    previous[i] = p->GetPosition(); positioned[i] = true; wasBelow[i] = downstairs;
                    previousSpeed[i] = speed;
                    previousVehicle[i] = p->GetVehicleBase() ? p->GetVehicleBase()->GetEntry() : 0u;
                    previousMotion[i] = uint32(p->GetMotionMaster()->GetCurrentMovementGeneratorType());
                    previousMoving[i] = p->isMoving();
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
        json bodies = json::array(), room = json::array();
        Player* roomActor = nullptr;
        if (state == "observe")
            for (std::size_t i = 0; i < actors.size(); ++i)
                if (auto* p = Actor(i))
                {
                    auto* target = GET_PLAYERBOT_AI(p)->GetAiObjectContext()->GetValue<Unit*>("current target")->Get();
                    auto* exit = Below(p) ? p->FindNearestGameObject(194625, 250.f) : nullptr;
                    bodies.push_back({{"slot", i}, {"x", p->GetPositionX()}, {"y", p->GetPositionY()},
                        {"z", p->GetPositionZ()}, {"facing", p->GetOrientation()}, {"moving", p->isMoving()},
                        {"target_entry", target ? target->GetEntry() : 0u},
                        {"exit_distance", exit ? p->GetExactDist(exit) : -1.f}, {"recent_casts", CastFacts(i)}});
                    if (!roomActor && Below(p)) roomActor = p;
                }
        if (roomActor)
            for (uint32 entry : {33943u, 33716u, 33717u, 33718u, 33719u, 33720u, 33567u, 33433u, 33990u})
            {
                std::list<Creature*> nearby;
                roomActor->GetCreatureListWithEntryInGrid(nearby, entry, 180.f);
                for (auto* creature : nearby)
                    if (room.size() < 64 && creature->GetPositionZ() < 300.f)
                        room.push_back({{"entry", entry}, {"guid", creature->GetGUID().GetCounter()},
                            {"x", creature->GetPositionX()}, {"y", creature->GetPositionY()}, {"z", creature->GetPositionZ()},
                            {"alive", creature->IsAlive()}, {"health", creature->GetHealth()}, {"gaze", creature->HasAura(64167)}});
            }
        return {{"kind", "dojo-yogg-observation"}, {"format", 1}, {"token", token}, {"state", state}, {"failure", failure},
            {"observation_clock", "map_update"}, {"native_charge_samples", nativeChargeSamples},
            {"negative", negative}, {"ready", ready}, {"cleanup", cleanup}, {"samples", samples}, {"max_gap_ms", maxGap},
            {"illusion", illusion}, {"portals", portals}, {"initial_sanity_members", initialSanity},
            {"entered", entered}, {"exited", exited}, {"portal_casts", portalCasts}, {"exit_casts", exitCasts},
            {"minimum_sanity", minimumSanity}, {"max_below", maxBelow}, {"max_healers_below", maxHealersBelow},
            {"healer_entrants", std::count(healerEntrants.begin(), healerEntrants.end(), true)},
            {"min_healers_above", minHealersAbove}, {"tank_below_samples", tanksBelow},
            {"influence_deaths", influenceDeaths.size()}, {"brain_hits", brainHits},
            {"brain_start_health", brainStart}, {"brain_least_health", brainLeast}, {"exit_remaining_ms", exitRemaining},
            {"failure_facts", failureFacts}, {"native_constrictor_attachments", constrictorAttachments},
            {"native_spell_teleports", nativeSpellTeleports}, {"bodies", bodies}, {"room", room}};
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
    void OnUpdate(uint32 diff) override { trial.WorldUpdate(diff); }
};
class MapTicks : public AllMapScript
{
public:
    MapTicks() : AllMapScript("DojoYoggMapTicks", {ALLMAPHOOK_ON_MAP_UPDATE}) { }
    void OnMapUpdate(Map* map, uint32 diff) override { trial.MapUpdate(map, diff); }
};
class Effects : public UnitScript
{
public:
    Effects() : UnitScript("DojoYoggEffects", true, {UNITHOOK_ON_DAMAGE, UNITHOOK_ON_UNIT_DEATH, UNITHOOK_ON_AURA_APPLY}) { }
    void OnAuraApply(Unit* unit, Aura* aura) override
    {
        if (!unit || unit->GetEntry() != 33983 || !aura || aura->GetId() != 64123) return;
        std::lock_guard<std::recursive_mutex> lock(trial.mutex);
        if (trial.state != "observe" && trial.state != "portals") return;
        // Unit::_ApplyAura calls this after all aura effects, including
        // HandleAuraControlVehicle -> _EnterVehicle -> AddPassenger. A cast
        // callback is too early when the lunge has a missile travel delay.
        auto* caster = aura->GetCaster();
        auto* player = caster ? caster->ToPlayer() : nullptr;
        auto* creature = unit->ToCreature();
        auto* summon = creature ? creature->ToTempSummon() : nullptr;
        auto* kit = player ? player->GetVehicle() : nullptr;
        if (!kit || !summon || kit->GetBase() != unit || unit->GetMap() != player->GetMap()
            || summon->GetSummonerGUID() != player->GetGUID()
            || kit->GetPassenger(player->GetTransSeat()) != player) return;
        auto found = std::find(trial.actors.begin(), trial.actors.end(), player->GetGUID());
        if (found == trial.actors.end()) return;
        auto& seat = trial.constrictorSeats[std::size_t(found - trial.actors.begin())];
        if (seat.base != unit->GetGUID())
            seat = {unit->GetGUID(), SeatPosition(player, unit), trial.elapsed};
    }
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
    Casts() : PlayerScript("DojoYoggCasts", {PLAYERHOOK_ON_SPELL_CAST, PLAYERHOOK_ON_BEFORE_TELEPORT}) { }
    void OnPlayerSpellCast(Player* player, Spell* spell, bool) override
    {
        std::lock_guard<std::recursive_mutex> lock(trial.mutex);
        if ((trial.state != "observe" && trial.state != "portals") || !player || !spell) return;
        auto found = std::find(trial.actors.begin(), trial.actors.end(), player->GetGUID());
        if (found == trial.actors.end()) return;
        auto const i = std::size_t(found - trial.actors.begin());
        uint32 const id = spell->GetSpellInfo()->Id;
        for (std::size_t n = trial.recentSpells[i].size() - 1; n > 0; --n)
        {
            trial.recentSpells[i][n] = trial.recentSpells[i][n - 1];
            trial.recentSpellMs[i][n] = trial.recentSpellMs[i][n - 1];
        }
        trial.recentSpells[i][0] = id; trial.recentSpellMs[i][0] = trial.elapsed;
        if (id == 63997 || id == 63998 || id == 63989) ++trial.portalCasts[i];
        if (id == 63992) ++trial.exitCasts[i];
        if (id == 64133)
        {
            trial.constrictorOrigins[i] = player->GetPosition();
            trial.constrictorCastMs[i] = trial.elapsed;
        }
        if (spell->GetSpellInfo()->HasEffect(SPELL_EFFECT_CHARGE))
        {
            auto* target = spell->m_targets.GetUnitTarget();
            if (target && target->GetMap() == player->GetMap() && player->IsValidAttackTarget(target))
            {
                float const speed = spell->GetSpellInfo()->Speed;
                bool const flying = player->HasUnitMovementFlag(
                    MovementFlags(MOVEMENTFLAG_CAN_FLY | MOVEMENTFLAG_DISABLE_GRAVITY));
                trial.chargeSpeed[i] = flying ? 0.f :
                    Dojo::GroundChargeSpeed(speed > 0.f ? speed : SPEED_CHARGE, player->GetSpeed(MOVE_RUN));
                trial.chargeMs[i] = trial.elapsed;
            }
        }
        if (id == 57840 && player->HasAura(51690))
        {
            auto* target = spell->m_targets.GetUnitTarget();
            if (target && target->GetMap() == player->GetMap() && player->IsValidAttackTarget(target))
            {
                auto& proof = trial.spellTeleports[i];
                proof = {};
                proof.spell = id;
                proof.target = target->GetGUID(); proof.origin = player->GetPosition();
                proof.castMs = trial.elapsed; proof.pending = true; proof.castAura = true;
            }
        }
        if (id == 1953 && spell->GetSpellInfo()->Effects[EFFECT_0].Effect == SPELL_EFFECT_LEAP)
        {
            auto& proof = trial.spellTeleports[i];
            proof = {};
            proof.spell = id; proof.origin = player->GetPosition();
            proof.blinkRadius = spell->GetSpellInfo()->Effects[EFFECT_0].CalcRadius(player);
            proof.castMs = trial.elapsed; proof.pending = true;
        }
    }
    bool OnPlayerBeforeTeleport(Player* player, uint32 map, float x, float y, float z,
        float orientation, uint32 options, Unit*) override
    {
        std::lock_guard<std::recursive_mutex> lock(trial.mutex);
        if (trial.state != "observe" || !player || map != 603 || player->GetMapId() != map
            || !(options & TELE_TO_SPELL)) return true;
        auto found = std::find(trial.actors.begin(), trial.actors.end(), player->GetGUID());
        if (found == trial.actors.end()) return true;
        auto& proof = trial.spellTeleports[std::size_t(found - trial.actors.begin())];
        if (!proof.pending || proof.castMs != trial.elapsed) return true;
        proof.pending = false;
        if (proof.spell == 57840)
        {
            auto* target = ObjectAccessor::GetUnit(*player, proof.target);
            if (!player->HasAura(51690) || !target || target->GetMap() != player->GetMap()
                || !player->IsValidAttackTarget(target)) return true;
            proof.teleportAura = true;
        }
        else if (proof.spell == 1953)
        {
            if (!Dojo::BlinkDestination(x - proof.origin.GetPositionX(), y - proof.origin.GetPositionY(),
                proof.origin.GetOrientation(), proof.blinkRadius)) return true;
        }
        else return true;
        // Witness the core's actual destination, then require the measured actor
        // to arrive there. A recent spell ID alone never permits a displacement.
        proof.destination = Position(x, y, z, orientation);
        proof.teleportMs = trial.elapsed; proof.observed = true;
        return true;
    }
};
}
void AddSC_dojo_yogg() { new Commands(); new Ticks(); new MapTicks(); new Effects(); new Casts(); }
#else
void AddSC_dojo_yogg() { }
#endif
#else
void AddSC_dojo_yogg() { }
#endif
