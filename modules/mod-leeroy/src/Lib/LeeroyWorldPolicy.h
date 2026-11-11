// This file is part of leeroy, released under the GNU GPL v2.
#ifndef LEEROY_WORLD_POLICY_H
#define LEEROY_WORLD_POLICY_H
#include <algorithm>
#include <cstdint>
#include <vector>
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
}
#endif
