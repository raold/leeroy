// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "GearUpJob.h"
#include "GuildPorts.h"
#include "LeeroyGearUpPolicy.h"
#include "Realm/RealmModule.h"
#include "Realm/RealmPorts.h"

#include "Bag.h"
#include "Event.h"
#include "Guild.h"
#include "GuildMgr.h"
#include "Log.h"
#include "Map.h"
#include "PlayerbotAI.h"
#include "PlayerbotFactory.h"
#include "Playerbots.h"

#include <array>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <exception>
#include <optional>
#include <string>
#include <utility>

namespace Leeroy::Guild
{
namespace
{
using Clock = std::chrono::steady_clock;

struct GearJob
{
    GearUpCmd command;
    JobEvent result;
    uint32 guild = 0, human = 0;
    std::size_t next = 0;
    Clock::time_point started;
};

// World thread only. No player/group pointers survive a tick. Starting the job does not publish anything:
// the command dispatcher enqueues its ack before a later guild tick emits progress or completion.
std::optional<GearJob> active;

bool InOwnedGuild(uint32 guid, uint32 guildId, uint32 human)
{
    if (!guid || guid == human || !guildId)
        return false;
    ::Guild const* guild = sGuildMgr->GetGuildById(guildId);
    return guild && guild->GetMember(ObjectGuid::Create<HighGuid::Player>(guid));
}

GearUpState StateFor(uint32 guid, GearJob const& job, Player* bot)
{
    GearUpState state;
    state.owned = InOwnedGuild(guid, job.guild, job.human)
        && (!bot || bot->GetGuildId() == job.guild);
    state.online = bot != nullptr;
    if (!bot)
        return state;
    state.bot = GET_PLAYERBOT_AI(bot) && !IsSelfBot(bot);
    state.grouped = bot->GetGroup() || bot->GetGroupInvite();
    state.inBg = bot->InBattleground() || bot->InBattlegroundQueue();
    state.inInstance = !bot->GetMap() || bot->GetMap()->Instanceable() || bot->HasPendingBind();
    state.teleporting = bot->IsBeingTeleported();
    state.dead = bot->isDead();
    state.inCombat = bot->IsInCombat();
    state.inFlight = bot->IsInFlight();
    state.trading = bot->GetTradeData() != nullptr;
    return state;
}

// Every mode: the body checks, then every Realm/training fence (MutationReason, whose holder check is RealmOwns).
std::string SkipFor(uint32 guid, GearJob const& job, Player* bot)
{
    std::string const fence = Realm::RealmModule::Instance().MutationReason(guid);
    if (!job.command.regear)
        return std::string(GearUpFencedSkipReason(StateFor(guid, job, bot), fence));
    RegearTarget target;
    target.human = guid == job.human || guid == HumanGuid();
    target.cast = IsCast(guid);
    target.randomAccount = Realm::IsOnRandomBotAccount(guid);
    return std::string(RegearSkipReason(target, FindOnline(job.human) != nullptr, StateFor(guid, job, bot), fence));
}

std::array<RegearSlotItem, kRegearSlots> Equipped(Player* bot)
{
    static_assert(EQUIPMENT_SLOT_END == kRegearSlots && EQUIPMENT_SLOT_BODY == kRegearSlotBody
        && EQUIPMENT_SLOT_TABARD == kRegearSlotTabard);
    std::array<RegearSlotItem, kRegearSlots> slots{};
    for (uint8 slot = EQUIPMENT_SLOT_START; slot < EQUIPMENT_SLOT_END; ++slot)
        if (Item* item = bot->GetItemByPos(INVENTORY_SLOT_BAG_0, slot))
            if (ItemTemplate const* proto = item->GetTemplate())
                slots[slot] = {item->GetEntry(), proto->ItemLevel};
    return slots;
}

std::array<ObjectGuid, kRegearSlots> EquippedGuids(Player* bot)
{
    std::array<ObjectGuid, kRegearSlots> guids{};
    for (uint8 slot = EQUIPMENT_SLOT_START; slot < EQUIPMENT_SLOT_END; ++slot)
        if (Item* item = bot->GetItemByPos(INVENTORY_SLOT_BAG_0, slot))
            guids[slot] = item->GetGUID();
    return guids;
}

// The backpack and the four bags; never the bank.
uint32 DestroyBagItemsAbove(Player* bot, uint32 cap)
{
    uint32 destroyed = 0;
    auto const check = [&](uint8 bag, uint8 slot)
    {
        Item* item = bot->GetItemByPos(bag, slot);
        ItemTemplate const* proto = item ? item->GetTemplate() : nullptr;
        if (proto && RegearDestroysBagItem(proto->Class == ITEM_CLASS_WEAPON || proto->Class == ITEM_CLASS_ARMOR,
                proto->ItemLevel, cap))
        {
            bot->DestroyItem(bag, slot, true);
            ++destroyed;
        }
    };
    for (uint8 slot = INVENTORY_SLOT_ITEM_START; slot < INVENTORY_SLOT_ITEM_END; ++slot)
        check(INVENTORY_SLOT_BAG_0, slot);
    for (uint8 bag = INVENTORY_SLOT_BAG_START; bag < INVENTORY_SLOT_BAG_END; ++bag)
        if (Bag* container = bot->GetBagByPos(bag))
            for (uint32 slot = 0; slot < container->GetBagSize(); ++slot)
                check(bag, static_cast<uint8>(slot));
    return destroyed;
}

struct CapOutcome
{
    uint32 stripped = 0, restored = 0, returned = 0, empty = 0, destroyed = 0, kept = 0;
};

// The regear only, after its gear pass (autogear bis <cap>, else AutoGear at the cap): that pass puts replaced items in
// the bags, keeps an item when the bags are full or the capped search has nothing for its slot, and a BiS list holds
// items above its tier. Nothing above the regear's ilvl stays equipped or in the bags: strip it, refill from the
// capped search, give back what cannot be filled.
CapOutcome RegearToCap(Player* bot, uint32 cap, std::array<RegearSlotItem, kRegearSlots> const& before)
{
    CapOutcome out;
    out.destroyed = DestroyBagItemsAbove(bot, cap);
    auto const now = Equipped(bot);
    std::array<uint32, kRegearSlots> old{};
    bool refill = false;
    for (uint8 slot = 0; slot < kRegearSlots; ++slot)
    {
        RegearStep const step = RegearSlotStep(slot, before[slot], now[slot], cap);
        if (step == RegearStep::Leave)
            continue;
        old[slot] = RegearOldEntry(step, before[slot], now[slot]);
        if (step == RegearStep::Strip)
        {
            bot->DestroyItem(INVENTORY_SLOT_BAG_0, slot, true);
            ++out.stripped;
        }
        refill = true;
    }
    if (refill)
        PlayerbotFactory::AutoGear(bot, ITEM_QUALITY_EPIC, cap, /*incremental*/ true);
    auto const after = Equipped(bot);
    for (uint8 slot = 0; slot < kRegearSlots; ++slot)
    {
        uint32 const entry = RegearRestoreEntry(after[slot], old[slot]);
        if (!entry)
            continue;
        uint16 dest = 0;
        InventoryResult const result = bot->CanEquipNewItem(slot, dest, entry, false);
        if (result == EQUIP_ERR_OK && bot->EquipNewItem(dest, entry, true))
        {
            ++out.restored;
            bot->AutoUnequipOffhandIfNeed();
        }
        else if (slot != EQUIPMENT_SLOT_OFFHAND || result != EQUIP_ERR_CANT_EQUIP_WITH_TWOHANDED)
            ++out.empty; // An off hand beside a two-hander is meant to be empty.
    }
    out.kept = RegearKept(Equipped(bot), cap);
    return out;
}

// An ordinary gear-up: one capped incremental pass, which fills empty slots and upgrades slots under the cap, never
// autogear bis (which destroys everything) and never a destroyed bag item. What the pass replaced at or above the cap
// goes back in its slot, enchants and all; what this gear-up put on above the cap comes off and the slot is refilled.
CapOutcome GearUpToCap(Player* bot, uint32 cap, std::array<RegearSlotItem, kRegearSlots> const& before,
    std::array<ObjectGuid, kRegearSlots> const& beforeGuids)
{
    CapOutcome out;
    PlayerbotFactory::AutoGear(bot, ITEM_QUALITY_EPIC, cap, /*incremental*/ true);
    auto const now = Equipped(bot);
    bool refill = false;
    for (uint8 slot = 0; slot < kRegearSlots; ++slot)
    {
        GearUpStep const step = GearUpSlotStep(slot, before[slot], now[slot], cap);
        if (step == GearUpStep::Return)
        {
            Item* old = bot->GetItemByGuid(beforeGuids[slot]);
            uint16 dest = 0;
            if (old && !old->IsEquipped() && Player::IsInventoryPos(old->GetPos())
                && bot->CanEquipItem(slot, dest, old, true) == EQUIP_ERR_OK)
            {
                if (bot->GetItemByPos(INVENTORY_SLOT_BAG_0, slot))
                    bot->DestroyItem(INVENTORY_SLOT_BAG_0, slot, true); // made by this pass, at or under the cap
                bot->RemoveItem(old->GetBagSlot(), old->GetSlot(), true);
                bot->EquipItem(dest, old, true);
                bot->AutoUnequipOffhandIfNeed();
                ++out.returned;
            }
        }
        else if (step == GearUpStep::Strip)
        {
            bot->DestroyItem(INVENTORY_SLOT_BAG_0, slot, true); // made by this gear-up, above its cap
            ++out.stripped;
            refill = true;
        }
    }
    if (refill)
        PlayerbotFactory::AutoGear(bot, ITEM_QUALITY_EPIC, cap, /*incremental*/ true);
    auto const after = Equipped(bot);
    for (uint8 slot = 0; slot < kRegearSlots; ++slot)
        if (slot != kRegearSlotBody && slot != kRegearSlotTabard && before[slot].entry && !after[slot].entry
            && (slot != EQUIPMENT_SLOT_OFFHAND || !bot->IsTwoHandUsed()))
            ++out.empty;
    out.kept = RegearKept(after, cap);
    return out;
}

void Finish(std::string const& failure = {})
{
    GearJob& job = *active;
    if (!failure.empty())
    {
        while (job.next < job.command.guids.size())
            job.result.skipped.emplace_back(job.command.guids[job.next++], failure);
        job.result.reason = failure;
    }
    job.result.state = failure.empty() ? "done" : "failed";
    JobEvent result = std::move(job.result);
    active.reset();
    PublishJob(result);
}
}

LeeroyCommandResult HandleGearUp(nlohmann::json const& message)
{
    std::string error;
    auto command = ParseGearUp(message, error);
    if (!command)
        return {false, std::move(error)};
    if (!GuildEnabled())
        return {false, "disabled"};
    if (active)
        return {false, "busy"};

    GearJob job;
    job.guild = LeeroyModule::Instance().GuildId();
    job.human = HumanGuid();
    if (command->regear && FindOnline(job.human))
        return {false, "owner_online"};
    // IsCast is level-80-only. Gear-up must also accept under-level members of this same owned guild; a regear takes
    // only the cast's bots. Validate who each member is before queuing; a non-bot never reaches a factory. A member a
    // Realm lease or a training fence holds is skipped by the tick (SkipFor), never a reason to refuse the batch.
    for (uint32 guid : command->guids)
    {
        if (command->regear)
        {
            RegearTarget target;
            target.human = guid == job.human;
            target.cast = IsCast(guid);
            target.randomAccount = Realm::IsOnRandomBotAccount(guid);
            if (auto const refusal = RegearRefusal(target); !refusal.empty())
                return {false, std::string(refusal) + ":" + std::to_string(guid)};
        }
        if (!InOwnedGuild(guid, job.guild, job.human))
            return {false, "not_cast:" + std::to_string(guid)};
        if (Player* bot = FindOnline(guid))
        {
            if (bot->GetGuildId() != job.guild)
                return {false, "not_cast:" + std::to_string(guid)};
            if (!GET_PLAYERBOT_AI(bot) || IsSelfBot(bot))
                return {false, "not_bot:" + std::to_string(guid)};
        }
    }
    job.command = std::move(*command);
    job.result.id = message.at("id").get<std::string>();
    job.result.kind = "gear_up";
    job.started = Clock::now();
    active = std::move(job);
    return {true, {}};
}

void TickGearUp()
{
    if (!active)
        return;
    GearJob& job = *active;
    if (!GuildEnabled() || job.guild != LeeroyModule::Instance().GuildId() || job.human != HumanGuid())
    {
        Finish("cancelled");
        return;
    }
    if (GearUpExpired(job.started, Clock::now()))
    {
        Finish("timeout");
        return;
    }
    if (job.command.regear && FindOnline(job.human))
    {
        Finish("owner_online"); // his login ends a regear; the mind sends the rest in a later window
        return;
    }

    uint32 const guid = job.command.guids[job.next++];
    Player* bot = FindOnline(guid);
    std::string const skip = SkipFor(guid, job, bot);
    if (!skip.empty())
        job.result.skipped.emplace_back(guid, skip);
    else
    {
        try
        {
            uint32 const level = GearUpLevel(bot->GetLevel(), job.command.levelTo);
            if (bot->GetLevel() < level)
                PlayerbotFactory(bot, level).Randomize(true);

            // Randomize can initialize guild/talent state. Recheck the actual player and ownership after it
            // before running the separate gear mutation; never reuse an AI pointer across that call.
            bot = FindOnline(guid);
            std::string const afterLevel = SkipFor(guid, job, bot);
            if (!afterLevel.empty())
                job.result.skipped.emplace_back(guid, afterLevel);
            else
            {
                auto const before = Equipped(bot);
                auto const beforeGuids = EquippedGuids(bot);
                CapOutcome capped;
                auto const gearFailure = [&]() -> std::string
                {
                    if (!job.command.regear)
                    {
                        capped = GearUpToCap(bot, job.command.ilvl, before, beforeGuids);
                        bot = FindOnline(guid);
                        return bot ? std::string{} : std::string("offline");
                    }
                    PlayerbotAI* ai = GET_PLAYERBOT_AI(bot);
                    // silent suppresses wrapper feedback. The installed BiS action may still TellMaster internally;
                    // changing global chat settings or temporarily changing the bot's master would be unsafe here.
                    bool const applied = ai->DoSpecificAction("autogear bis",
                        Event("autogear bis", std::to_string(job.command.ilvl)), true);
                    bot = FindOnline(guid);
                    if (auto failure = SkipFor(guid, job, bot); !failure.empty())
                        return failure;
                    if (!applied)
                    {
                        PlayerbotFactory::AutoGear(bot, ITEM_QUALITY_EPIC, job.command.ilvl, false);
                        bot = FindOnline(guid);
                    }
                    if (auto failure = SkipFor(guid, job, bot); !failure.empty())
                        return failure;
                    capped = RegearToCap(bot, job.command.ilvl, before);
                    bot = FindOnline(guid);
                    return bot ? std::string{} : std::string("offline");
                }();
                if (!gearFailure.empty())
                    job.result.skipped.emplace_back(guid, gearFailure);
                else
                {
                    bot->SaveToDB(false, false);
                    LeeroyModule::Instance().Cast().MarkDirty();
                    LeeroyModule::Instance().Cast().RequestPush();
                    job.result.guids.push_back(guid);

                    JobEvent progress;
                    progress.id = job.result.id;
                    progress.kind = "gear_up";
                    progress.state = "progress";
                    progress.guid = guid;
                    progress.level = bot->GetLevel();
                    float const average = bot->GetAverageItemLevel();
                    progress.avgIlvl = std::isfinite(average) && average > 0 ? static_cast<uint32>(average) : 0;
                    progress.kept = capped.kept;
                    LOG_INFO("module.leeroy", "mod-leeroy: {} guid={} ilvl={} avg={} stripped={} restored={} "
                        "returned={} empty={} kept={} destroyed={}", job.command.regear ? "regear_member"
                        : "gear_up_member", guid, job.command.ilvl, progress.avgIlvl, capped.stripped, capped.restored,
                        capped.returned, capped.empty, capped.kept, capped.destroyed);
                    PublishJob(progress);
                }
            }
        }
        catch (std::exception const& e)
        {
            LOG_ERROR("module.leeroy", "mod-leeroy: gear_up failed for guid {}: {}", guid, e.what());
            job.result.skipped.emplace_back(guid, "failed");
        }
        catch (...)
        {
            LOG_ERROR("module.leeroy", "mod-leeroy: gear_up failed for guid {}", guid);
            job.result.skipped.emplace_back(guid, "failed");
        }
    }
    if (job.next == job.command.guids.size())
        Finish();
}

void RegisterGearUpHandler() { RegisterCommand("gear_up", HandleGearUp); }
}
