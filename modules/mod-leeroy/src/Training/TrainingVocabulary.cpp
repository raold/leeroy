// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "TrainingCommands.h"
#include "TrainingTap.h"
#include "GuildPorts.h"
#include "LeeroyTrainingVocabulary.h"
#include "AiObjectContext.h"
#include "CustomStrategy.h"
#include "PlayerbotAI.h"
#include "Playerbots.h"
#include "RandomPlayerbotMgr.h"

#if !defined(LEEROY_HAS_DUNGEON_CLEAR)
#error "LEEROY_HAS_DUNGEON_CLEAR comes from mod-leeroy.cmake; re-run cmake"
#endif
#if LEEROY_HAS_DUNGEON_CLEAR
#include "Ai/Dungeon/DungeonClear/Settings/DcSettings.h"
#endif

#include <cmath>
#include <algorithm>
#include <deque>
#include <map>
#include <set>

namespace Leeroy::Training
{
namespace
{
constexpr std::size_t kMaxVocabJobs = 64, kSpellLookupsPerTick = 64, kMaxPrimed = 1024;
struct PendingVocab
{
    VocabJob plan;
    std::vector<std::string> askedTriggers;
    Vocabulary data;
    uint32 guild = 0, human = 0;
    std::size_t nextAction = 0;
    bool loaded = false;
};
std::deque<PendingVocab> pending;
std::map<std::string, std::string, std::less<>> primed;

bool Bot(Player* player)
{
    return player && player->IsInWorld() && !player->IsDuringRemoveFromWorld() && GET_PLAYERBOT_AI(player)
        && !IsSelfBot(player) && player->GetGUID().GetCounter() != Guild::HumanGuid();
}

Player* Probe(uint32 cls, uint32 guid)
{
    if (guid)
    {
        auto* player = Guild::FindOnline(guid);
        return Bot(player) && (!cls || player->getClass() == cls) ? player : nullptr;
    }
    auto cast = Guild::CastMembers();
    std::sort(cast.begin(), cast.end());
    for (auto low : cast)
        if (auto* player = Guild::FindOnline(low); Bot(player) && player->getClass() == cls
            && player->GetGuildId() == LeeroyModule::Instance().GuildId()) return player;
    for (auto it = sRandomPlayerbotMgr.GetPlayerBotsBegin(); it != sRandomPlayerbotMgr.GetPlayerBotsEnd(); ++it)
        if (Bot(it->second) && it->second->getClass() == cls) return it->second;
    return nullptr;
}

void Failed(PendingVocab const& item, std::string reason)
{
    Guild::JobEvent event;
    event.id = item.data.id;
    event.kind = "get_vocab";
    event.state = "failed";
    event.reason = std::move(reason) + ':' + std::to_string(item.data.cls) + ':' + std::to_string(item.data.probe);
    Guild::PublishJob(event);
}

void Finish(PendingVocab item)
{
    // Keep the request identity available if queue admission throws after the payload has moved.
    PendingVocab failure;
    failure.data.id = item.data.id;
    failure.data.cls = item.data.cls;
    failure.data.probe = item.data.probe;
    try
    {
        TrainingTap::Instance().EnqueueEncode([data = std::move(item.data)]
        {
            if (auto line = EncodeVocabulary(data, NowUnixMs())) return std::vector<std::string>{std::move(*line)};
            Guild::JobEvent failure;
            failure.id = data.id;
            failure.kind = "get_vocab";
            failure.state = "failed";
            failure.reason = "vocab_too_large:" + std::to_string(data.cls) + ':' + std::to_string(data.probe);
            return std::vector<std::string>{EncodeEvent(NowUnixMs(), Guild::MakeJobEvent(failure))};
        });
    }
    catch (...) { Failed(failure, "encoder_unavailable"); }
}
}

bool IsPrimed(std::string const& qualifier)
{
    auto expected = primed.find(qualifier);
    auto actual = CustomStrategy::actionLinesCache.find(qualifier);
    return expected != primed.end() && actual != CustomStrategy::actionLinesCache.end()
        && actual->second == expected->second && !actual->second.empty();
}

bool RepairPrimedForRestore(std::string const& qualifier)
{
    // Cleanup may run with training off. Repair only our recorded definition before GetStrategy/ChangeStrategy
    // can enter InitTriggers; no probe, DB lookup, new name, or public mutation-gate exception is involved.
    return RestorePrimedEntry(CustomStrategy::actionLinesCache, primed, qualifier) && IsPrimed(qualifier);
}

LeeroyCommandResult HandleCustomStrategy(nlohmann::json const& message)
{
    std::string error;
    auto command = ParseCustomStrategy(message, error);
    if (!command) return {false, error};
    if (!TrainingAllowed()) return {false, "training_off"};
    auto* bot = Probe(command->cls, command->probe);
    if (!bot) return {false, "no_bot:" + std::to_string(command->cls)};
    auto* context = GET_PLAYERBOT_AI(bot)->GetAiObjectContext();
    auto const actions = context->GetSupportedActions();
    for (auto const& line : command->lines)
    {
        auto arrow = line.find('>'), bang = line.find('!');
        auto trigger = line.substr(0, arrow), action = line.substr(arrow + 1, bang - arrow - 1);
        if (!context->GetTrigger(trigger)) return {false, "unknown_trigger:" + trigger};
        if (!actions.count(action)) return {false, "unknown_action:" + action};
        auto* value = context->GetValue<uint32>("spell id", action);
        auto spell = value ? value->Get() : 0;
        if (!spell || !bot->HasSpell(spell)) return {false, "not_spell:" + action};
    }
    auto wanted = FormatCacheEntry(command->name, command->lines);
    auto& cache = CustomStrategy::actionLinesCache;
    auto existing = cache.find(command->name);
    auto receipt = primed.find(command->name);
    auto decision = PrimeOwnedEntry(existing == cache.end() ? "" : existing->second, wanted,
        receipt == primed.end() ? "" : receipt->second);
    if (decision == PrimeDecision::Taken) return {false, "name_taken"};
    if (!primed.count(command->name) && primed.size() >= kMaxPrimed) return {false, "busy"};
    // World barrier only; never use GetStrategy before this nonempty entry prevents InitTriggers' DB fallback.
    primed[command->name] = wanted;
    if (decision == PrimeDecision::Write) cache[command->name] = std::move(wanted);
    return {true, {}};
}

LeeroyCommandResult HandleGetVocab(nlohmann::json const& message)
{
    std::string error;
    auto command = ParseGetVocab(message, error);
    if (!command) return {false, error};
    if (!Guild::GuildEnabled()) return {false, "guild_off"};
    auto jobs = PlanVocab(*command, Guild::FindOnline(Guild::HumanGuid()) != nullptr);
    if (pending.size() + jobs.size() > kMaxVocabJobs) return {false, "busy"};
    for (auto const& job : jobs)
    {
        PendingVocab item;
        item.plan = job;
        item.askedTriggers = command->triggers;
        item.data.id = message.at("id").get<std::string>();
        item.guild = LeeroyModule::Instance().GuildId();
        item.human = Guild::HumanGuid();
        pending.push_back(std::move(item));
    }
    return {true, {}};
}

void TickVocab()
{
    if (pending.empty()) return;
    auto& item = pending.front();
    if (!Guild::GuildEnabled() || item.guild != LeeroyModule::Instance().GuildId() || item.human != Guild::HumanGuid())
    {
        Failed(item, "cancelled");
        pending.pop_front();
        return;
    }
    try
    {
        if (!item.loaded)
        {
            item.loaded = true;
            if (item.plan.kind == VocabKind::Server)
            {
#if LEEROY_HAS_DUNGEON_CLEAR
                auto centi = [](double value) { return static_cast<std::int64_t>(std::llround(value * 100.0)); };
                for (auto key : kDcKeys)
                    if (auto* def = FindDcSetting(key))
                        item.data.dc.push_back({std::string(key), centi(def->minVal), centi(def->maxVal),
                            centi(DcSettings::GetEffectiveRaw(ObjectGuid::Empty, *def)),
                            DcHasRaidDefault(*def) ? centi(def->raidVal) : -1,
                            DcHasHeroicDefault(*def) ? centi(def->heroicVal) : -1});
#endif
                std::set<std::string> names;
                for (auto name : Guild::kBotStrategyAdd) names.emplace(name);
                for (auto name : Guild::kBotStrategyRemove) names.emplace(name);
                item.data.whitelist.assign(names.begin(), names.end());
            }
            else
            {
                auto* bot = Probe(item.plan.cls, item.plan.probe);
                item.data.cls = item.plan.cls;
                item.data.probe = item.plan.probe;
                if (bot)
                {
                    item.data.cls = bot->getClass();
                    item.data.probe = bot->GetGUID().GetCounter();
                    auto* context = GET_PLAYERBOT_AI(bot)->GetAiObjectContext();
                    auto strategies = context->GetSupportedStrategies(), actions = context->GetSupportedActions();
                    item.data.strategies.assign(strategies.begin(), strategies.end());
                    item.data.actions.assign(actions.begin(), actions.end());
                    for (auto const& trigger : item.askedTriggers)
                        if (context->GetTrigger(trigger)) item.data.triggers.push_back(trigger);
                }
            }
        }
        if (item.plan.wantSpells && item.data.probe)
        {
            // His login can happen between request acceptance and any of these bounded scan slices.
            if (Guild::FindOnline(Guild::HumanGuid()))
            {
                item.plan.wantSpells = false;
                item.data.spells.clear();
                item.data.spellsChecked = false;
            }
            else
            {
                auto* bot = Probe(item.data.cls, item.data.probe);
                if (!bot)
                {
                    Failed(item, "probe_offline");
                    pending.pop_front();
                    return;
                }
                auto* context = GET_PLAYERBOT_AI(bot)->GetAiObjectContext();
                auto end = std::min(item.data.actions.size(), item.nextAction + kSpellLookupsPerTick);
                for (; item.nextAction < end; ++item.nextAction)
                {
                    auto const& action = item.data.actions[item.nextAction];
                    auto* value = context->GetValue<uint32>("spell id", action);
                    auto spell = value ? value->Get() : 0;
                    if (spell && bot->HasSpell(spell)) item.data.spells.push_back(action);
                }
                if (item.nextAction < item.data.actions.size()) return;
                item.data.spellsChecked = true;
            }
        }
        auto done = std::move(item);
        pending.pop_front();
        Finish(std::move(done));
    }
    catch (...)
    {
        // No empty success on a partial lookup failure; later independent jobs still run.
        if (!pending.empty())
        {
            Failed(pending.front(), "vocab_failed");
            pending.pop_front();
        }
    }
}

void ResetTrainingVocabulary() { pending.clear(); } // Cached immutable definitions can still be held by bot engines.
}
