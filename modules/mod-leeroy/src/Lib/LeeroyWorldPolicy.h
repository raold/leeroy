// This file is part of leeroy, released under the GNU GPL v2.
#ifndef LEEROY_WORLD_POLICY_H
#define LEEROY_WORLD_POLICY_H
#include <algorithm>
#include <cstdint>
#include <vector>
#include <charconv>
#include <set>
#include <sstream>
#include <string>
namespace Leeroy::World
{
struct MemberEvidence
{
    std::uint32_t guid = 0, account = 0;
    bool exists = false, sameGuild = false, randomAccount = false, auctionAccount = false;
};
inline bool EligibleGuild(std::uint32_t guild, std::uint32_t own, std::vector<MemberEvidence> const& members)
{
    return guild && own && guild != own && !members.empty()
        && std::all_of(members.begin(), members.end(), [](auto const& m)
            { return m.guid && m.account && m.exists && m.sameGuild && m.randomAccount; });
}
inline bool EligibleMember(MemberEvidence const& member, std::uint32_t human)
{
    return human && member.guid != human && !member.auctionAccount && member.exists
        && member.sameGuild && member.randomAccount;
}
inline constexpr std::size_t kPageSize = 100, kMaxMembers = 8192;

inline bool Enabled(bool realm, bool world) { return realm && world; }

// Resolve the whole AH account, not just its configured character. Invalid
// configuration never grants a partial exclusion list.
template<class Resolve>
bool AuctionAccounts(std::string const& configured, Resolve resolve, std::set<std::uint32_t>& accounts)
{
    if (configured.empty() || configured.back() == ',') return false;
    std::set<std::uint32_t> found;
    std::istringstream input(configured);
    for (std::string token; std::getline(input,token,',');)
    {
        auto first = token.find_first_not_of(" \t"), last = token.find_last_not_of(" \t");
        if (first == std::string::npos) return false;
        auto text = std::string_view(token).substr(first,last-first+1);
        std::uint32_t guid = 0;
        auto [end,error] = std::from_chars(text.data(),text.data()+text.size(),guid);
        if (error != std::errc{} || end != text.data()+text.size()) return false;
        if (!guid) continue;
        auto account = resolve(guid);
        if (!account) return false;
        found.insert(account);
    }
    accounts = std::move(found);
    return true;
}

inline std::vector<MemberEvidence> Roster(std::uint32_t guild, std::uint32_t own,
    std::uint32_t human, std::vector<MemberEvidence> const& members)
{
    std::vector<MemberEvidence> out;
    if (EligibleGuild(guild,own,members))
        for (auto const& m : members) if (EligibleMember(m,human)) out.push_back(m);
    std::sort(out.begin(),out.end(),[](auto const& a,auto const& b) { return a.guid < b.guid; });
    return out;
}

inline bool WorldSpeaker(bool enabled, bool rival, bool cast, std::uint32_t guid,
    std::uint32_t guild, std::uint32_t own, std::uint32_t human, std::vector<MemberEvidence> const& members)
{
    if (!enabled || !rival || cast || !guid) return false;
    auto roster = Roster(guild,own,human,members);
    return std::any_of(roster.begin(),roster.end(),[&](auto const& m) { return m.guid == guid; });
}
}
#endif
