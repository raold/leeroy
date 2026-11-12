// This file is part of leeroy, released under the GNU GPL v2.
#include "RealmWorld.h"
#include "RealmPorts.h"
#include "LeeroyModule.h"
#include "LeeroyWorldPolicy.h"
#include "LeeroyWorldGuilds.h"
#include "LeeroyWorldProtocol.h"
#include "Config.h"
#include <charconv>
#include <set>
#include <sstream>

namespace Leeroy::Realm
{
namespace
{
bool AuctionAccounts(std::set<std::uint32_t>& accounts)
{
    return World::AuctionAccounts(sConfigMgr->GetOption<std::string>("AuctionHouseBot.GUIDs", "0"),
        [](std::uint32_t guid) {
            auto const* character = sCharacterCache->GetCharacterCacheByGuid(PlayerGuid(guid));
            return character ? character->AccountId : 0;
        },accounts);
}

std::vector<World::MemberEvidence> Evidence(::Guild const& guild, std::set<std::uint32_t> const& auction)
{
    std::vector<World::MemberEvidence> result;
    for (auto const& [guid,member] : guild.GetUnorderedMembers())
    {
        auto const* character = sCharacterCache->GetCharacterCacheByGuid(PlayerGuid(guid));
        auto account = character ? character->AccountId : 0;
        result.push_back({guid,account,character != nullptr,
            character && character->GuildId == guild.GetId() && account == member.GetAccountId(),
            sPlayerbotAIConfig.IsInRandomAccountList(account),auction.contains(account)});
    }
    std::sort(result.begin(),result.end(),[](auto const& a,auto const& b) { return a.guid<b.guid; });
    return result;
}

LeeroyCommandResult Roster(nlohmann::json const& message)
{
    if (!WorldOn()) return {false,"realm_off"};
    std::string error;
    auto request = World::RosterRequest(message,error);
    if (!request) return {false,error};
    auto const own = GuildId(), human = HumanGuid();
    if (!own || !human) return {false,"world_identity_unavailable"};
    std::set<std::uint32_t> auction;
    if (!AuctionAccounts(auction)) return {false,"world_auction_unavailable"};
    nlohmann::json rows = nlohmann::json::array();
    for (auto const* guild : World::GuildSnapshot<GuildMgr>::All(*sGuildMgr))
    {
        auto evidence = Evidence(*guild,auction);
        for (auto const& item : World::Roster(guild->GetId(),own,human,evidence))
        {
            auto const* character = sCharacterCache->GetCharacterCacheByGuid(PlayerGuid(item.guid));
            auto const* member = guild->GetMember(PlayerGuid(item.guid));
            auto const* online = FindOnline(item.guid);
            rows.push_back({{"guid",item.guid},{"account",item.account},{"guild_id",guild->GetId()},
                {"guild_name",guild->GetName()},{"name",member->GetName()},{"race",character->Race},
                {"class",member->GetClass()},{"gender",member->GetGender()},
                {"level",member->GetLevel()},{"rank",member->GetRankId()},{"online",online != nullptr},
                {"map",online ? online->GetMapId() : 0},{"zone",online ? online->GetZoneId() : 0}});
            if (rows.size() > World::kMaxMembers) return {false,"world_roster_capacity"};
        }
    }
    // Publish only complete snapshots, in bounded pages. Python waits for every page.
    auto const pages = World::RosterPages(*request,own,human,rows);
    if (!pages) return {false,"world_roster_invalid"};
    for (auto const& body : *pages) Publish(body);
    return {true,{}};
}
}

bool WorldOn()
{
    // LoadConfig refreshes this cached switch on .reload config, not on every poll.
    return World::Enabled(RealmModule::Instance().On(),LeeroyModule::Instance().WorldEnabled());
}

bool WorldSpeaker(std::uint32_t guid, std::uint32_t guildId)
{
    if (!WorldOn() || !RealmModule::Instance().RealmGuilds().contains(guildId) || IsCast(guid)) return false;
    auto const* guild = sGuildMgr->GetGuildById(guildId);
    std::set<std::uint32_t> auction;
    if (!guild || !AuctionAccounts(auction)) return false;
    auto members = Evidence(*guild,auction);
    return World::WorldSpeaker(WorldOn(),RealmModule::Instance().RealmGuilds().contains(guildId),
        IsCast(guid),guid,guildId,GuildId(),HumanGuid(),members);
}

void RegisterWorldRosterHandler() { RegisterCommand("get_world_roster",Roster); }
}
