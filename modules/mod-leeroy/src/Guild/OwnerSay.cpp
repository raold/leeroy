// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "OwnerSay.h"
#include "GuildPorts.h"
#include "LeeroyOwnerSay.h"

#include "Group.h"
#include "Player.h"

#include <chrono>
#include <cstdint>
#include <optional>
#include <string>
#include <utility>

// Runs on the world thread, like every bridge command. Nothing here logs the text or a name.

namespace Leeroy::Guild
{
namespace
{
std::uint64_t SteadyNowMs()
{
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count());
}

// One limiter for every channel, kept for the life of the server.
OwnerSayLimiter& Limiter()
{
    static OwnerSayLimiter limiter;
    return limiter;
}

// The group his /p and /ra reach (ChatHandler.cpp): his own, kept as the original group while a battleground or
// battlefield raid stands in for it. A battle group itself is never his.
Group* HisGroup(Player* human)
{
    Group* group = human->GetOriginalGroup() ? human->GetOriginalGroup() : human->GetGroup();
    return group && !group->isBGGroup() && !group->isBFGroup() ? group : nullptr;
}

LeeroyCommandResult HandleOwnerSay(nlohmann::json const& message)
{
    std::string error;
    std::optional<OwnerSayCmd> const cmd = ParseOwnerSay(message, error);
    if (!cmd)
        return {false, std::move(error)};
    if (!GuildEnabled())
        return {false, "disabled"};
    Player* human = FindOnline(HumanGuid());
    if (!human)
        return {false, "human_offline"};

    OwnerSayFacts facts;
    facts.humanGuid = HumanGuid();
    uint32 const guildId = LeeroyModule::Instance().GuildId();
    facts.inGuild = guildId != 0 && human->GetGuildId() == guildId;
    Group const* group = HisGroup(human);
    facts.inGroup = group != nullptr;
    facts.inRaid = group && group->isRaidGroup();
    bool const whisper = cmd->channel == "whisper";
    Player* target = nullptr;
    if (whisper)
    {
        facts.targetCast = IsCast(cmd->toGuid);
        target = FindOnline(cmd->toGuid);
        facts.targetOnline = target != nullptr;
    }

    std::uint64_t const now = SteadyNowMs();
    OwnerSayVerdict const verdict = CheckOwnerSay(*cmd, facts, Limiter(), now);
    if (!verdict.error.empty())
        return {false, std::string(verdict.error)};
    // The parser admits only channels with a chat type, so this refusal is never sent.
    std::optional<Channel> const channel = ParseChannel(cmd->channel);
    if (!channel || !LeeroyQueueHumanChat(human, *channel, verdict.text, whisper ? target->GetName() : std::string()))
        return {false, std::string(Err::BadField) + "channel"};
    Limiter().Record(now);
    return {true, {}};
}
}

void RegisterOwnerSayHandler() { RegisterCommand(std::string(kOwnerSayType), HandleOwnerSay); }
}
