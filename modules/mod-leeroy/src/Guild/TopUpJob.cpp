// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "TopUpJob.h"
#include "GuildPorts.h"
#include "LeeroyTopUpPolicy.h"

#include "Bag.h"
#include "Item.h"
#include "Log.h"
#include "Map.h"
#include "ObjectMgr.h"
#include "PlayerbotFactory.h"
#include "Playerbots.h"
#include "RandomItemMgr.h"

#include <chrono>
#include <exception>
#include <unordered_map>

namespace Leeroy::Guild
{
namespace
{
std::uint32_t untilPass = kTopUpEveryMs, lastHandled = 0, ownerGuild = 0, ownerHuman = 0;
std::unordered_map<std::uint32_t, std::uint64_t> lastTopUp;

template<class Visitor>
void VisitBags(Player* bot, Visitor visit)
{
    for (uint8 slot = INVENTORY_SLOT_ITEM_START; slot < INVENTORY_SLOT_ITEM_END; ++slot)
        if (Item* item = bot->GetItemByPos(INVENTORY_SLOT_BAG_0, slot))
            visit(item);
    for (uint8 slot = INVENTORY_SLOT_BAG_START; slot < INVENTORY_SLOT_BAG_END; ++slot)
        if (Bag* bag = bot->GetBagByPos(slot))
            for (uint32 inside = 0; inside < bag->GetBagSize(); ++inside)
                if (Item* item = bag->GetItemByPos(static_cast<uint8>(inside)))
                    visit(item);
}

void FillTo(Player* bot, uint32 entry, uint32 target)
{
    if (!sObjectMgr->GetItemTemplate(entry))
        return;
    uint32 const missing = TopUpMissing(bot->GetItemCount(entry, false), target);
    if (!missing)
        return;
    ItemPosCountVec dest;
    InventoryResult result = bot->CanStoreNewItem(NULL_BAG, NULL_SLOT, dest, entry, missing);
    if ((result == EQUIP_ERR_OK || result == EQUIP_ERR_INVENTORY_FULL) && !dest.empty())
        bot->StoreNewItem(dest, entry, true);
}

void AddAmmo(Player* bot)
{
    if (bot->getClass() != CLASS_HUNTER && bot->getClass() != CLASS_ROGUE && bot->getClass() != CLASS_WARRIOR)
        return;
    Item* ranged = bot->GetItemByPos(INVENTORY_SLOT_BAG_0, EQUIPMENT_SLOT_RANGED);
    ItemTemplate const* weapon = ranged ? ranged->GetTemplate() : nullptr;
    if (!weapon)
        return;
    uint32 subclass;
    if (weapon->SubClass == ITEM_SUBCLASS_WEAPON_GUN)
        subclass = ITEM_SUBCLASS_BULLET;
    else if (weapon->SubClass == ITEM_SUBCLASS_WEAPON_BOW || weapon->SubClass == ITEM_SUBCLASS_WEAPON_CROSSBOW)
        subclass = ITEM_SUBCLASS_ARROW;
    else
        return;
    uint32 const entry = sRandomItemMgr.GetAmmo(bot->GetLevel(), subclass);
    if (!entry)
        return;
    // Factory::InitAmmo destroys obsolete ammunition. This path keeps every existing stack.
    FillTo(bot, entry, bot->getClass() == CLASS_HUNTER ? 6000 : 1000);
    if (bot->GetItemCount(entry, false))
        bot->SetAmmo(entry);
}

void AddReagents(Player* bot, PlayerbotFactory& factory)
{
    if (bot->getClass() != CLASS_SHAMAN)
    {
        factory.InitReagents(); // These class branches only add missing counts in the installed revision.
        return;
    }
    // Factory::InitReagents deletes obsolete shaman totems, including bank items. Keep them instead.
    bool relic = false;
    auto checkRelic = [&relic](Item* item)
    {
        ItemTemplate const* info = item ? item->GetTemplate() : nullptr;
        relic |= info && info->Class == ITEM_CLASS_ARMOR && info->SubClass == ITEM_SUBCLASS_ARMOR_TOTEM;
    };
    checkRelic(bot->GetItemByPos(INVENTORY_SLOT_BAG_0, EQUIPMENT_SLOT_RANGED));
    VisitBags(bot, checkRelic);
    if (!relic)
        for (uint32 entry : {5175u, 5176u, 5177u, 5178u})
            FillTo(bot, entry, 1);
    FillTo(bot, 17030, 20); // Ankh; this job handles level-80 cast members only.
}

bool Alcohol(ItemTemplate const* info)
{
    switch (info->Spells[0].SpellId)
    {
        case 11007:
        case 11008:
        case 11009:
        case 11629:
        case 50986:
            return true;
        default:
            return false;
    }
}

void AddFood(Player* bot)
{
    if (GET_PLAYERBOT_AI(bot)->HasCheat(BotCheatMask::food))
        return;
    bool food = false, drink = false;
    VisitBags(bot, [&](Item* item)
    {
        ItemTemplate const* info = item->GetTemplate();
        if (!info || info->Class != ITEM_CLASS_CONSUMABLE || info->SubClass != ITEM_SUBCLASS_FOOD
            || Alcohol(info) || bot->CanUseItem(item) != EQUIP_ERR_OK)
            return;
        food |= info->Spells[0].SpellCategory == 11;
        drink |= info->Spells[0].SpellCategory == 59;
    });
    // Current power type is rage/energy for a shifted druid, but that bot still needs drink.
    for (uint32 category : TopUpFoodCategories(food, drink, bot->GetMaxPower(POWER_MANA)))
    {
        ItemTemplate const* best = nullptr;
        uint32 entry = 0;
        for (auto const& candidate : *sObjectMgr->GetItemTemplateStore())
        {
            ItemTemplate const* info = &candidate.second;
            if (info->Class != ITEM_CLASS_CONSUMABLE || info->SubClass != ITEM_SUBCLASS_FOOD
                || info->Spells[0].SpellCategory != category || info->Bonding != NO_BIND
                || info->RequiredLevel > bot->GetLevel()
                || static_cast<int32>(info->RequiredLevel) < static_cast<int32>(bot->GetLevel()) - 9
                || info->Area || info->Map || info->RequiredCityRank || info->RequiredHonorRank
                || Alcohol(info) || bot->CanUseItem(info) != EQUIP_ERR_OK)
                continue;
            // Prefer the highest usable tier; equal tiers use a stable entry rather than hash iteration order.
            if (!best || info->RequiredLevel > best->RequiredLevel
                || (info->RequiredLevel == best->RequiredLevel && candidate.first < entry))
            {
                best = info;
                entry = candidate.first;
            }
        }
        if (best)
            FillTo(bot, entry, best->GetMaxStackSize());
    }
}
}

void TickTopUp(std::uint32_t diffMs)
{
    if (!GuildEnabled() || !KeepCast())
    {
        untilPass = kTopUpEveryMs;
        return;
    }
    if (ownerGuild != LeeroyModule::Instance().GuildId() || ownerHuman != HumanGuid())
    {
        ownerGuild = LeeroyModule::Instance().GuildId();
        ownerHuman = HumanGuid();
        lastTopUp.clear();
        lastHandled = 0;
    }
    if (diffMs < untilPass)
    {
        untilPass -= diffMs;
        return;
    }
    // A stalled world tick must not create a catch-up burst of factory operations.
    untilPass = kTopUpEveryMs - (diffMs - untilPass) % kTopUpEveryMs;
    auto members = TopUpOrder(CastMembers(), lastHandled);
    for (auto it = lastTopUp.begin(); it != lastTopUp.end();)
        if (!IsCast(it->first))
            it = lastTopUp.erase(it);
        else
            ++it;
    auto const now = static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count());
    uint32 attempted = 0;
    for (uint32 guid : members)
    {
        lastHandled = guid;
        Player* bot = FindOnline(guid);
        if (!bot || !GET_PLAYERBOT_AI(bot) || IsSelfBot(bot))
            continue;
        TopUpState state;
        state.isCast = IsCast(guid) && guid != ownerHuman && bot->GetGuildId() == ownerGuild && bot->GetLevel() >= 80;
        state.online = true;
        state.inCombat = bot->IsInCombat();
        state.dead = bot->isDead();
        state.grouped = bot->GetGroup() || bot->GetGroupInvite();
        state.onInstanceMap = !bot->GetMap() || bot->GetMap()->Instanceable() || bot->HasPendingBind();
        state.inBattleground = bot->InBattleground() || bot->InBattlegroundQueue();
        state.teleporting = bot->IsBeingTeleported();
        state.inFlight = bot->IsInFlight();
        state.trading = bot->GetTradeData() != nullptr;
        state.nowMs = now;
        auto const previous = lastTopUp.find(guid);
        state.lastTopUpMs = previous == lastTopUp.end() ? 0 : previous->second;
        if (!TopUpAllowed(state, kTopUpCooldownMs))
            continue;
        ++attempted;
        lastTopUp[guid] = now; // Bound retries too if a factory helper fails partway through.
        try
        {
            PlayerbotFactory factory(bot, bot->GetLevel());
            AddAmmo(bot);
            AddReagents(bot, factory);
            factory.InitPotions();
            AddFood(bot); // InitFood adds both categories even when only one is missing.
            factory.InitConsumables();
            bot->DurabilityRepairAll(false, 1.0f, false);
            LOG_INFO("module.leeroy", "leeroy: topped up consumables for {} ({})", bot->GetName(), guid);
        }
        catch (std::exception const&)
        {
            LOG_ERROR("module.leeroy", "leeroy: consumable top-up failed for {}", guid);
        }
        if (attempted >= kTopUpPerTick)
            break;
    }
}
}
