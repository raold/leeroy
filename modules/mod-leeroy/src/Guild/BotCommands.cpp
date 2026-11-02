// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "BotCommands.h"
#include "GuildPorts.h"
#include "LeeroyBotCommandPolicy.h"

#include "AiObjectContext.h"
#include "ChatHelper.h"
#include "Corpse.h"
#include "Event.h"
#include "Formations.h"
#include "Guild.h"
#include "GuildMgr.h"
#include "Item.h"
#include "Log.h"
#include "Map.h"
#include "ObjectMgr.h"
#include "PlayerbotAI.h"
#include "PlayerbotAIConfig.h"
#include "PlayerbotFactory.h"
#include "Playerbots.h"
#include "Spell.h"
#include "SpellMgr.h"

#include <chrono>
#include <deque>
#include <exception>
#include <map>
#include <string>
#include <string_view>
#include <utility>

namespace Leeroy::Guild
{
namespace
{
using Clock = std::chrono::steady_clock;
constexpr std::size_t kMaxPending = 32;
constexpr auto kActionLifetime = std::chrono::seconds(30);

struct BotJob
{
    BotDoCmd command;
    JobEvent result;
    uint32 guild = 0, human = 0;
    Clock::time_point deadline;
};

// World thread only. Queue copied IDs/parameters; resolve all players again when the acknowledged job executes.
std::deque<BotJob> pending;
std::map<uint32, uint32> preferredFlask;
uint32 preferenceGuild = 0, preferenceHuman = 0;

void SyncPreferences()
{
    uint32 const guild = LeeroyModule::Instance().GuildId(), human = HumanGuid();
    if (guild != preferenceGuild || human != preferenceHuman)
    {
        preferredFlask.clear();
        preferenceGuild = guild;
        preferenceHuman = human;
    }
}

LeeroyCommandResult Fail(std::string reason) { return {false, std::move(reason)}; }
ObjectGuid Guid(uint32 guid) { return ObjectGuid::Create<HighGuid::Player>(guid); }

bool OwnedCast(uint32 guid)
{
    if (!guid || guid == HumanGuid() || !IsCast(guid))
        return false;
    ::Guild const* guild = sGuildMgr->GetGuildById(LeeroyModule::Instance().GuildId());
    return guild && guild->GetMember(Guid(guid));
}

bool OwnedTarget(uint32 guid) { return guid && (guid == HumanGuid() || OwnedCast(guid)); }

std::string_view BotFailure(Player* bot)
{
    if (!bot)
        return "offline";
    if (bot->GetGuildId() != LeeroyModule::Instance().GuildId() || !OwnedCast(bot->GetGUID().GetCounter()))
        return "not_cast";
    if (!GET_PLAYERBOT_AI(bot) || IsSelfBot(bot))
        return "not_bot";
    if (bot->isDead())
        return "dead";
    if (!bot->GetMap() || bot->IsBeingTeleported() || bot->IsInFlight() || bot->GetTradeData())
        return "busy";
    return {};
}

bool ChangeStrategies(PlayerbotAI* ai, std::string const& param, BotState state)
{
    std::map<std::string, bool> expected;
    std::string_view rest(param);
    for (;;)
    {
        auto const end = rest.find(',');
        auto const part = rest.substr(0, end);
        std::string name(part.substr(1)); // ParseBotDo already validated the exact signed whitelist.
        if (part.front() == '+' && !ai->GetAiObjectContext()->GetStrategy(name))
            return false; // A class-specific strategy may not exist for this bot.
        expected[name] = part.front() == '+';
        if (end == std::string_view::npos)
            break;
        rest.remove_prefix(end + 1);
    }
    ai->ChangeStrategy(param, state);
    for (auto const& [name, enabled] : expected)
        if (ai->HasStrategy(name, state) != enabled)
            return false;
    return true;
}

std::string_view Cast(Player* bot, PlayerbotAI* ai, BotDoCmd const& command)
{
    Player* target = nullptr;
    if (BotSpellDefaultsToSelf(command.param, command.targetGuid))
        target = bot;
    else if (command.targetGuid)
        target = FindOnline(command.targetGuid);
    else if (Unit* selected = bot->GetSelectedUnit())
        target = selected->ToPlayer();
    if (!target || !OwnedTarget(target->GetGUID().GetCounter()) || target->IsBeingTeleported()
        || !bot->IsFriendlyTo(target))
        return "no_target";
    if (target->GetGUID().GetCounter() != HumanGuid()
        && (target->GetGuildId() != LeeroyModule::Instance().GuildId()
            || !GET_PLAYERBOT_AI(target) || IsSelfBot(target)))
        return "no_target";

    auto* value = ai->GetAiObjectContext()->GetValue<uint32>("spell id", command.param);
    uint32 const spellId = value ? value->Get() : 0;
    SpellInfo const* spell = spellId ? sSpellMgr->GetSpellInfo(spellId) : nullptr;
    if (!spell || !bot->HasSpell(spellId) || bot->IsNonMeleeSpellCast(false)
        || bot->HasUnitState(UNIT_STATE_LOST_CONTROL))
        return "failed";

    if (command.param == "rebirth")
    {
        if (!target->isDead())
            return "no_target";
        if (Corpse* corpse = target->GetCorpse())
        {
            if (corpse->GetType() == CORPSE_BONES || corpse->GetOwnerGUID() != target->GetGUID()
                || !corpse->IsInWorld()
                || corpse->GetMap() != bot->GetMap() || !bot->InSamePhase(corpse))
                return "no_target";
            // Corpse is not a Unit. Use the core's ordinary, non-triggered cast path with the AI's learned rank;
            // normal cooldown, reagent, range, line-of-sight and cast-state validation still runs in prepare().
            if (bot->isMoving())
                return "failed";
            SpellCastTargets targets;
            targets.SetCorpseTarget(corpse);
            if (bot->CastSpell(targets, spell, nullptr, TRIGGERED_NONE) != SPELL_CAST_OK)
                return "failed";
            return {};
        }
    }
    else if (!target->IsAlive())
        return "no_target";

    if (target->GetMap() != bot->GetMap() || !bot->InSamePhase(target))
        return "no_target";
    return ai->CanCastSpell(command.param, target) && ai->CastSpell(command.param, target)
        ? std::string_view{} : std::string_view{"failed"};
}

bool UseStockItem(Player* bot, PlayerbotAI* ai, uint32 entry)
{
    ItemTemplate const* definition = sObjectMgr->GetItemTemplate(entry);
    if (!definition)
        return false;
    // Task17 explicitly chooses a role's flask. Its death-persistent aura already
    // satisfies that exact request, even after consuming the last carried copy.
    // Feasts retain their normal item-use path and may be placed again.
    if (IsBotFlask(entry))
        for (auto const& effect : definition->Spells)
            if (effect.SpellId > 0 && bot->HasAura(static_cast<uint32>(effect.SpellId)))
                return true;
    Item* item = bot->GetItemByEntry(entry); // This core helper searches equipped/inventory bags, never the bank.
    if (!item || item->GetOwnerGUID() != bot->GetGUID() || !item->GetCount()
        || bot->CanUseItem(item) != EQUIP_ERR_OK)
        return false;
    bool castable = false;
    for (auto const& effect : item->GetTemplate()->Spells)
        if (effect.SpellId > 0 && ai->CanCastSpell(static_cast<uint32>(effect.SpellId), bot, false, nullptr, item))
            castable = true;
    if (!castable)
        return false;
    return ai->DoSpecificAction("use", Event("use", ChatHelper::FormatItem(item->GetTemplate())), true);
}

bool BuffFromBags(Player* bot, PlayerbotAI* ai)
{
    std::vector<BotFlask> flasks;
    for (uint32 entry : kBotFlasks)
    {
        ItemTemplate const* definition = sObjectMgr->GetItemTemplate(entry);
        if (!definition)
            continue;
        Item* item = bot->GetItemByEntry(entry);
        bool const carried = item && item->GetOwnerGUID() == bot->GetGUID() && item->GetCount();
        bool active = false;
        for (auto const& effect : definition->Spells)
            if (effect.SpellId > 0 && bot->HasAura(static_cast<uint32>(effect.SpellId)))
                active = true;
        flasks.push_back({entry, carried, active});
    }
    auto const preference = preferredFlask.find(bot->GetGUID().GetCounter());
    auto const selected = ChooseBotFlask(flasks, preference == preferredFlask.end() ? 0 : preference->second);
    if (selected.choice == FlaskChoice::AlreadyActive)
        return true; // Idempotent desired-state no-op: do not consume another stack.
    return selected.choice == FlaskChoice::Use && UseStockItem(bot, ai, selected.item);
}

std::string_view Apply(Player* bot, BotDoCmd const& command)
{
    PlayerbotAI* ai = GET_PLAYERBOT_AI(bot);
    bool applied = false;
    if (command.action == "co" || command.action == "nc")
        applied = ChangeStrategies(ai, command.param,
            command.action == "co" ? BOT_STATE_COMBAT : BOT_STATE_NON_COMBAT);
    else if (command.action == "rti")
    {
        auto* value = ai->GetAiObjectContext()->GetValue<std::string>("rti");
        if (value)
        {
            value->Set(command.param);
            applied = value->Get() == command.param;
        }
    }
    else if (command.action == "formation")
    {
        auto* value = dynamic_cast<FormationValue*>(ai->GetAiObjectContext()->GetValue<Formation*>("formation"));
        applied = value && value->Load(command.param); // The chat action emits an unnecessary whisper.
    }
    else if (command.action == "cast")
        return Cast(bot, ai, command);
    else if (command.action == "use")
    {
        // Resolve from the already validated whitelist rather than parse arbitrary decimal input again.
        for (uint32 item : kStockItems)
            if (command.param == std::to_string(item))
                applied = UseStockItem(bot, ai, item);
    }
    else if (command.action == "do")
    {
        // The pinned "buff" action only lists items. Keep the intended consuming behavior narrowly inside
        // the existing stock whitelist: one unambiguous/preferred flask, from bags, no spawning or elixirs.
        applied = command.param == "buff" && BuffFromBags(bot, ai);
    }
    return applied ? std::string_view{} : std::string_view{"failed"};
}
}

LeeroyCommandResult HandleSetSpec(nlohmann::json const& message)
{
    std::string error;
    auto const command = ParseSetSpec(message, error);
    if (!command)
        return Fail(std::move(error));
    if (!GuildEnabled())
        return Fail("disabled");
    if (!OwnedCast(command->guid))
        return Fail("not_cast:" + std::to_string(command->guid));
    Player* bot = FindOnline(command->guid);
    if (auto const failure = BotFailure(bot); !failure.empty())
        return Fail(std::string(failure));
    if (bot->IsInCombat() || bot->GetGroup() || bot->GetGroupInvite()
        || !bot->GetMap() || bot->GetMap()->Instanceable() || bot->InBattlegroundQueue() || bot->HasPendingBind())
        return Fail("busy");
    uint32 const cls = bot->getClass();
    if (!cls || cls >= MAX_CLASSES || command->specNo >= MAX_SPECNO
        || sPlayerbotAIConfig.PremadeSpecName[cls][command->specNo].empty()
        || sPlayerbotAIConfig.ParsedSpecLinkOrder[cls][command->specNo][80].empty())
        return Fail("bad_spec");
    PlayerbotFactory::InitTalentsBySpecNo(bot, command->specNo, true);
    if (command->glyphs)
        PlayerbotFactory(bot, bot->GetLevel()).InitGlyphs(false);
    bot->SaveToDB(false, false);
    LeeroyModule::Instance().Cast().MarkDirty();
    LeeroyModule::Instance().Cast().RequestPush();
    return {true, {}};
}

LeeroyCommandResult HandleBotDo(nlohmann::json const& message)
{
    std::string error;
    auto command = ParseBotDo(message, error);
    if (!command)
        return Fail(std::move(error));
    if (!GuildEnabled())
        return Fail("disabled");
    for (uint32 guid : command->guids)
        if (!OwnedCast(guid))
            return Fail("not_cast:" + std::to_string(guid));
    if (command->targetGuid && !OwnedTarget(command->targetGuid))
        return Fail("not_cast:" + std::to_string(command->targetGuid));
    if (pending.size() >= kMaxPending)
        return Fail("busy");
    BotJob job;
    job.command = std::move(*command);
    job.result.id = message.at("id").get<std::string>();
    job.result.kind = "bot_do";
    job.guild = LeeroyModule::Instance().GuildId();
    job.human = HumanGuid();
    job.deadline = Clock::now() + kActionLifetime;
    pending.push_back(std::move(job));
    return {true, {}}; // No player mutation or job publication until TickBotCommands, after the ack is queued.
}

void TickBotCommands()
{
    SyncPreferences();
    if (pending.empty())
        return;
    BotJob job = std::move(pending.front());
    pending.pop_front(); // One bounded batch per tick, at most 40 members.
    std::string abort;
    if (!GuildEnabled() || job.guild != LeeroyModule::Instance().GuildId() || job.human != HumanGuid())
        abort = "cancelled";
    else if (Clock::now() >= job.deadline)
        abort = "timeout";
    job.result.state = abort.empty() ? "done" : "failed";
    job.result.reason = abort;
    for (uint32 guid : job.command.guids)
    {
        std::string failure = abort;
        if (failure.empty() && !OwnedCast(guid))
            failure = "not_cast";
        if (failure.empty() && job.command.targetGuid && !OwnedTarget(job.command.targetGuid))
            failure = "no_target";
        if (failure.empty())
        {
            Player* bot = FindOnline(guid);
            failure = BotFailure(bot);
            if (failure.empty())
            {
                try
                {
                    failure = Apply(bot, job.command);
                }
                catch (std::exception const& e)
                {
                    LOG_ERROR("module.leeroy", "mod-leeroy: bot_do failed for guid {}: {}", guid, e.what());
                    failure = "failed";
                }
                catch (...)
                {
                    LOG_ERROR("module.leeroy", "mod-leeroy: bot_do failed for guid {}", guid);
                    failure = "failed";
                }
            }
        }
        if (failure.empty())
            job.result.guids.push_back(guid);
        else
            job.result.skipped.emplace_back(guid, std::move(failure));
    }
    PublishJob(job.result);
}

LeeroyCommandResult HandleStockItems(nlohmann::json const& message)
{
    SyncPreferences();
    std::string error;
    auto const command = ParseStockItems(message, error);
    if (!command)
        return Fail(std::move(error));
    if (!GuildEnabled())
        return Fail("disabled");
    if (!OwnedCast(command->guid))
        return Fail("not_cast:" + std::to_string(command->guid));
    Player* bot = FindOnline(command->guid);
    if (auto const failure = BotFailure(bot); !failure.empty())
        return Fail(std::string(failure));
    for (uint32 entry : command->items)
        if (!sObjectMgr->GetItemTemplate(entry))
            return Fail("bad field: items");

    bool changed = false, full = false;
    for (uint32 entry : command->items)
    {
        uint32 const missing = StockMissing(bot->GetItemCount(entry, false), command->count);
        if (!missing)
            continue;
        ItemPosCountVec dest;
        uint32 noSpace = 0;
        InventoryResult const result = bot->CanStoreNewItem(NULL_BAG, NULL_SLOT, dest, entry, missing, &noSpace);
        uint64 planned = 0;
        for (auto const& slot : dest)
            planned += slot.count;
        auto const count = StockPlanCount(missing, noSpace, planned,
            result == EQUIP_ERR_OK, result == EQUIP_ERR_INVENTORY_FULL);
        if (!count || (*count && !bot->StoreNewItem(dest, entry, true)))
        {
            if (changed)
                bot->SaveToDB(false, false);
            return Fail("failed");
        }
        changed = changed || *count != 0;
        full = full || noSpace != 0;
    }
    if (changed)
        bot->SaveToDB(false, false);
    if (!full)
    {
        uint32 flask = 0;
        std::size_t flaskTypes = 0;
        for (uint32 entry : command->items)
            if (IsBotFlask(entry))
            {
                flask = entry;
                ++flaskTypes;
            }
        if (flaskTypes == 1)
            preferredFlask[command->guid] = flask;
    }
    return full ? Fail("bags_full") : LeeroyCommandResult{true, {}};
}

void RegisterBotCommandHandlers()
{
    RegisterCommand("set_spec", HandleSetSpec);
    RegisterCommand("bot_do", HandleBotDo);
    RegisterCommand("stock_items", HandleStockItems);
}
}
