// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "GearUpJob.h"
#include "GuildPorts.h"
#include "LeeroyGearUpPolicy.h"

#include "Event.h"
#include "Guild.h"
#include "GuildMgr.h"
#include "Log.h"
#include "Map.h"
#include "PlayerbotAI.h"
#include "PlayerbotFactory.h"
#include "Playerbots.h"

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
    // IsCast is level-80-only. Gear-up must also accept under-level members of this same owned guild.
    // Validate the entire request before queuing it; a non-bot guild member never reaches a factory.
    for (uint32 guid : command->guids)
    {
        if (Realm::RealmOwns(guid)) return {false, "busy:realm_owner"};
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

    uint32 const guid = job.command.guids[job.next++];
    Player* bot = FindOnline(guid);
    std::string_view const skip = Realm::RealmOwns(guid) ? "busy:realm_owner"
        : GearUpSkipReason(StateFor(guid, job, bot));
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
            std::string_view const afterLevel = Realm::RealmOwns(guid) ? "busy:realm_owner"
                : GearUpSkipReason(StateFor(guid, job, bot));
            if (!afterLevel.empty())
                job.result.skipped.emplace_back(guid, afterLevel);
            else
            {
                auto const gearFailure = [&]() -> std::string_view
                {
                    PlayerbotAI* ai = GET_PLAYERBOT_AI(bot);
                    // silent suppresses wrapper feedback. The installed BiS action may still TellMaster internally;
                    // changing global chat settings or temporarily changing the bot's master would be unsafe here.
                    bool const applied = ai->DoSpecificAction("autogear bis",
                        Event("autogear bis", std::to_string(job.command.ilvl)), true);
                    bot = FindOnline(guid);
                    if (Realm::RealmOwns(guid)) return "busy:realm_owner";
                    if (auto const failure = GearUpSkipReason(StateFor(guid, job, bot)); !failure.empty())
                        return failure;
                    if (!applied)
                    {
                        PlayerbotFactory::AutoGear(bot, ITEM_QUALITY_EPIC, job.command.ilvl, false);
                        bot = FindOnline(guid);
                    }
                    return Realm::RealmOwns(guid) ? "busy:realm_owner" : GearUpSkipReason(StateFor(guid, job, bot));
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
