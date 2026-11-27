// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.
#include "DojoVehicle.h"
#if DOJO_TRAINING_BUILD
#if defined(DOJO_OFFLINE_ACCEPTANCE) && DOJO_OFFLINE_ACCEPTANCE
#include "DojoHuman.h"
#include "DojoMotion.h"
#include "DojoRaid.h"
#include "DojoRuntime.h"
#include "AccountMgr.h"
#include "AllMapScript.h"
#include "AllSpellScript.h"
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
#include "MoveSpline.h"
#include "ObjectAccessor.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "PlayerbotAIConfig.h"
#include "Playerbots.h"
#include "Spell.h"
#include "SpellMgr.h"
#include "UnitScript.h"
#include "Vehicle.h"
#include "WorldScript.h"
#include <algorithm>
#include <array>
#include <cmath>
#include <list>
#include <mutex>
#include <sstream>
#include <vector>

using nlohmann::json;
using namespace Acore::ChatCommands;
namespace
{
constexpr std::array<char const*, 2> Names{"Dojotank", "Dojodpsa"};
Position const StartPoint(119.8f, 38.37f, 409.803f, 0.f);
std::vector<ObjectGuid> Accessories(Creature* boss)
{
    std::vector<ObjectGuid> result;
    std::vector<Creature*> pending{boss};
    while (!pending.empty())
    {
        auto* unit = pending.back(); pending.pop_back();
        if (auto* kit = unit ? unit->GetVehicleKit() : nullptr)
            for (auto const& seat : kit->Seats)
                if (auto* accessory = ObjectAccessor::GetCreature(*unit, seat.second.Passenger.Guid))
                {
                    if (result.size() >= 64 || std::find(result.begin(), result.end(), accessory->GetGUID()) != result.end())
                        return {}; // Reject an incomplete or cyclic census.
                    result.push_back(accessory->GetGUID()); pending.push_back(accessory);
                }
    }
    return result;
}
struct Trial
{
    std::recursive_mutex mutex;
    std::string token, scenario, state = "idle", failure;
    std::array<ObjectGuid, 2> actors{}, mounts{};
    std::array<Position, 2> initial{}, previous{};
    std::array<float, 2> travelled{};
    std::array<float, 2> previousSpeed{};
    std::array<uint32, 2> hits{}, movingHits{}, afterDeathHits{}, seats{};
    std::array<uint32, 2> surgeSamples{}, shieldSamples{}, ringSamples{};
    std::array<uint32, 2> fieldMs{}, longestFieldMs{}, fieldEscapes{};
    uint32 setupKills = 0, healEvents = 0, dotSamples = 0, fieldSamples = 0;
    uint64 effectiveHealing = 0;
    ObjectGuid group, bossGuid;
    uint32 elapsed = 0, phaseMs = 0, samples = 0, maxGap = 0, deathAt = 0;
    uint32 startingBossHealth = 0, leastBossHealth = 0;
    json lastBodies = json::array(), lastBoss = json::object(), recentEffects = json::array();
    json lastFields = json::array();
    json recentCasts = json::array();
    json failureFacts = json::object();
    bool active = false, negative = false, ready = false, humanBoarded = false, cleanup = false, humanGone = false;
    bool oldSupport = false;
    std::vector<ObjectGuid> oldAccessories;
    uint32 removedAccessories = 0, freshAccessories = 0;
    bool resetRequested = false, resetReady = false, resetReengaged = false;

    Player* Actor(std::size_t i) const
    {
        auto* p = ObjectAccessor::FindConnectedPlayer(actors[i]);
        return p && p->IsInWorld() && GET_PLAYERBOT_AI(p) ? p : nullptr;
    }
    Creature* Boss() const
    {
        auto* p = Actor(0);
        return p ? p->GetMap()->GetCreature(bossGuid) : nullptr;
    }
    void Phase(std::string const& next) { state = next; phaseMs = 0; }
    void RecordEffect(char const* kind, Unit* source, Unit* receiver, uint32 amount)
    {
        if ((state != "observe" && state != "drakes") || !receiver) return;
        auto identity = [this](Unit* unit)
        {
            json result{{"entry", unit ? unit->GetEntry() : 0}};
            if (!unit) return result;
            result["boss"] = unit->GetGUID() == bossGuid;
            for (std::size_t i = 0; i < actors.size(); ++i)
            {
                if (unit->GetGUID() == actors[i]) result["actor_slot"] = i;
                if (unit->GetGUID() == mounts[i]) result["mount_slot"] = i;
            }
            return result;
        };
        json from = identity(source), to = identity(receiver);
        bool const tracked = from.contains("actor_slot") || from.contains("mount_slot") ||
            to.contains("actor_slot") || to.contains("mount_slot");
        if (!tracked) return;
        if (recentEffects.size() == 32) recentEffects.erase(recentEffects.begin());
        recentEffects.push_back({{"kind", kind}, {"elapsed_ms", elapsed}, {"phase_ms", phaseMs},
            {"source", std::move(from)}, {"receiver", std::move(to)}, {"requested_amount", amount},
            {"receiver_health", receiver->GetHealth()}, {"receiver_max_health", receiver->GetMaxHealth()},
            {"surge", receiver->HasAura(57407) || receiver->HasAura(60936)},
            {"shield", receiver->HasAura(57108)}, {"revivify", receiver->HasAura(57090)}});
    }
    void Capture()
    {
        lastBodies = json::array();
        for (std::size_t i = 0; i < actors.size(); ++i)
            if (auto* p = Actor(i))
            {
                auto* ai = GET_PLAYERBOT_AI(p);
                auto* base = p->GetVehicleBase();
                auto* target = ai->GetAiObjectContext()->GetValue<Unit*>("current target")->Get();
                json body{{"slot", i}, {"alive", p->IsAlive()}, {"health", p->GetHealth()},
                    {"combat", p->IsInCombat()}, {"target_entry", target ? target->GetEntry() : 0},
                    {"x", p->GetPositionX()}, {"y", p->GetPositionY()}, {"z", p->GetPositionZ()},
                    {"combat_strategy", ai->HasStrategy(scenario == "malygos" ? "wotlk-eoe" : "ulduar", BOT_STATE_COMBAT)},
                    {"idle_strategy", ai->HasStrategy(scenario == "malygos" ? "wotlk-eoe" : "ulduar", BOT_STATE_NON_COMBAT)}};
                if (base)
                {
                    float goalX = 0, goalY = 0, goalZ = 0;
                    bool const hasGoal = base->GetMotionMaster()->GetDestination(goalX, goalY, goalZ);
                    bool const splineReady = base->movespline->Initialized();
                    auto* speed = base->GetAura(57092);
                    body["vehicle"] = {{"entry", base->GetEntry()}, {"alive", base->IsAlive()},
                    {"x", base->GetPositionX()}, {"y", base->GetPositionY()}, {"z", base->GetPositionZ()},
                    {"health", base->GetHealth()}, {"max_health", base->GetMaxHealth()},
                    {"energy", base->GetPower(POWER_ENERGY)}, {"combo_points", base->GetComboPoints()},
                    {"combat", base->IsInCombat()}, {"moving", base->isMoving()},
                    {"flight_speed", base->GetSpeed(MOVE_FLIGHT)}, {"run_speed", base->GetSpeed(MOVE_RUN)},
                    {"motion", uint32(base->GetMotionMaster()->GetCurrentMovementGeneratorType())},
                    {"has_goal", hasGoal}, {"goal", {goalX, goalY, goalZ}},
                    {"spline_initialized", splineReady},
                    {"spline_velocity", base->movespline->Velocity()},
                    {"spline_time_ms", base->movespline->timePassed()},
                    {"spline_duration_ms", splineReady ? base->movespline->Duration() : 0},
                    {"speed_aura_ms", speed ? speed->GetDuration() : 0},
                    {"speed_cooldown", base->HasSpellCooldown(57092)}};
                }
                lastBodies.push_back(std::move(body));
            }
        lastFields = json::array();
        if (auto* boss = Boss())
        {
            lastBoss = {{"health", boss->GetHealth()}, {"combat", boss->IsInCombat()},
            {"x", boss->GetPositionX()}, {"y", boss->GetPositionY()}, {"z", boss->GetPositionZ()},
            {"boss_state", uint32(boss->GetInstanceScript()->GetBossState(0))},
            {"non_attackable", boss->HasUnitFlag(UNIT_FLAG_NON_ATTACKABLE)}, {"immune_pc", boss->IsImmuneToPC()}};
            if (scenario == "malygos")
            {
                std::list<Creature*> fields;
                boss->GetCreatureListWithEntryInGrid(fields, 30592, 200.f);
                auto const* damage = sSpellMgr->GetSpellInfo(57429);
                for (auto* field : fields)
                    if (field && lastFields.size() < 32)
                        lastFields.push_back({{"guid", field->GetGUID().GetCounter()},
                            {"x", field->GetPositionX()}, {"y", field->GetPositionY()}, {"z", field->GetPositionZ()},
                            {"radius", damage ? damage->Effects[EFFECT_0].CalcRadius(field) : 0.f}});
            }
        }
    }
    void Fail(std::string const& why) { Capture(); failure = why; Phase("cleanup"); }
    std::string Start(std::string const& encounter, std::string const& id, bool control)
    {
        std::lock_guard<std::recursive_mutex> lock(mutex);
        if (active || state == "cleanup_failed") return "fixture_busy";
        if (Dojo::State().plan.state != Dojo::RuntimeState::Armed || Dojo::State().manifest) return "fixture_not_armed";
        if (encounter != "leviathan" && encounter != "leviathan-reset" && encounter != "malygos") return "unknown_vehicle_scenario";
        if (id.empty() || id.size() > 64 || id.find_first_not_of("abcdefghijklmnopqrstuvwxyz0123456789-") != std::string::npos)
            return "invalid_token";
        uint32 const account = AccountMgr::GetId("RNDBOTDOJO");
        for (std::size_t i = 0; i < actors.size(); ++i)
        {
            actors[i] = sCharacterCache->GetCharacterGuidByName(encounter == "malygos" && i == 0 ? "Dojoheala" : Names[i]);
            if (!account || !actors[i] || sCharacterCache->GetCharacterAccountIdByGuid(actors[i]) != account)
                return "fixture_account_mismatch";
            if (ObjectAccessor::FindConnectedPlayer(actors[i])) return "fixture_already_online";
        }
        auto why = Dojo::BeginHumanFixture(id);
        if (!why.empty()) return why;
        token = id; scenario = encounter; negative = control; failure.clear(); group.Clear(); bossGuid.Clear(); mounts = {};
        failureFacts = json::object();
        lastBodies = json::array(); lastBoss = json::object(); recentEffects = json::array(); recentCasts = json::array();
        lastFields = json::array();
        elapsed = samples = maxGap = deathAt = startingBossHealth = leastBossHealth = 0;
        travelled = {}; hits = {}; movingHits = {}; afterDeathHits = {}; seats = {};
        surgeSamples = {}; shieldSamples = {}; ringSamples = {};
        fieldMs = {}; longestFieldMs = {}; fieldEscapes = {};
        setupKills = healEvents = dotSamples = fieldSamples = 0; effectiveHealing = 0;
        oldAccessories.clear(); removedAccessories = freshAccessories = 0;
        resetRequested = resetReady = resetReengaged = false;
        ready = humanBoarded = cleanup = humanGone = false; active = true;
        oldSupport = sPlayerbotAIConfig.RaidVehicleSupport;
        sPlayerbotAIConfig.RaidVehicleSupport = true;
        for (auto guid : actors) Dojo::LoginRaidFixture(guid);
        Phase("login");
        return "started";
    }
    bool Measuring() const { return state == "boarding" || state == "observe" || state == "drakes" || state == "reset"; }
    bool MapPhase() const { return Measuring() || state == "vehicle_settle" || state == "human_board"; }
    void WorldUpdate(uint32 diff)
    {
        std::lock_guard<std::recursive_mutex> lock(mutex);
        if (!active) return;
        elapsed += diff;
        if (elapsed > 300000 && state != "cleanup" && state != "logout") Fail("vehicle_fixture_deadline");
        if (MapPhase())
        {
            auto* human = Dojo::HumanFixture(token);
            if (human && human->GetMap() && human->GetMapId() == (scenario == "malygos" ? 616u : 603u)) return;
            Fail("vehicle_measurement_map_lost");
        }
        Update(diff);
    }
    void MapUpdate(Map* map, uint32 diff)
    {
        std::lock_guard<std::recursive_mutex> lock(mutex);
        auto* human = active && MapPhase() ? Dojo::HumanFixture(token) : nullptr;
        // The playerbots core batches raid-map physics across world ticks.
        // Observe after that map's actual update with the same elapsed time
        // that advanced its splines, never a shorter unrelated world tick.
        if (diff && human && human->GetMap() == map) Update(diff);
    }
    void Update(uint32 diff)
    {
        phaseMs += diff;
        auto* human = Dojo::HumanFixture(token);
        auto* tank = Actor(0);
        auto* ranged = Actor(1);
        if (state == "login")
        {
            auto why = Dojo::HumanFixtureError(token);
            if (!why.empty()) { Fail(why); return; }
            if (!human || !tank || !ranged) return;
            for (auto* p : {human, tank, ranged})
                if (p->GetGroup() || p->GetLevel() != 80 || !p->IsAlive() || p->GetVehicle())
                { Fail("unclean_vehicle_fixture"); return; }
            auto* party = new Group();
            if (!party->Create(human)) { delete party; Fail("fixture_group_creation"); return; }
            sGroupMgr->AddGroup(party); party->ConvertToRaid(); group = party->GetGUID();
            for (auto* p : {tank, ranged})
            {
                if (!party->AddMember(p)) { Fail("fixture_group_join"); return; }
                auto* ai = GET_PLAYERBOT_AI(p);
                ai->SetMaster(human);
                ai->ChangeStrategy("-stay,-passive,+follow", BOT_STATE_NON_COMBAT);
                ai->ChangeStrategy("-stay,-passive", BOT_STATE_COMBAT);
            }
            for (auto* p : {human, tank, ranged})
            {
                p->SetRaidDifficulty(RAID_DIFFICULTY_10MAN_NORMAL);
                Position const point = scenario == "malygos" ? Position(754.395f, 1301.27f, 266.10f, 0.f) : StartPoint;
                if (!p->TeleportTo(scenario == "malygos" ? 616 : 603, point.GetPositionX(), point.GetPositionY(), point.GetPositionZ(), 0.f))
                { Fail("fixture_teleport_refused"); return; }
            }
            Phase("arrive");
        }
        else if (state == "arrive")
        {
            if (!human || !tank || !ranged || human->IsBeingTeleported() || tank->IsBeingTeleported() || ranged->IsBeingTeleported()) return;
            if (human->GetMapId() != (scenario == "malygos" ? 616u : 603u) || tank->GetMap() != human->GetMap() || ranged->GetMap() != human->GetMap())
            { Fail("fixture_destination_mismatch"); return; }
            auto* instance = human->GetInstanceScript();
            auto* boss = human->FindNearestCreature(scenario == "malygos" ? 28859 : 33113, 500.f, true);
            if (!instance || !boss) { Fail("native_vehicle_boss_missing"); return; }
            bossGuid = boss->GetGUID();
            if (scenario == "malygos")
            {
                // Native intro, adds, platform and drake summons run in the core.
                // The measured actors retain ordinary AI throughout setup too.
                for (auto* p : {tank, ranged})
                {
                    auto* ai = GET_PLAYERBOT_AI(p);
                    ai->SetMaster(human);
                    ai->ChangeStrategy(negative ? "-wotlk-eoe" : "+wotlk-eoe", BOT_STATE_COMBAT);
                    ai->ChangeStrategy(negative ? "-wotlk-eoe" : "+wotlk-eoe", BOT_STATE_NON_COMBAT);
                }
                for (auto* p : {human, tank, ranged}) { p->SetMaxHealth(1000000); p->SetFullHealth(); }
                instance->ProcessEvent(human, 20711); // Actual focusing-iris event.
                Phase("malygos_phase1");
                return;
            }
            // The native reset after the opening gauntlet creates the actual
            // vehicle kits, accessories and spell-clicks at Formation Grounds.
            // This probe starts at that phase; it does not certify the gauntlet.
            instance->SetBossState(0, SPECIAL);
            boss->AI()->Reset();
            for (auto* p : {tank, ranged})
            {
                auto* ai = GET_PLAYERBOT_AI(p);
                ai->SetMaster(human);
                bool const disabled = negative && scenario != "leviathan-reset";
                ai->ChangeStrategy(disabled ? "-ulduar" : "+ulduar", BOT_STATE_COMBAT);
                ai->ChangeStrategy(disabled ? "-ulduar" : "+ulduar", BOT_STATE_NON_COMBAT);
            }
            Phase("vehicle_settle");
        }
        else if (state == "malygos_phase1")
        {
            auto* boss = Boss();
            if (!boss || !human || !human->IsAlive()) { Fail("malygos_phase_setup_lost"); return; }
            if (!boss->IsInCombat() || boss->HasUnitFlag(UNIT_FLAG_NON_ATTACKABLE)) return;
            // Select the phase boundary, then deliver real damage so the core's
            // DamageTaken schedules phase two. No measured combat has begun.
            boss->SetHealth(boss->GetMaxHealth() * 49 / 100);
            Unit::DealDamage(human, boss, 1);
            Phase("malygos_phase2");
        }
        else if (state == "malygos_phase2")
        {
            auto* boss = Boss();
            if (!boss || !human || !tank || !ranged || !human->IsAlive()) { Fail("malygos_phase_setup_lost"); return; }
            for (uint32 entry : {30245u, 30249u})
            {
                std::list<Creature*> adds;
                human->GetCreatureListWithEntryInGrid(adds, entry, 300.f);
                for (auto* add : adds) if (add && add->IsAlive()) { Unit::Kill(human, add); ++setupKills; }
            }
            if (setupKills < 6 || !boss->HasUnitFlag(UNIT_FLAG_DISABLE_MOVE) || boss->HasUnitFlag(UNIT_FLAG_NON_ATTACKABLE)) return;
            for (std::size_t i = 0; i < actors.size(); ++i)
            {
                auto* p = Actor(i);
                auto* base = p->GetVehicleBase();
                if (!base || base->GetEntry() != 30161 || !p->GetVehicle() || p->GetVehicle()->GetPassenger(0) != p) return;
            }
            if (!human->GetVehicleBase() || human->GetVehicleBase()->GetEntry() != 30161) return;
            humanBoarded = true;
            for (std::size_t i = 0; i < actors.size(); ++i)
            {
                auto* p = Actor(i);
                auto* ai = GET_PLAYERBOT_AI(p);
                auto* base = p->GetVehicleBase();
                mounts[i] = base->GetGUID(); seats[i] = base->GetEntry(); initial[i] = previous[i] = base->GetPosition();
                previousSpeed[i] = base->GetSpeed(MOVE_FLIGHT);
                ai->SetMaster(human);
                ai->ChangeStrategy(negative ? "-wotlk-eoe" : "+wotlk-eoe", BOT_STATE_COMBAT);
                ai->ChangeStrategy(negative ? "-wotlk-eoe" : "+wotlk-eoe", BOT_STATE_NON_COMBAT);
            }
            startingBossHealth = leastBossHealth = boss->GetHealth();
            ready = true; Phase("drakes");
        }
        else if (state == "drakes")
        {
            auto* boss = Boss();
            if (!human || !boss || !boss->IsAlive() || !boss->IsInCombat()) { Fail("drake_encounter_lost"); return; }
            ++samples; maxGap = std::max(maxGap, diff);
            leastBossHealth = std::min(leastBossHealth, boss->GetHealth());
            std::list<Creature*> fields;
            boss->GetCreatureListWithEntryInGrid(fields, 30592, 200.f);
            if (!fields.empty()) ++fieldSamples;
            auto const* fieldDamage = sSpellMgr->GetSpellInfo(57429);
            if (!fieldDamage) { Fail("native_static_field_damage_missing"); return; }
            for (std::size_t i = 0; i < actors.size(); ++i)
            {
                auto* p = Actor(i);
                auto* base = p ? p->GetVehicleBase() : nullptr;
                if (!p || !p->IsAlive() || !base || !base->IsAlive() || base->GetGUID() != mounts[i] ||
                    !p->GetVehicle() || p->GetVehicle()->GetPassenger(0) != p || base->GetMap() != boss->GetMap())
                { Fail("drake_driver_or_mount_lost"); return; }
                if (!Dojo::ContinuousMovement(base->GetExactDist(previous[i]), previousSpeed[i],
                    base->GetSpeed(MOVE_FLIGHT), diff))
                {
                    failureFacts = {{"slot", i}, {"diff_ms", diff}, {"phase_ms", phaseMs},
                        {"previous", {previous[i].GetPositionX(), previous[i].GetPositionY(), previous[i].GetPositionZ()}},
                        {"current", {base->GetPositionX(), base->GetPositionY(), base->GetPositionZ()}},
                        {"distance", base->GetExactDist(previous[i])}, {"flight_speed", base->GetSpeed(MOVE_FLIGHT)},
                        {"previous_speed", previousSpeed[i]},
                        {"run_speed", base->GetSpeed(MOVE_RUN)}, {"moving", base->isMoving()},
                        {"motion", uint32(base->GetMotionMaster()->GetCurrentMovementGeneratorType())},
                        {"spline_velocity", base->movespline->Velocity()},
                        {"spline_time_ms", base->movespline->timePassed()}, {"spline_duration_ms", base->movespline->Duration()}};
                    Fail("measured_drake_jumped"); return;
                }
                previous[i] = base->GetPosition();
                previousSpeed[i] = base->GetSpeed(MOVE_FLIGHT);
                travelled[i] = std::max(travelled[i], base->GetExactDist(initial[i]));
                bool const surge = base->HasAura(57407) || base->HasAura(60936);
                if (surge) ++surgeSamples[i];
                if (surge && base->HasAura(57108)) ++shieldSamples[i];
                float const radius = base->GetExactDist2d(boss);
                bool const fieldSafe = std::none_of(fields.begin(), fields.end(), [base, fieldDamage](Creature* field)
                { return !field || base->GetExactDist(field) < fieldDamage->Effects[EFFECT_0].CalcRadius(field) + 2.f; });
                if (!fieldSafe)
                {
                    fieldMs[i] += diff;
                    longestFieldMs[i] = std::max(longestFieldMs[i], fieldMs[i]);
                }
                else if (fieldMs[i])
                {
                    ++fieldEscapes[i]; fieldMs[i] = 0;
                }
                if (radius >= 38.f && radius <= 55.f && std::abs(base->GetPositionZ() - boss->GetPositionZ()) <= 10.f && fieldSafe)
                    ++ringSamples[i];
            }
            if (boss->HasAura(56092, mounts[1])) ++dotSamples;
            if (phaseMs >= 90000) Phase("cleanup");
        }
        else if (state == "vehicle_settle")
        {
            // Reset queues the retired vehicle set for map removal. Let that
            // map finish a full update before clicking its replacement set;
            // world ticks can otherwise board a vehicle awaiting removal.
            Phase("human_board");
        }
        else if (state == "human_board")
        {
            if (!human || !tank || !ranged) { Fail("vehicle_actor_lost"); return; }
            auto* siege = human->FindNearestCreature(33060, 10.f, true);
            if (!siege || !siege->GetVehicleKit()) return;
            siege->HandleSpellClick(human);
            humanBoarded = human->GetVehicleBase() == siege && siege->GetVehicleKit()->GetPassenger(0) == human;
            if (!humanBoarded) return;
            ready = true;
            Phase("boarding");
        }
        else if (state == "boarding")
        {
            ++samples; maxGap = std::max(maxGap, diff);
            if (!human || !human->IsAlive() || !human->GetVehicle() || !tank || !ranged)
            {
                failureFacts = {{"human_present", human != nullptr}, {"human_alive", human && human->IsAlive()},
                    {"human_health", human ? human->GetHealth() : 0u},
                    {"human_vehicle", human && human->GetVehicleBase() ? human->GetVehicleBase()->GetEntry() : 0u},
                    {"tank_present", tank != nullptr}, {"ranged_present", ranged != nullptr}};
                Fail("boarding_stimulus_lost"); return;
            }
            for (std::size_t i = 0; i < actors.size(); ++i)
            {
                auto* p = Actor(i);
                auto* base = p->GetVehicleBase();
                if (base && p->GetVehicle() && p->GetVehicle()->GetPassenger(0) == p)
                { seats[i] = base->GetEntry(); mounts[i] = base->GetGUID(); }
            }
            if (negative && scenario != "leviathan-reset")
            {
                if (tank->GetVehicle() || ranged->GetVehicle()) { Fail("control_boarded_without_strategy"); return; }
                if (phaseMs >= 30000) Phase("cleanup");
            }
            else if (seats[0] == 33060 && seats[1] == 33109)
            {
                auto* boss = Boss();
                if (!boss || boss->HasUnitFlag(UNIT_FLAG_NON_ATTACKABLE)) { Fail("leviathan_not_ready"); return; }
                for (std::size_t i = 0; i < actors.size(); ++i)
                {
                    initial[i] = previous[i] = Actor(i)->GetVehicleBase()->GetPosition();
                    previousSpeed[i] = Actor(i)->GetVehicleBase()->GetSpeed(MOVE_RUN);
                }
                boss->AI()->AttackStart(tank->GetVehicleBase());
                boss->AI()->DoZoneInCombat();
                resetReengaged = resetReady;
                // Entering combat applies native Leviathan health scaling.
                // Take the baseline on the first combat observation, not the idle template HP.
                startingBossHealth = leastBossHealth = 0;
                Phase("observe");
            }
            else if (phaseMs > 60000) Fail("ordinary_ai_did_not_board_driver_seats");
        }
        else if (state == "observe")
        {
            if (!human || !tank || !ranged || GET_PLAYERBOT_AI(human)) { Fail("vehicle_actor_lost"); return; }
            auto* boss = Boss();
            if (!boss || !boss->IsAlive() || !boss->IsInCombat()) { Fail("leviathan_encounter_lost"); return; }
            ++samples; maxGap = std::max(maxGap, diff);
            if (!startingBossHealth) startingBossHealth = leastBossHealth = boss->GetHealth();
            leastBossHealth = std::min(leastBossHealth, boss->GetHealth());
            for (std::size_t i = 0; i < actors.size(); ++i)
            {
                auto* p = Actor(i);
                auto* base = p->GetVehicleBase();
                if (!p->IsAlive() || !base || !base->IsAlive() || base->GetGUID() != mounts[i] ||
                    base->GetMap() != boss->GetMap() || !p->GetVehicle() || p->GetVehicle()->GetPassenger(0) != p)
                { Fail("vehicle_driver_or_mount_lost"); return; }
                if (!Dojo::ContinuousMovement(base->GetExactDist2d(previous[i]), previousSpeed[i],
                    base->GetSpeed(MOVE_RUN), diff))
                { Fail("measured_vehicle_jumped"); return; }
                previous[i] = base->GetPosition();
                previousSpeed[i] = base->GetSpeed(MOVE_RUN);
                travelled[i] = std::max(travelled[i], base->GetExactDist2d(initial[i]));
            }
            if (scenario == "leviathan-reset" && !resetRequested && phaseMs >= 10000)
            {
                oldAccessories = Accessories(boss);
                if (oldAccessories.empty()) { Fail("reset_accessory_stimulus_missing"); return; }
                // The paired control has identical driving but no evade stimulus.
                if (negative) { Phase("cleanup"); return; }
                for (auto* p : {tank, ranged})
                {
                    GET_PLAYERBOT_AI(p)->ChangeStrategy("+stay,+passive", BOT_STATE_COMBAT);
                    GET_PLAYERBOT_AI(p)->ChangeStrategy("+stay", BOT_STATE_NON_COMBAT);
                }
                resetRequested = true;
                boss->AI()->EnterEvadeMode();
                // The reset boundary is setup, not measured combat. Withdraw
                // surviving actors so a freshly respawned boss cannot aggro
                // them before its idle state and new accessories are observed.
                for (auto* p : {human, tank, ranged})
                {
                    p->ExitVehicle(); p->CombatStop(true);
                    if (!p->TeleportTo(603, StartPoint.GetPositionX(), StartPoint.GetPositionY(), StartPoint.GetPositionZ(), 0.f))
                    { Fail("reset_withdraw_refused"); return; }
                }
                Phase("reset");
                return;
            }
            // Real raid stimulus: the human dies after both bots are driving.
            // The shipped feature promises drivers continue in the active fight.
            if (!deathAt && phaseMs >= 20000)
            { Unit::Kill(boss, human); deathAt = phaseMs; }
            if (deathAt && human->IsAlive()) { Fail("human_death_stimulus_lost"); return; }
            if (phaseMs >= 50000) Phase("cleanup");
        }
        else if (state == "reset")
        {
            ++samples; maxGap = std::max(maxGap, diff);
            if (!human || !human->IsAlive() || !tank || !ranged || !tank->IsAlive() || !ranged->IsAlive())
            { Fail("reset_actor_lost"); return; }
            removedAccessories = 0;
            for (auto guid : oldAccessories)
                if (!human->GetMap()->GetCreature(guid)) ++removedAccessories;
            auto* instance = human->GetInstanceScript();
            auto* boss = instance ? instance->GetCreature(0) : nullptr;
            auto const fresh = Accessories(boss);
            freshAccessories = uint32(fresh.size());
            bool const replaced = removedAccessories == oldAccessories.size() && fresh.size() == oldAccessories.size()
                && std::none_of(fresh.begin(), fresh.end(), [&](ObjectGuid guid)
                { return std::find(oldAccessories.begin(), oldAccessories.end(), guid) != oldAccessories.end(); });
            if (boss && boss->IsAlive() && !boss->IsInCombat() && !boss->IsEngaged()
                && !boss->HasUnitFlag(UNIT_FLAG_NON_ATTACKABLE) && replaced)
            {
                bossGuid = boss->GetGUID(); resetReady = true;
                // The second pull has a new setup boundary. Do not count effects
                // from the first pull as evidence of successful re-entry.
                hits = {}; movingHits = {}; afterDeathHits = {}; travelled = {}; seats = {}; mounts = {};
                startingBossHealth = leastBossHealth = deathAt = 0;
                for (auto* p : {human, tank, ranged})
                {
                    p->ExitVehicle(); p->CombatStop(true);
                    if (!p->TeleportTo(603, StartPoint.GetPositionX(), StartPoint.GetPositionY(), StartPoint.GetPositionZ(), 0.f))
                    { Fail("reset_setup_teleport_refused"); return; }
                }
                Phase("reset_arrive");
            }
            else if (phaseMs >= 60000) Fail("native_hard_reset_not_observed");
        }
        else if (state == "reset_arrive")
        {
            if (!human || !tank || !ranged) { Fail("reset_actor_lost"); return; }
            if (human->IsBeingTeleported() || tank->IsBeingTeleported() || ranged->IsBeingTeleported()) return;
            if (human->GetMapId() != 603 || tank->GetMap() != human->GetMap() || ranged->GetMap() != human->GetMap())
            { Fail("reset_destination_mismatch"); return; }
            for (auto* p : {tank, ranged})
            {
                GET_PLAYERBOT_AI(p)->ChangeStrategy("-stay,-passive,+ulduar", BOT_STATE_COMBAT);
                GET_PLAYERBOT_AI(p)->ChangeStrategy("-stay,-passive,+follow,+ulduar", BOT_STATE_NON_COMBAT);
            }
            Phase("vehicle_settle");
        }
        else if (state == "cleanup")
        {
            if (auto* boss = Boss()) boss->AI()->EnterEvadeMode();
            for (auto* p : {human, tank, ranged}) if (p) { p->ExitVehicle(); p->CombatStop(true); }
            for (std::size_t i = 0; i < actors.size(); ++i) if (auto* p = Actor(i))
            {
                auto* ai = GET_PLAYERBOT_AI(p);
                ai->SetMaster(nullptr);
            }
            if (auto* party = sGroupMgr->GetGroupByGUID(group.GetCounter()); party && party->GetGUID() == group) party->Disband();
            for (auto guid : actors) Dojo::LogoutRaidFixture(guid);
            humanGone = Dojo::EndHumanFixture(token);
            sPlayerbotAIConfig.RaidVehicleSupport = oldSupport;
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
        if (active && state != "cleanup" && state != "logout") Capture();
        return {{"kind", "dojo-vehicle-observation"}, {"format", 1}, {"scenario", scenario},
            {"token", token}, {"state", state}, {"failure", failure}, {"failure_facts", failureFacts}, {"negative", negative},
            {"observation_clock", "map_update"},
            {"reset_requested", resetRequested}, {"reset_ready", resetReady}, {"reset_reengaged", resetReengaged},
            {"reset_old_accessories", oldAccessories.size()}, {"reset_removed_accessories", removedAccessories},
            {"reset_fresh_accessories", freshAccessories},
            {"ready", ready}, {"human_boarded", humanBoarded}, {"cleanup", cleanup}, {"samples", samples},
            {"max_gap_ms", maxGap}, {"human_death_ms", deathAt}, {"driver_entries", seats},
            {"distance", travelled}, {"damage_hits", hits}, {"moving_hits", movingHits}, {"after_death_hits", afterDeathHits},
            {"boss_start_health", startingBossHealth}, {"boss_least_health", leastBossHealth},
            {"setup_add_kills", setupKills}, {"heal_events", healEvents}, {"effective_healing", effectiveHealing},
            {"dot_samples", dotSamples}, {"field_samples", fieldSamples}, {"surge_samples", surgeSamples},
            {"shield_samples", shieldSamples}, {"safe_ring_samples", ringSamples},
            {"field_escapes", fieldEscapes}, {"longest_field_ms", longestFieldMs}, {"field_ms", fieldMs},
            {"elapsed_ms", elapsed}, {"phase_ms", phaseMs}, {"static_fields", lastFields},
            {"bodies", lastBodies}, {"boss", lastBoss}, {"recent_effects", recentEffects}, {"recent_casts", recentCasts}};
    }
};
Trial trial;
class Commands : public CommandScript
{
public:
    Commands() : CommandScript("DojoVehicleCommands") { }
    ChatCommandTable GetCommands() const override
    {
        static ChatCommandTable commands{{"start", Start, SEC_CONSOLE, Console::Yes},
            {"status", Status, SEC_CONSOLE, Console::Yes}, {"abort", Abort, SEC_CONSOLE, Console::Yes}};
        static ChatCommandTable dojo{{"vehicle", commands}};
        return {{"dojo", dojo}};
    }
    static bool Start(ChatHandler* h, Tail args)
    {
        std::istringstream in{std::string(args)};
        std::string scenario, token, mode, extra; in >> scenario >> token >> mode >> extra;
        if (!extra.empty() || (mode != "normal" && mode != "negative"))
            h->SendSysMessage("dojo vehicle: expected scenario token normal|negative");
        else h->SendSysMessage("dojo vehicle: " + trial.Start(scenario, token, mode == "negative"));
        return true;
    }
    static bool Status(ChatHandler* h) { h->SendSysMessage("dojo-vehicle " + trial.Status().dump()); return true; }
    static bool Abort(ChatHandler* h) { if (trial.active) trial.Fail("aborted"); h->SendSysMessage("dojo vehicle: cleanup requested"); return true; }
};
class Ticks : public WorldScript
{
public:
    Ticks() : WorldScript("DojoVehicleTicks", {WORLDHOOK_ON_UPDATE}) { }
    void OnUpdate(uint32 diff) override { trial.WorldUpdate(diff); }
};
class MapTicks : public AllMapScript
{
public:
    MapTicks() : AllMapScript("DojoVehicleMapTicks", {ALLMAPHOOK_ON_MAP_UPDATE}) { }
    void OnMapUpdate(Map* map, uint32 diff) override { trial.MapUpdate(map, diff); }
};
class Effects : public UnitScript
{
public:
    Effects() : UnitScript("DojoVehicleEffects", true,
        {UNITHOOK_ON_DAMAGE, UNITHOOK_ON_HEAL, UNITHOOK_ON_UNIT_DEATH}) { }
    void OnHeal(Unit* healer, Unit* receiver, uint32& gain) override
    {
        std::lock_guard<std::recursive_mutex> lock(trial.mutex);
        trial.RecordEffect("heal", healer, receiver, gain);
        if (trial.state != "drakes" || !healer || !receiver || healer->GetGUID() != trial.mounts[0] ||
            std::find(trial.mounts.begin(), trial.mounts.end(), receiver->GetGUID()) == trial.mounts.end()) return;
        auto const effective = std::min(gain, receiver->GetMaxHealth() - receiver->GetHealth());
        if (effective) { ++trial.healEvents; trial.effectiveHealing += effective; }
    }
    void OnDamage(Unit* attacker, Unit* victim, uint32& damage) override
    {
        std::lock_guard<std::recursive_mutex> lock(trial.mutex);
        trial.RecordEffect("damage", attacker, victim, damage);
        if ((trial.state != "observe" && trial.state != "drakes") || !attacker || !victim || !damage || victim->GetGUID() != trial.bossGuid) return;
        for (std::size_t i = 0; i < trial.mounts.size(); ++i)
            if (attacker->GetGUID() == trial.mounts[i])
            {
                ++trial.hits[i];
                if (attacker->isMoving()) ++trial.movingHits[i];
                if (trial.deathAt && trial.phaseMs >= trial.deathAt + 5000) ++trial.afterDeathHits[i];
            }
    }
    void OnUnitDeath(Unit* victim, Unit* killer) override
    {
        std::lock_guard<std::recursive_mutex> lock(trial.mutex);
        trial.RecordEffect("death", killer, victim, 0);
    }
};
class Casts : public AllSpellScript
{
public:
    Casts() : AllSpellScript("DojoVehicleCasts", {ALLSPELLHOOK_ON_CAST}) { }
    void OnSpellCast(Spell* spell, Unit* caster, SpellInfo const* info, bool) override
    {
        if (!spell || spell->IsTriggered() || !caster || !info) return;
        std::lock_guard<std::recursive_mutex> lock(trial.mutex);
        if (!trial.active || (trial.state != "observe" && trial.state != "drakes")) return;
        for (std::size_t slot = 0; slot < trial.mounts.size(); ++slot)
            if (caster->GetGUID() == trial.mounts[slot])
            {
                auto* boss = trial.Boss();
                json event{{"slot", slot}, {"spell", info->Id}, {"phase_ms", trial.phaseMs},
                    {"energy", caster->GetPower(POWER_ENERGY)}, {"moving", caster->isMoving()},
                    {"facing", caster->GetOrientation()}};
                if (boss) event.update({{"boss_distance", caster->GetExactDist2d(boss)},
                    {"boss_bearing", caster->GetAngle(boss)}, {"pursued", boss->GetVictim() == caster}});
                if (trial.recentCasts.size() >= 32) trial.recentCasts.erase(trial.recentCasts.begin());
                trial.recentCasts.push_back(std::move(event));
            }
    }
};
}
void AddSC_dojo_vehicle() { new Commands(); new Ticks(); new MapTicks(); new Effects(); new Casts(); }
#else
void AddSC_dojo_vehicle() { }
#endif
#else
void AddSC_dojo_vehicle() { }
#endif
