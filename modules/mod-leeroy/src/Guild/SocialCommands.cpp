// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "GearUpJob.h"
#include "GuildPorts.h"

#include "SocialMgr.h"

#include <string>
#include <utility>

namespace Leeroy::Guild
{
LeeroyCommandResult HandleRemoveFriend(nlohmann::json const& message)
{
    std::string error;
    auto const command = ParseRemoveFriend(message, error);
    if (!command)
        return {false, std::move(error)};
    if (!GuildEnabled())
        return {false, "disabled"};
    Player* human = FindOnline(HumanGuid());
    if (!human)
        return {false, "human_offline"};
    ObjectGuid const guid = ObjectGuid::Create<HighGuid::Player>(command->guid);
    PlayerSocial* social = human->GetSocial();
    if (!social || !social->HasFriend(guid))
        return {false, "not_friend"};
    // A former guild member can still be removed: only the configured human's own friend list is mutated.
    social->RemoveFromSocialList(guid, SOCIAL_FLAG_FRIEND);
    sSocialMgr->SendFriendStatus(human, FRIEND_REMOVED, guid, false);
    return {true, {}};
}

void RegisterSocialCommands() { RegisterCommand("remove_friend", HandleRemoveFriend); }
}
