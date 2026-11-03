// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "GuildFishing.h"
#include "GuildPorts.h"
#include "Realm/RealmModule.h"
#include "Realm/RealmPorts.h"
#include "LeeroyGuildFishingProof.h"
#include "Action.h"
#include "Bag.h"
#include "Engine.h"
#include "FishingAction.h"
#include "FishValues.h"
#include "GameObject.h"
#include "LastMovementValue.h"
#include "MotionMaster.h"
#include "MovementGenerator.h"
#include "PlayerbotAI.h"
#include "PositionValue.h"
#include "ReactionEngine.h"
#include "ScriptMgr.h"
#include "Spell.h"
#include "WorldSession.h"
#include <algorithm>
#include <array>
#include <map>
#include <mutex>
#include <memory>
#include <limits>

namespace Leeroy::Guild
{
namespace
{
using namespace Leeroy::Realm;
constexpr std::array<uint8, 3> Slots{EQUIPMENT_SLOT_MAINHAND, EQUIPMENT_SLOT_OFFHAND, EQUIPMENT_SLOT_RANGED};
constexpr uint32 NativeSpotTimeout = 180000; // FishingAction.cpp's native stale-location limit.
constexpr std::array<uint32, 6> FishingSpells{7620, 7731, 7732, 18248, 33095, 51294};
bool FishingSpell(uint32 spell)
{ return std::find(FishingSpells.begin(), FishingSpells.end(), spell) != FishingSpells.end(); }
enum class Phase { Fence, Spot, Moving, Equip, Cast, AwaitCast, Wait, Ended, Verified };
struct Attempt;
struct GateLifetime;
struct Gate : ActionExecutionListener
{
    std::weak_ptr<Attempt> attempt;
    std::shared_ptr<GateLifetime> lifetime;
    Gate(std::shared_ptr<Attempt> const& value, std::shared_ptr<GateLifetime> receipt)
        : attempt(value), lifetime(std::move(receipt)) { }
    ~Gate() override;
    bool Before(Action*, Event) override;
    bool AllowExecution(Action*, Event) override { return true; }
    void After(Action*, bool, Event) override;
    bool OverrideResult(Action*, bool executed, Event) override { return executed; }
};
struct GateLifetime { Engine* engine = nullptr; Gate* gate = nullptr; bool alive = true; };
struct Attempt
{
    uint32 guid = 0;
    Player* body = nullptr;
    PlayerbotAI* ai = nullptr;
    WorldSession* session = nullptr;
    Phase phase = Phase::Fence;
    std::array<uint32, 3> original{}, expected{};
    uint32 pole = 0;
    FishingProof proof;
    WorldPosition originalSpot, spot;
    uint32 spotStamp = 0;
    bool spotChanged = false, fenced = false, gatesRemoved = false, used = false, succeeded = false;
    bool movementChanged = false, valuesRestored = false;
    bool removalRequested = false;
    std::size_t inFlight = 0;
    MovementGenerator* movement = nullptr;
    uint32 spline = 0;
    uint32 oldMoveMap = 0, oldMoveMs = 0;
    uint32 ownedMoveMs = 0;
    float oldX = 0, oldY = 0, oldZ = 0, oldO = 0, oldDelay = 0;
    Unit* oldFollow = nullptr;
    WorldPosition oldShort;
    MovementPriority oldPriority = MovementPriority::MOVEMENT_NORMAL;
    std::vector<std::shared_ptr<GateLifetime>> gates;
    ReactionEngine* reaction = nullptr;
    std::array<std::vector<std::string>, BOT_STATE_MAX> strategies;
    std::string failure;
    ObjectGuid loot;
};
struct Job
{
    std::string token, id;
    std::vector<std::shared_ptr<Attempt>> attempts;
    std::size_t cursor = 0;
};
std::mutex inbox;
std::map<std::string, std::shared_ptr<Job>, std::less<>> journals;
std::uint64_t dispatchSerial = 0;
struct Dispatch { std::shared_ptr<Attempt> attempt; uint64 serial = 0; bool use = false; };
thread_local Dispatch* dispatch = nullptr;
struct CastFrame
{
    std::shared_ptr<Attempt> attempt;
    FishingCastCandidate candidate;
};
thread_local CastFrame castFrame;
struct DispatchScope
{
    Dispatch local;
    explicit DispatchScope(std::shared_ptr<Attempt> value, uint64 serial, bool use = false)
        : local{std::move(value), serial, use} { dispatch = &local; }
    ~DispatchScope() { dispatch = nullptr; }
};
struct Quiescence : CleanupQuiescence
{
    std::unique_lock<std::mutex> lock{inbox};
};
Gate::~Gate()
{
    std::lock_guard lock(inbox);
    lifetime->alive = false;
    if (auto value = attempt.lock(); value && !value->gatesRemoved) value->proof.poisoned = true;
}

bool Gate::Before(Action* action, Event)
{
    auto value = attempt.lock();
    if (!value) return true;
    std::lock_guard lock(inbox);
    if (value->gatesRemoved) return true;
    ++value->inFlight;
    // Block execution on every engine. Fishing-specific pre-listener checks
    // are pure except move-near-water's spot write: record it as interference.
    if (action && FishingActionName(action->getName())) { value->proof.poisoned = true; return false; }
    // Ordinary combat is allowed to interrupt this attempt. It must finish
    // before equipment restoration, and foreign equipment/movement still fails
    // exact cleanup checks. Retained fishing must not disable self-defence.
    return value->body && value->body->IsInCombat();
}
void Gate::After(Action*, bool, Event)
{
    if (auto value = attempt.lock())
    {
        std::lock_guard lock(inbox);
        if (value->inFlight) --value->inFlight;
    }
}
std::shared_ptr<Attempt> FindAttempt(uint32 guid)
{
    // Caller holds inbox. This is bounded receipt lookup, never body admission.
    for (auto const& [id, job] : journals)
    {
        (void)id;
        for (auto const& value : job->attempts) if (value->guid == guid && !value->gatesRemoved) return value;
    }
    return {};
}
std::array<uint32, 3> Weapons(Player* bot)
{
    std::array<uint32, 3> result{};
    for (std::size_t i = 0; i < Slots.size(); ++i)
        if (auto* item = bot->GetItemByPos(INVENTORY_SLOT_BAG_0, Slots[i])) result[i] = item->GetGUID().GetCounter();
    return result;
}
bool Pole(Item const* item)
{
    return item && item->GetTemplate()->Class == ITEM_CLASS_WEAPON
        && item->GetTemplate()->SubClass == ITEM_SUBCLASS_WEAPON_FISHING_POLE;
}
Item* ExistingPole(Player* bot)
{
    if (auto* item = bot->GetItemByPos(INVENTORY_SLOT_BAG_0, EQUIPMENT_SLOT_MAINHAND); Pole(item)) return item;
    // Identical order to EquipFishingPoleAction::isUseful, without its spawn fallback.
    for (uint8 slot = INVENTORY_SLOT_ITEM_START; slot < INVENTORY_SLOT_ITEM_END; ++slot)
        if (auto* item = bot->GetItemByPos(INVENTORY_SLOT_BAG_0, slot); Pole(item)) return item;
    for (uint8 slot = INVENTORY_SLOT_BAG_START; slot < INVENTORY_SLOT_BAG_END; ++slot)
        if (auto* bag = bot->GetBagByPos(slot))
            for (uint32 index = 0; index < bag->GetBagSize(); ++index)
                if (auto* item = bag->GetItemByPos(static_cast<uint8>(index)); Pole(item)) return item;
    return nullptr;
}
std::string Ready(Player* bot)
{
    if (!bot || !GET_PLAYERBOT_AI(bot) || IsSelfBot(bot)) return "busy:body_unavailable";
    auto* ai = GET_PLAYERBOT_AI(bot);
    if (bot->isDead() || bot->IsInCombat() || bot->GetGroup() || bot->GetGroupInvite() || ai->GetMaster()
        || ai->GetState() != BOT_STATE_NON_COMBAT || bot->IsBeingTeleported() || bot->IsInFlight()
        || bot->GetVehicle() || bot->IsMounted() || bot->isSwimming() || bot->isMoving()
        || bot->GetMotionMaster()->GetCurrentMovementGeneratorType() != IDLE_MOTION_TYPE
        || !bot->GetMap() || bot->GetMap()->Instanceable() || bot->InBattlegroundQueue()
        || sLFGMgr->GetState(bot->GetGUID()) != lfg::LFG_STATE_NONE) return "busy:fishing_idle_required";
    if (!bot->GetSkillValue(SKILL_FISHING) || !bot->HasSpell(FISHING_SPELL)) return "fishing_skill_required";
    auto const& movement = ai->GetAiObjectContext()->GetValue<LastMovement&>("last movement")->Get();
    if (movement.lastFollow || movement.future.valid()) return "busy:fishing_movement_state";
    if (bot->GetCurrentSpell(CURRENT_GENERIC_SPELL) || bot->GetCurrentSpell(CURRENT_CHANNELED_SPELL)
        || bot->GetGameObject(FISHING_SPELL) || bot->GetGameObject(0)
        || !bot->GetGuidValue(UNIT_FIELD_CHANNEL_OBJECT).IsEmpty()
        || !bot->GetLootGUID().IsEmpty()) return "busy:fishing_state";
    for (auto spell : FishingSpells) if (bot->GetGameObject(spell)) return "busy:fishing_state";
    for (uint8 state = 0; state < BOT_STATE_MAX; ++state)
        if (ai->HasStrategy("master fishing", static_cast<BotState>(state))
            || ai->HasStrategy("use bobber", static_cast<BotState>(state))) return "busy:fishing_strategy";
    auto* pole = ExistingPole(bot);
    if (!pole || pole->GetOwnerGUID() != bot->GetGUID()) return "fishing_pole_required";
    if (bot->CanUseItem(pole) != EQUIP_ERR_OK) return "fishing_pole_cannot_use";
    if (Weapons(bot)[1] && !bot->GetFreeInventorySpace()) return "fishing_bag_space_required";
    return "";
}
std::string Admission(Queued const&, uint32 guid)
{
    if (!GuildEnabled()) return "disabled";
    if (!Guild::IsCast(guid) || guid == Guild::HumanGuid()) return "not_cast";
    if (!Realm::IsOnRandomBotAccount(guid)) return "not_bot";
    {
        std::lock_guard lock(inbox);
        if (FindAttempt(guid)) return "busy:fishing_outcome_pending";
    }
    return Ready(Guild::FindOnline(guid));
}
void EngineBarrier(PlayerbotAI* ai, std::shared_ptr<Attempt> const& value, bool install)
{
    auto const state = ai->GetState();
    auto* context = ai->GetAiObjectContext();
    auto* stay = context->GetValue<PositionInfo>("pos", "stay");
    auto* combat = context->GetValue<time_t>("combat start time");
    auto const oldStay = stay->Get();
    auto const oldCombat = combat->Get();
    for (uint8 index = 0; index < BOT_STATE_MAX; ++index)
    {
        ai->ChangeEngine(static_cast<BotState>(index));
        auto* engine = ai->GetCurrentEngine();
        engine->Init(); // Includes original current engine: retire stale pending actions.
        if (index == BOT_STATE_REACTION)
        {
            auto* reaction = static_cast<ReactionEngine*>(engine);
            reaction->ResetReactions(); // Init/Engine::Reset do not clear these two fields.
            if (install) value->reaction = reaction;
        }
        if (install)
        {
            auto lifetime = std::make_shared<GateLifetime>();
            auto* gate = new Gate(value, lifetime);
            lifetime->engine = engine; lifetime->gate = gate;
            engine->AddActionExecutionListener(gate);
            value->gates.push_back(std::move(lifetime));
        }
    }
    ai->ChangeEngine(state);
    // These are the only value writes performed by the public engine switches.
    stay->Set(oldStay);
    combat->Set(oldCombat);
}
RealmStep Begin(Queued& entry, uint32)
{
    std::string error;
    auto& module = RealmModule::Instance();
    auto job = std::make_shared<Job>();
    job->id = entry.id;
    for (auto guid : entry.guids)
    {
        auto* bot = Guild::FindOnline(guid);
        if (auto why = Ready(bot); !why.empty()) return {StepState::Refused, why};
        auto value = std::make_shared<Attempt>();
        value->guid = guid; value->body = bot; value->ai = GET_PLAYERBOT_AI(bot); value->session = bot->GetSession();
        value->original = value->expected = Weapons(bot);
        value->pole = ExistingPole(bot)->GetGUID().GetCounter();
        auto* ai = GET_PLAYERBOT_AI(bot);
        auto* spot = static_cast<FishingSpotValue*>(ai->GetAiObjectContext()->GetValue<WorldPosition>("fishing spot"));
        value->originalSpot = value->spot = spot->Get(); value->spotStamp = spot->lastUpdated();
        for (uint8 state = 0; state < BOT_STATE_MAX; ++state)
            value->strategies[state] = ai->GetStrategies(static_cast<BotState>(state));
        job->attempts.push_back(std::move(value));
    }
    auto owner = module.PrepareGuildFishingContinuation(entry.id, entry.guids, error);
    if (!owner) return {StepState::Refused, error};
    job->token = owner->token;
    { std::lock_guard lock(inbox); journals.emplace(entry.id, std::move(job)); }
    return {StepState::Detached}; // Ownership precedes every native action.
}
bool SameStrategies(PlayerbotAI* ai, Attempt const& value)
{
    for (uint8 state = 0; state < BOT_STATE_MAX; ++state)
        if (ai->GetStrategies(static_cast<BotState>(state)) != value.strategies[state]) return false;
    return true;
}
void SaveMovement(PlayerbotAI* ai, Attempt& value)
{
    auto& movement = ai->GetAiObjectContext()->GetValue<LastMovement&>("last movement")->Get();
    value.oldMoveMap = movement.lastMoveToMapId; value.oldX = movement.lastMoveToX;
    value.oldY = movement.lastMoveToY; value.oldZ = movement.lastMoveToZ; value.oldO = movement.lastMoveToOri;
    value.oldMoveMs = movement.msTime; value.oldDelay = movement.lastdelayTime;
    value.oldFollow = movement.lastFollow; value.oldShort = movement.lastMoveShort; value.oldPriority = movement.priority;
}
void RestoreMovement(PlayerbotAI* ai, Attempt const& value)
{
    auto& movement = ai->GetAiObjectContext()->GetValue<LastMovement&>("last movement")->Get();
    movement.lastMoveToMapId = value.oldMoveMap; movement.lastMoveToX = value.oldX;
    movement.lastMoveToY = value.oldY; movement.lastMoveToZ = value.oldZ; movement.lastMoveToOri = value.oldO;
    movement.msTime = value.oldMoveMs; movement.lastdelayTime = value.oldDelay;
    movement.lastFollow = value.oldFollow; movement.lastMoveShort = value.oldShort; movement.priority = value.oldPriority;
}
std::string RestoreOne(Player* bot, Attempt& value)
{
    if (Weapons(bot) != value.expected) return "fishing_equipment_interference";
    for (std::size_t index = 0; index < Slots.size(); ++index)
    {
        if (value.expected[index] == value.original[index]) continue;
        auto* equipped = bot->GetItemByPos(INVENTORY_SLOT_BAG_0, Slots[index]);
        if (equipped && !(index == 0 && equipped->GetGUID().GetCounter() == value.pole))
            return "fishing_foreign_equipment";
        if (value.original[index])
        {
            auto* item = bot->GetItemByGuid(ObjectGuid::Create<HighGuid::Item>(value.original[index]));
            if (!item || item->GetOwnerGUID() != bot->GetGUID() || item->IsEquipped()
                || !Player::IsInventoryPos(item->GetPos())) return "fishing_original_weapon_missing";
            uint16 destination = 0;
            if (bot->CanEquipItem(Slots[index], destination, item, true) != EQUIP_ERR_OK
                || destination != uint16((INVENTORY_SLOT_BAG_0 << 8) | Slots[index])) return "fishing_original_weapon_cannot_equip";
            bot->SwapItem(item->GetPos(), destination);
        }
        else
        {
            ItemPosCountVec destination;
            if (!equipped || bot->CanUnequipItem(equipped->GetPos(), false) != EQUIP_ERR_OK
                || bot->CanStoreItem(NULL_BAG, NULL_SLOT, destination, equipped, false) != EQUIP_ERR_OK
                || destination.size() != 1 || destination.front().count != 1) return "fishing_pole_cannot_store";
            bot->SwapItem(equipped->GetPos(), destination.front().pos);
        }
        value.expected[index] = value.original[index];
        if (Weapons(bot) != value.expected) return "fishing_weapon_restore_unproved";
        return "pending";
    }
    return "";
}
PvpContinuationStep Step(PvpOwner const& owner)
{
    std::shared_ptr<Job> job;
    { std::lock_guard lock(inbox); auto it = journals.find(owner.originId); if (it != journals.end()) job = it->second; }
    auto unknown = [](std::string why) { return PvpContinuationStep{PvpStage::Unknown, std::move(why)}; };
    if (!job || job->token != owner.token) return unknown("fishing_journal_unavailable");
    bool const cleaning = owner.stage == PvpStage::Cleaning;
    auto wait = [&](std::string reason = {}) { return PvpContinuationStep{owner.stage, std::move(reason)}; };
    auto& module = RealmModule::Instance();
    for (std::size_t count = 0; count < job->attempts.size(); ++count)
    {
        auto value = job->attempts[job->cursor++ % job->attempts.size()];
        auto* bot = Guild::FindOnline(value->guid);
        if (!bot) return unknown("fishing_body_unavailable");
        auto* ai = GET_PLAYERBOT_AI(bot);
        { std::lock_guard lock(inbox); if (value->proof.poisoned) return unknown("fishing_native_interference"); }
        if (!SameStrategies(ai, *value)) return unknown("fishing_strategy_interference");
        if (Weapons(bot) != value->expected) return unknown("fishing_equipment_interference");
        auto* spotValue = static_cast<FishingSpotValue*>(ai->GetAiObjectContext()->GetValue<WorldPosition>("fishing spot"));
        if (spotValue->Get() != value->spot || spotValue->lastUpdated() != value->spotStamp)
            return unknown("fishing_spot_interference");
        if (value->movementChanged
            && ai->GetAiObjectContext()->GetValue<LastMovement&>("last movement")->Get().msTime != value->ownedMoveMs)
            return unknown("fishing_movement_value_interference");
        if (bot->GetGroup() || bot->GetGroupInvite() || ai->GetMaster() || bot->IsBeingTeleported())
            return unknown("fishing_body_scope_changed");
        if (value->phase == Phase::Verified) continue;
        if (bot->IsInCombat() || bot->isDead())
        {
            value->failure = "fishing_interrupted";
            if (!cleaning) value->phase = Phase::Ended;
        }
        if (!cleaning && value->phase == Phase::Ended) continue;
        if (auto why = module.ChargeContinuationBodies(owner.token, {value->guid}); !why.empty())
            return why == "busy:tick_budget" ? wait(why) : unknown(why);
        if (cleaning)
        {
            { std::lock_guard lock(inbox); value->proof.sealed = true; }
            if (bot->IsInCombat() || bot->isDead()) return wait("fishing_cleanup_waiting_for_idle");
            if (bot->isMoving())
            {
                if (!value->movement || bot->GetMotionMaster()->top() != value->movement
                    || bot->GetMotionMaster()->GetCurrentSplineId() != value->spline) return unknown("fishing_movement_interference");
                bot->StopMoving(); bot->GetMotionMaster()->Clear(); value->movement = nullptr;
                return wait();
            }
            auto* channel = bot->GetCurrentSpell(CURRENT_CHANNELED_SPELL);
            FishingProof facts;
            { std::lock_guard lock(inbox); facts = value->proof; }
            if (channel)
            {
                if (!facts.prepared || reinterpret_cast<uintptr_t>(channel) != facts.spell
                    || channel->GetSpellInfo()->Id != FISHING_SPELL) return unknown("fishing_channel_interference");
                auto* owned = bot->GetGameObject(FISHING_SPELL);
                if (owned && owned->GetGUID().GetRawValue() != facts.bobber) return unknown("fishing_bobber_interference");
                if (owned) value->removalRequested = true;
                channel->cancel(); return wait();
            }
            if (bot->GetCurrentSpell(CURRENT_GENERIC_SPELL)) return unknown("fishing_foreign_cast");
            if (!bot->GetLootGUID().IsEmpty())
            {
                ObjectGuid loot;
                { std::lock_guard lock(inbox); loot = value->loot; }
                if (loot.IsEmpty() || bot->GetLootGUID() != loot) return unknown("fishing_loot_interference");
                bot->GetSession()->DoLootRelease(loot); return wait();
            }
            if (facts.bobber)
                if (auto* go = bot->GetMap()->GetGameObject(ObjectGuid(facts.bobber)))
                {
                    if (go->GetOwnerGUID() != bot->GetGUID() || go->GetGoType() != GAMEOBJECT_TYPE_FISHINGNODE)
                    {
                        if (value->removalRequested && go->GetOwnerGUID().IsEmpty()) return wait("fishing_bobber_removal_pending");
                        return unknown("fishing_bobber_interference");
                    }
                    if (!value->removalRequested) { bot->RemoveGameObject(go, true); value->removalRequested = true; }
                    return wait();
                }
            if ((facts.bobber && !facts.removed) || (facts.prepared && !facts.bobber && !facts.canceled))
                return wait("fishing_native_end_receipt_pending");
            auto restored = RestoreOne(bot, *value);
            if (restored == "pending") return wait();
            if (!restored.empty()) return unknown(restored);
            if (!value->valuesRestored)
            {
                if (value->spotChanged)
                {
                    spotValue->Set(value->originalSpot);
                    value->spot = value->originalSpot; value->spotStamp = spotValue->lastUpdated();
                }
                value->spotChanged = false;
                if (value->movementChanged) { RestoreMovement(ai, *value); value->ownedMoveMs = value->oldMoveMs; }
                if (value->fenced) EngineBarrier(ai, value, false);
                value->valuesRestored = true;
                return wait();
            }
            if (!facts.CanHandBack(Weapons(bot) == value->original,
                !channel && bot->GetGuidValue(UNIT_FIELD_CHANNEL_OBJECT).IsEmpty(),
                !facts.bobber || !bot->GetMap()->GetGameObject(ObjectGuid(facts.bobber)), bot->GetLootGUID().IsEmpty()))
                return unknown("fishing_handback_unproved");
            value->phase = Phase::Verified;
            return wait();
        }
        switch (value->phase)
        {
            case Phase::Fence:
                if (auto why = Ready(bot); !why.empty()) { value->failure = why; value->phase = Phase::Ended; break; }
                EngineBarrier(ai, value, true); value->fenced = true; value->phase = Phase::Spot; break;
            case Phase::Spot:
            {
                MoveNearWaterAction action(ai);
                bool const move = action.isPossible();
                value->spot = spotValue->Get(); value->spotStamp = spotValue->lastUpdated(); value->spotChanged = true;
                if (!value->spot.IsValid() || spotValue->IsStale(NativeSpotTimeout))
                { value->failure = "fishing_spot_unavailable"; value->phase = Phase::Ended; break; }
                if (!move)
                {
                    if (bot->GetExactDist(&value->spot) >= 0.1f)
                    { value->failure = "fishing_spot_unavailable"; value->phase = Phase::Ended; }
                    else value->phase = Phase::Equip;
                    break;
                }
                SaveMovement(ai, *value);
                bool const moved = action.Execute(Event("leeroy"));
                value->movementChanged = true;
                value->ownedMoveMs = ai->GetAiObjectContext()->GetValue<LastMovement&>("last movement")->Get().msTime;
                value->movement = bot->GetMotionMaster()->top();
                value->spline = bot->GetMotionMaster()->GetCurrentSplineId();
                if (!moved) { value->failure = "fishing_movement_refused"; value->phase = Phase::Ended; }
                else value->phase = Phase::Moving;
                break;
            }
            case Phase::Moving:
                if (bot->isMoving())
                {
                    if (bot->GetMotionMaster()->top() != value->movement
                        || bot->GetMotionMaster()->GetCurrentSplineId() != value->spline)
                        return unknown("fishing_movement_interference");
                }
                else if (bot->GetExactDist(&value->spot) < 0.1f) { value->movement = nullptr; value->phase = Phase::Equip; }
                else { value->failure = "fishing_movement_incomplete"; value->phase = Phase::Ended; }
                break;
            case Phase::Equip:
            {
                auto* pole = ExistingPole(bot);
                if (!pole || pole->GetGUID().GetCounter() != value->pole || pole->GetOwnerGUID() != bot->GetGUID())
                    return unknown("fishing_pole_changed");
                EquipFishingPoleAction action(ai);
                if (value->expected[0] != value->pole)
                {
                    uint16 destination = 0;
                    if ((value->expected[1] && !bot->GetFreeInventorySpace())
                        || bot->CanEquipItem(EQUIPMENT_SLOT_MAINHAND, destination, pole, true) != EQUIP_ERR_OK)
                    { value->failure = "fishing_pole_cannot_equip"; value->phase = Phase::Ended; break; }
                    if (!action.isUseful() || !action.Execute(Event("leeroy"))) return unknown("fishing_pole_equip_unproved");
                    value->expected[0] = value->pole;
                    if (pole->GetTemplate()->InventoryType == INVTYPE_2HWEAPON) value->expected[1] = 0;
                    if (Weapons(bot) != value->expected) return unknown("fishing_pole_equip_unproved");
                }
                value->phase = Phase::Cast; break;
            }
            case Phase::Cast:
            {
                auto* pole = ExistingPole(bot);
                if (!pole || pole->GetGUID().GetCounter() != value->pole || value->expected[0] != value->pole)
                    return unknown("fishing_pole_changed");
                FishingAction action(ai);
                if (!action.isUseful()) { value->failure = "fishing_not_ready"; value->phase = Phase::Ended; break; }
                if (dispatch || dispatchSerial == std::numeric_limits<uint64>::max()) return unknown("fishing_dispatch_unavailable");
                {
                    DispatchScope capability(value, ++dispatchSerial);
                    action.Execute(Event("leeroy"));
                }
                // Native adds this strategy even when CastSpell refuses. Remove
                // it synchronously and Init retires any queued bobber actions.
                ai->ChangeStrategy("-use bobber", BOT_STATE_NON_COMBAT);
                FishingProof facts;
                { std::lock_guard lock(inbox); facts = value->proof; }
                if (facts.poisoned) return unknown("fishing_cast_interference");
                if (!facts.prepared) { value->failure = "fishing_cast_refused"; value->phase = Phase::Ended; }
                else value->phase = Phase::AwaitCast;
                break;
            }
            case Phase::AwaitCast:
            {
                FishingProof facts;
                { std::lock_guard lock(inbox); facts = value->proof; }
                auto* channel = bot->GetCurrentSpell(CURRENT_CHANNELED_SPELL);
                auto* bobber = bot->GetGameObject(FISHING_SPELL);
                if (channel && reinterpret_cast<uintptr_t>(channel) != facts.spell)
                    return unknown("fishing_channel_interference");
                if (bobber && (!facts.Started() || bobber->GetGUID().GetRawValue() != facts.bobber))
                    return unknown("fishing_uncommitted_bobber");
                if (facts.Started())
                {
                    if (!channel || !bobber || bot->GetGuidValue(UNIT_FIELD_CHANNEL_OBJECT).GetRawValue() != facts.bobber)
                    { value->failure = "fishing_attempt_ended"; value->phase = Phase::Ended; }
                    else value->phase = Phase::Wait;
                }
                else if (facts.cast) { value->failure = "fishing_bobber_not_created"; value->phase = Phase::Ended; }
                else if (facts.canceled) { value->failure = "fishing_cast_cancelled"; value->phase = Phase::Ended; }
                // The exact prepared channel waits for its own native cast or
                // cancellation receipt. Elapsed time cannot complete it.
                break;
            }
            case Phase::Wait:
            {
                FishingProof facts;
                { std::lock_guard lock(inbox); facts = value->proof; }
                auto* channel = bot->GetCurrentSpell(CURRENT_CHANNELED_SPELL);
                auto* bobber = bot->GetMap()->GetGameObject(ObjectGuid(facts.bobber));
                if (!channel || !bobber)
                { value->failure = "fishing_attempt_ended"; value->phase = Phase::Ended; break; }
                if (reinterpret_cast<uintptr_t>(channel) != facts.spell || bobber->GetOwnerGUID() != bot->GetGUID()
                    || bot->GetGameObject(FISHING_SPELL) != bobber) return unknown("fishing_channel_interference");
                if (bobber->getLootState() != GO_READY) break;
                // The native action searches a value cache. Only the exact
                // current bobber may appear as an own fishing-node candidate.
                auto const candidates = ai->GetAiObjectContext()->GetValue<GuidVector>("nearest game objects no los")->Get();
                bool found = false;
                for (auto const& guid : candidates)
                    if (auto* go = ai->GetGameObject(guid); go && go->GetEntry() == FISHING_BOBBER
                        && go->GetOwnerGUID() == bot->GetGUID())
                    { if (go != bobber) return unknown("fishing_bobber_interference"); found = true; }
                if (!found) break;
                UseBobberAction action(ai);
                {
                    DispatchScope capability(value, facts.serial, true);
                    value->used = action.Execute(Event("leeroy"));
                }
                value->succeeded = value->used;
                if (!value->used) value->failure = "fishing_bobber_use_refused";
                value->phase = Phase::Ended; break;
            }
            default: break;
        }
        return wait(); // At most one native member stage per controller visit.
    }
    if (!cleaning) return {PvpStage::Cleaning};
    bool resetReaction = false;
    for (auto const& value : job->attempts)
        if (value->reaction && (value->reaction->IsReacting() || value->reaction->HasIncomingReaction()))
        {
            if (resetReaction) return wait("fishing_reaction_seal_pending");
            if (auto why = module.ChargeContinuationBodies(owner.token, {value->guid}); !why.empty())
                return why == "busy:tick_budget" ? wait(why) : unknown(why);
            value->reaction->ResetReactions(); // Outside proof guard, one charged native member.
            resetReaction = true;
        }
    auto guard = std::make_shared<Quiescence>();
    for (auto const& value : job->attempts)
    {
        if (value->phase != Phase::Verified || value->proof.poisoned) return unknown("fishing_final_proof_changed");
        if (value->inFlight) return wait("fishing_native_execution_pending");
        if (value->reaction && (value->reaction->IsReacting() || value->reaction->HasIncomingReaction()))
            return wait("fishing_reaction_seal_pending");
        auto* bot = Guild::FindOnline(value->guid);
        if (bot && (bot->IsInCombat() || bot->isDead())) return wait("fishing_cleanup_waiting_for_idle");
        if (auto why = Ready(bot); !why.empty()) return unknown("fishing_final_state_changed:" + why);
        auto* spot = static_cast<FishingSpotValue*>(GET_PLAYERBOT_AI(bot)->GetAiObjectContext()->GetValue<WorldPosition>("fishing spot"));
        if (spot->Get() != value->spot || spot->lastUpdated() != value->spotStamp)
            return unknown("fishing_final_spot_interference");
        if (!bot || Weapons(bot) != value->original || !SameStrategies(GET_PLAYERBOT_AI(bot), *value)
            || bot->GetCurrentSpell(CURRENT_CHANNELED_SPELL) || bot->GetCurrentSpell(CURRENT_GENERIC_SPELL)
            || !bot->GetGuidValue(UNIT_FIELD_CHANNEL_OBJECT).IsEmpty() || bot->isMoving()
            || !bot->GetLootGUID().IsEmpty()) return unknown("fishing_final_state_changed");
    }
    PvpCleanProof proof;
    proof.token = owner.token; proof.revision = owner.revision; proof.members = owner.members;
    proof.quiescence = std::move(guard);
    return {PvpStage::Clean, "", std::move(proof)};
}
void Outcome(Queued const& entry, bool clean, std::string const& reason)
{
    std::shared_ptr<Job> job;
    { std::lock_guard lock(inbox); auto found = journals.find(entry.id); if (found != journals.end()) job = found->second; }
    JobEvent event;
    event.id = entry.id; event.kind = "bot_do"; event.state = clean ? "done" : "failed"; event.reason = reason;
    if (job)
    {
        for (auto const& value : job->attempts)
        {
            if (clean && !value->gatesRemoved)
            {
                auto* bot = Guild::FindOnline(value->guid);
                { std::lock_guard lock(inbox); value->gatesRemoved = true; }
                // Publication may wait behind a bounded backlog after release.
                // A logged-out body's engines own/delete their listeners; never
                // dereference a saved engine after that lifetime has changed.
                if (bot == value->body && GET_PLAYERBOT_AI(bot) == value->ai && bot->GetSession() == value->session)
                    for (auto const& lifetime : value->gates)
                    {
                        bool alive;
                        { std::lock_guard lock(inbox); alive = lifetime->alive; }
                        // The destructor receipt is immutable across allocator
                        // pointer reuse after a deferred logout/login boundary.
                        if (alive)
                        { lifetime->engine->removeActionExecutionListener(lifetime->gate); delete lifetime->gate; }
                    }
            }
            if (clean && value->succeeded) event.guids.push_back(value->guid);
            else event.skipped.emplace_back(value->guid, reason.empty()
                ? (value->failure.empty() ? "fishing_cancelled" : value->failure) : reason);
        }
    }
    else for (auto guid : entry.guids) event.skipped.emplace_back(guid, reason);
    PublishJob(event);
    if (clean) { std::lock_guard lock(inbox); journals.erase(entry.id); }
}

class FishingSpellScript : public AllSpellScript
{
public:
    FishingSpellScript() : AllSpellScript("LeeroyGuildFishingSpell",
        {ALLSPELLHOOK_CAN_PREPARE, ALLSPELLHOOK_ON_CAST, ALLSPELLHOOK_ON_CAST_CANCEL}) { }
    bool CanPrepare(Spell* spell, SpellCastTargets const*, AuraEffect const*) override
    {
        if (castFrame.attempt)
        {
            std::lock_guard lock(inbox);
            castFrame.attempt->proof.poisoned = true;
            castFrame = {};
        }
        if (!spell || !FishingSpell(spell->GetSpellInfo()->Id)) return true;
        auto* caster = spell->GetCaster()->ToPlayer();
        if (!caster) return true;
        std::lock_guard lock(inbox);
        auto value = FindAttempt(caster->GetGUID().GetCounter());
        if (!value) return true;
        if (!dispatch || dispatch->use || dispatch->attempt != value || spell->GetSpellInfo()->Id != FISHING_SPELL)
        { value->proof.poisoned = true; return false; }
        return value->proof.Prepare(dispatch->serial, reinterpret_cast<uintptr_t>(spell));
    }
    void OnSpellCast(Spell* spell, Unit* caster, SpellInfo const* info, bool) override
    {
        std::lock_guard lock(inbox);
        if (castFrame.attempt)
        {
            auto frame = std::move(castFrame);
            castFrame = {};
            frame.candidate.Commit(frame.attempt->proof, reinterpret_cast<uintptr_t>(spell));
            return;
        }
        if (caster && info && FishingSpell(info->Id))
            if (auto value = FindAttempt(caster->GetGUID().GetCounter())) value->proof.poisoned = true;
    }
    void OnSpellCastCancel(Spell* spell, Unit* caster, SpellInfo const* info, bool) override
    {
        if (!caster || !info || !FishingSpell(info->Id)) return;
        std::lock_guard lock(inbox);
        if (castFrame.attempt && castFrame.candidate.spell == reinterpret_cast<uintptr_t>(spell))
        {
            if (castFrame.candidate.bobber || castFrame.candidate.poisoned) castFrame.attempt->proof.poisoned = true;
            castFrame = {};
        }
        if (auto value = FindAttempt(caster->GetGUID().GetCounter())) value->proof.Cancel(reinterpret_cast<uintptr_t>(spell));
    }
};
class FishingObjectScript : public AllGameObjectScript
{
public:
    FishingObjectScript() : AllGameObjectScript("LeeroyGuildFishingObject") { }
    void OnGameObjectAddWorld(GameObject* go) override
    {
        if (!go || go->GetGoType() != GAMEOBJECT_TYPE_FISHINGNODE) return;
        std::lock_guard lock(inbox);
        auto value = FindAttempt(go->GetOwnerGUID().GetCounter());
        if (!value) return;
        if (!castFrame.attempt || castFrame.attempt != value || go->GetSpellId() != FISHING_SPELL)
        { value->proof.poisoned = true; return; }
        if (!castFrame.candidate.Add(go->GetGUID().GetRawValue())) value->proof.poisoned = true;
    }
    void OnGameObjectRemoveWorld(GameObject* go) override
    {
        if (!go || go->GetGoType() != GAMEOBJECT_TYPE_FISHINGNODE) return;
        std::lock_guard lock(inbox);
        // Unit::RemoveGameObject clears owner before deferred RemoveFromWorld.
        // Match the exact bound object GUID rather than losing that receipt.
        for (auto const& [id, job] : journals)
        {
            (void)id;
            for (auto const& value : job->attempts)
                if (value->proof.bobber == go->GetGUID().GetRawValue()) value->proof.Remove(go->GetGUID().GetRawValue());
        }
    }
};
class FishingLootScript : public PlayerScript
{
public:
    FishingLootScript() : PlayerScript("LeeroyGuildFishingLoot",
        {PLAYERHOOK_ON_BEFORE_SEND_LOOT, PLAYERHOOK_ON_SPELL_CAST}) { }
    void OnPlayerSpellCast(Player* player, Spell* spell, bool) override
    {
        std::lock_guard lock(inbox);
        if (castFrame.attempt)
        {
            castFrame.attempt->proof.poisoned = true;
            castFrame = {};
        }
        if (!player || !spell || !FishingSpell(spell->GetSpellInfo()->Id)) return;
        auto value = FindAttempt(player->GetGUID().GetCounter());
        if (!value) return;
        FishingCastCandidate candidate;
        if (!candidate.Open(value->proof, reinterpret_cast<uintptr_t>(spell)))
        { value->proof.poisoned = true; return; }
        castFrame.attempt = std::move(value); castFrame.candidate = candidate;
    }
    void OnPlayerBeforeSendLoot(Player* player, ObjectGuid guid, Loot*) override
    {
        if (!player) return;
        std::lock_guard lock(inbox);
        if (auto value = FindAttempt(player->GetGUID().GetCounter()))
        {
            if (!dispatch || !dispatch->use || dispatch->attempt != value || !value->loot.IsEmpty())
                value->proof.poisoned = true;
            else value->loot = guid;
        }
    }
};
}
void RegisterGuildFishing()
{ Realm::RealmModule::Instance().RegisterGuildFishing(Begin, Admission, Step, Outcome); }
void AddGuildFishingScripts()
{ new FishingSpellScript(); new FishingObjectScript(); new FishingLootScript(); }
}
