// This file is part of leeroy, released under the GNU GPL v2.
#include "RealmWorld.h"
#include "RealmPorts.h"
#include "LeeroyWorldPolicy.h"
#include "Config.h"
#include <charconv>
#include <set>
#include <sstream>

namespace Leeroy::Realm
{
namespace
{
class GuildView : public GuildMgr
{
public:
    static std::vector<::Guild const*> All(GuildMgr const& manager)
    {
        std::vector<::Guild const*> result;
        for (auto const& [id,guild] : manager.*(&GuildView::GuildStore))
            if (guild) result.push_back(guild);
        std::sort(result.begin(),result.end(),[](auto a,auto b) { return a->GetId()<b->GetId(); });
        return result;
    }
};

bool AuctionAccounts(std::set<std::uint32_t>& accounts)
{
    auto configured = sConfigMgr->GetOption<std::string>("AuctionHouseBot.GUIDs", "0");
    if (configured.empty() || configured.back() == ',') return false;
    std::istringstream input(configured);
    std::string token;
    while (std::getline(input,token,','))
    {
        auto first = token.find_first_not_of(" \t"), last = token.find_last_not_of(" \t");
        if (first == std::string::npos) return false;
        auto text = std::string_view(token).substr(first,last-first+1);
        std::uint32_t guid = 0;
        auto [end,error] = std::from_chars(text.data(),text.data()+text.size(),guid);
        if (error != std::errc{} || end != text.data()+text.size()) return false;
        if (!guid) continue;
        auto const* character = sCharacterCache->GetCharacterCacheByGuid(PlayerGuid(guid));
        if (!character || !character->AccountId) return false;
        accounts.insert(character->AccountId);
    }
    return true;
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
    auto const own = GuildId(), human = HumanGuid();
    if (!own || !human) return {false,"world_identity_unavailable"};
    std::set<std::uint32_t> auction;
    if (!AuctionAccounts(auction)) return {false,"world_auction_unavailable"};
    nlohmann::json rows = nlohmann::json::array();
    for (auto const* guild : GuildView::All(*sGuildMgr))
    {
        auto evidence = Evidence(*guild,auction);
        if (!World::EligibleGuild(guild->GetId(),own,evidence)) continue;
        for (auto const& item : evidence)
        {
            if (!World::EligibleMember(item,human)) continue;
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
    auto const pages = std::max<std::size_t>(1,(rows.size()+World::kPageSize-1)/World::kPageSize);
    for (std::size_t page=0; page<pages; ++page)
    {
        auto values = nlohmann::json::array();
        for (std::size_t i=page*World::kPageSize; i<std::min(rows.size(),(page+1)*World::kPageSize); ++i)
            values.push_back(rows[i]);
        Publish({{"type","world_roster"},{"request",message.at("id")},{"own_guild",own},
                 {"human",human},{"page",page},{"pages",pages},{"total",rows.size()},{"members",values}});
    }
    return {true,{}};
}
}

bool WorldSpeaker(std::uint32_t guid, std::uint32_t guildId)
{
    if (!RealmModule::Instance().RealmGuilds().contains(guildId) || IsCast(guid)) return false;
    auto const* guild = sGuildMgr->GetGuildById(guildId);
    std::set<std::uint32_t> auction;
    if (!guild || !AuctionAccounts(auction)) return false;
    auto members = Evidence(*guild,auction);
    return World::EligibleGuild(guildId,GuildId(),members)
        && std::any_of(members.begin(),members.end(),[&](auto const& m)
            { return m.guid == guid && World::EligibleMember(m,HumanGuid()); });
}

void RegisterWorldRosterHandler() { RegisterCommand("get_world_roster",Roster); }
}
