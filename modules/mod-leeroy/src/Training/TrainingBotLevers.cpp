// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "TrainingCommands.h"
#include "TrainingTap.h"
#include "DummyTrialJob.h"
#include "GuildPorts.h"
#include "OfflineRaidJob.h"
#include "LeeroyTrainingProtocol.h"
#include "AiObjectContext.h"
#include "Formations.h"
#include "Group.h"
#include "Log.h"
#include "PlayerbotAI.h"
#include "Playerbots.h"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <limits>
#include <list>
#include <map>

namespace Leeroy::Training
{
namespace
{
std::map<uint32, BotJournal> journals; // World thread only; logout discards the retired AI's journal.
BotState State(Engine engine) { return engine == Engine::Combat ? BOT_STATE_COMBAT : BOT_STATE_NON_COMBAT; }
BotJournal::Present Present(PlayerbotAI* ai)
{
    return [ai](Engine engine, std::string_view name) { return ai->HasStrategy(std::string(name), State(engine)); };
}

template<class T> bool Number(std::string_view text, T& out)
{
    auto result = std::from_chars(text.data(), text.data() + text.size(), out);
    return result.ec == std::errc{} && result.ptr == text.data() + text.size();
}

std::optional<std::string> ReadValue(PlayerbotAI* ai, std::string const& name)
{
    auto* context = ai->GetAiObjectContext();
    if (name == "mana save level")
    {
        auto* value = context->GetValue<double>(name);
        if (!value || !std::isfinite(value->Get())) return {};
        char text[64];
        auto result = std::to_chars(text, text + sizeof(text), value->Get(),
            std::chars_format::general, std::numeric_limits<double>::max_digits10);
        if (result.ec != std::errc{}) return {};
        return std::string(text, result.ptr); // Save() uses lossy default ostream precision in this playerbots version.
    }
    if (name == "wait for attack time")
    {
        auto* value = context->GetValue<uint8>(name);
        if (value) return std::to_string(value->Get());
    }
    if (name == "focus heal targets")
    {
        auto* value = context->GetValue<std::list<ObjectGuid>>(name);
        if (!value) return {};
        std::string text;
        for (auto guid : value->Get())
        {
            if (!guid.IsPlayer()) return {}; // Do not lose a non-player baseline by serializing just its low part.
            if (!text.empty()) text += ',';
            text += std::to_string(guid.GetCounter());
        }
        return text;
    }
    if (name == "formation")
    {
        auto* value = dynamic_cast<FormationValue*>(context->GetValue<Formation*>(name));
        if (value) return value->Save();
    }
    return {};
}

bool WriteValue(Player* bot, std::string const& name, std::string const& text, bool restoring)
{
    auto* context = GET_PLAYERBOT_AI(bot)->GetAiObjectContext();
    if (name == "mana save level")
    {
        double parsed = 0;
        auto* value = context->GetValue<double>(name);
        if (!value || !Number(text, parsed) || !std::isfinite(parsed)) return false;
        value->Set(parsed);
        return value->Get() == parsed;
    }
    if (name == "wait for attack time")
    {
        unsigned parsed = 0;
        auto* value = context->GetValue<uint8>(name);
        if (!value || !Number(text, parsed) || parsed > 255) return false;
        value->Set(static_cast<uint8>(parsed));
        return value->Get() == parsed;
    }
    if (name == "focus heal targets")
    {
        auto* value = context->GetValue<std::list<ObjectGuid>>(name);
        if (!value) return false;
        std::list<ObjectGuid> targets;
        std::string_view rest(text);
        while (!rest.empty())
        {
            auto end = rest.find(',');
            uint32 low = 0;
            if (!Number(rest.substr(0, end), low) || !low) return false;
            auto guid = ObjectGuid::Create<HighGuid::Player>(low);
            if (!restoring && (!bot->GetGroup() || !bot->GetGroup()->IsMember(guid))) return false;
            targets.push_back(guid);
            if (end == std::string_view::npos) break;
            rest.remove_prefix(end + 1);
            if (rest.empty()) return false;
        }
        value->Set(targets);
        return value->Get() == targets;
    }
    if (name == "formation")
    {
        auto* value = dynamic_cast<FormationValue*>(context->GetValue<Formation*>(name));
        return value && value->Load(text) && value->Save() == text;
    }
    return false;
}

bool RestoreOne(Player* bot, std::string const& name, bool stockFallback, uint32& changes)
{
    auto* ai = GET_PLAYERBOT_AI(bot);
    auto guid = bot->GetGUID().GetCounter();
    auto found = journals.find(guid);
    BotJournal candidate = found == journals.end() ? BotJournal{} : found->second;
    auto const values = candidate.Values();
    bool const restoreValue = stockFallback || std::find(values.begin(), values.end(), name) != values.end();
    auto strategy = candidate.RestoreStrategy(name, Present(ai));
    for (auto const& [engine, param] : strategy)
    {
        if (param.front() != '+') continue;
        auto strategyName = param.substr(1);
        if (strategyName.starts_with("custom::") && !RepairPrimedForRestore(strategyName.substr(8))) return false;
        if (!ai->GetAiObjectContext()->GetStrategy(strategyName)) return false;
    }
    for (auto const& [engine, param] : strategy)
    {
        ai->ChangeStrategy(param, State(engine));
        if (ai->HasStrategy(param.substr(1), State(engine)) != (param.front() == '+')) return false;
        ++changes;
    }
    // Check the final state too: adding a sibling can evict an earlier requested name.
    for (auto const& [engine, param] : strategy)
        if (ai->HasStrategy(param.substr(1), State(engine)) != (param.front() == '+')) return false;
    if (auto value = restoreValue ? candidate.RestoreValue(name) : std::optional<std::string>{})
    {
        auto before = ReadValue(ai, name);
        if (!before || !WriteValue(bot, name, *value, true)) return false;
        if (*before != *value) ++changes;
    }
    if (found != journals.end())
    {
        if (candidate.Empty()) journals.erase(found);
        else found->second = std::move(candidate);
    }
    return true;
}
}

bool TrainingAllowed()
{
    auto& tap = TrainingTap::Instance();
    return Guild::GuildEnabled() && tap.TrainingOn() && !tap.TrainingCleanupPending() && !TrainingCleanupPending();
}
BotJournal& JournalOf(uint32 guid) { return journals[guid]; }
void JournalReset(uint32 guid, std::vector<std::string_view> const& values)
{
    if (auto found = journals.find(guid); found != journals.end())
    {
        found->second.Reset(values);
        if (found->second.Empty()) journals.erase(found);
    }
}
void JournalForget(uint32 guid) { journals.erase(guid); }
bool HasTrainingJournal(uint32 guid) { return journals.contains(guid); }
void JournalValueReplaced(uint32 guid, std::string_view name)
{
    if (auto found = journals.find(guid); found != journals.end())
    {
        found->second.RestoreValue(name); // Discard ownership only; the newer P1d value is already verified.
        if (found->second.Empty()) journals.erase(found);
    }
}

bool RestoreJournaled(Player* bot, std::string_view name, uint32* changes)
{
    if (!bot || !GET_PLAYERBOT_AI(bot) || IsSelfBot(bot)) return false;
    uint32 count = 0;
    bool success = true;
    if (name == "*")
    {
        auto found = journals.find(bot->GetGUID().GetCounter());
        if (found == journals.end()) return true; // Wildcard never imposes stock values on an unowned bot.
        auto names = found->second.Strategies();
        auto values = found->second.Values();
        for (auto const& value : values)
            if (std::find(names.begin(), names.end(), value) == names.end()) names.push_back(value);
        for (auto const& entry : names) success = RestoreOne(bot, entry, false, count) && success;
    }
    else success = RestoreOne(bot, std::string(name), true, count);
    if (changes) *changes += count;
    if (!success) RequireTrainingCleanup();
    return success;
}

std::string_view ApplyTrainingStrategies(Player* bot, std::string const& param, Engine engine, bool journalAll)
{
    if (journalAll && !TrainingAllowed()) return "training_off";
    if (!TierTwoAllowed(TrainingAllowed(), engine == Engine::Combat ? "co" : "nc", param)) return "training_off";
    auto* ai = GET_PLAYERBOT_AI(bot);
    std::vector<std::string> parts;
    std::string_view rest(param);
    for (;;)
    {
        auto end = rest.find(',');
        parts.emplace_back(rest.substr(0, end));
        auto const& part = parts.back();
        auto name = part.substr(1);
        if (part.front() == '+' && name.starts_with("custom::") && !IsPrimed(name.substr(8))) return "not_primed";
        if (part.front() == '+' && !ai->GetAiObjectContext()->GetStrategy(name)) return "failed";
        if (end == std::string_view::npos) break;
        rest.remove_prefix(end + 1);
    }
    auto guid = bot->GetGUID().GetCounter();
    // Snapshot every affected group before touching any of them. A failure leaves a complete retryable journal.
    for (auto const& part : parts)
        if ((journalAll || IsTierTwo("co", part)) && TrainingAllowed())
            JournalOf(guid).NoteStrategy(engine, part.substr(1), Present(ai));
    ai->ChangeStrategy(param, State(engine));
    for (auto const& part : parts)
        if (ai->HasStrategy(part.substr(1), State(engine)) != (part.front() == '+'))
        {
            RequireTrainingCleanup();
            return "failed";
        }
    if (!TrainingAllowed()) // An explicit off-state removal is authoritative; cleanup must not re-add it later.
        if (auto found = journals.find(guid); found != journals.end())
        {
            for (auto const& part : parts) found->second.RestoreStrategy(part.substr(1), Present(ai));
            if (found->second.Empty()) journals.erase(found);
        }
    return {};
}

std::string_view ApplyTrainingValue(Player* bot, std::string const& param)
{
    if (!TierTwoAllowed(TrainingAllowed(), "value", param)) return "training_off";
    for (auto name : kTrainingValues)
        if (param.starts_with(std::string(name) + ' '))
        {
            auto text = param.substr(name.size() + 1);
            auto before = ReadValue(GET_PLAYERBOT_AI(bot), std::string(name));
            if (!before) return "failed";
            // Validate target ownership before journaling or changing the typed value.
            if (name == "focus heal targets")
            {
                std::string_view rest(text);
                while (!rest.empty())
                {
                    auto end = rest.find(',');
                    uint32 low = 0;
                    if (!Number(rest.substr(0, end), low) || !bot->GetGroup()
                        || !bot->GetGroup()->IsMember(ObjectGuid::Create<HighGuid::Player>(low))) return "no_target";
                    if (end == std::string_view::npos) break;
                    rest.remove_prefix(end + 1);
                }
            }
            JournalOf(bot->GetGUID().GetCounter()).NoteValue(name, *before);
            if (WriteValue(bot, std::string(name), text, false)) return {};
            RequireTrainingCleanup();
            return "failed";
        }
    return "failed";
}

bool RestoreTrainingBots()
{
    std::vector<uint32> guids;
    for (auto const& [guid, journal] : journals) guids.push_back(guid);
    uint32 restored = 0, failed = 0;
    for (auto guid : guids)
    {
        if (DummyTrialOwns(guid)) continue; // Trial-owned return validates the session/master before touching its AI.
        // The benchmark adapter verifies its captured body/controller before restoring and returning it.
        if (Guild::OfflineRaidOwns(guid)) continue;
        try
        {
            auto* bot = Guild::FindOnline(guid);
            if (!bot) JournalForget(guid);
            else if (RestoreJournaled(bot, "*")) ++restored;
            else ++failed;
        }
        catch (...) { ++failed; } // Keep the original journal; another member's restoration still proceeds.
    }
    if (restored || failed)
        LOG_INFO("module.leeroy", "mod-leeroy: training cleanup restored {} bots; {} journals retained",
            restored, failed);
    return journals.empty();
}
}
