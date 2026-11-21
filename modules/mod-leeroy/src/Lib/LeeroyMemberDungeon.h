// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#pragma once

#include "LeeroyGuildAddPolicy.h"

namespace Leeroy::Guild
{
inline constexpr std::uint32_t MemberDungeonMap = 602;
struct DungeonCandidate
{
    std::uint32_t guid;
    GuildAddRole role;
    std::string refusal;
};
std::string MemberDungeonAdmission(std::uint32_t map, std::uint32_t leader,
    std::vector<DungeonCandidate> const& candidates, bool ownerOnline, bool slotBusy);
bool ValidDungeonReceipt(nlohmann::json const& data);
enum class DungeonWriteRead { Unavailable, Previous, Committed, Conflict };
struct DungeonWriteProof
{
    std::uint64_t revision;
    nlohmann::json before, after;
    DungeonWriteRead Observe(std::uint64_t seenRevision, nlohmann::json const* seen) const;
};
bool DungeonBeforeMutation(nlohmann::json const& data);
void ProjectDungeonAddBody(GuildAddBody& body, bool exactOwner);
bool DungeonEntrySource(bool exactOrigin, bool fighting, bool owned);
bool DungeonHandback(bool changed, bool proved, bool abandoned);
bool DungeonCanReleaseSlot(bool failed, std::uint64_t sinceReport, bool held);
bool DungeonMatchesGroup(nlohmann::json const& data, std::uint32_t group, std::uint32_t leader,
    std::vector<std::uint32_t> const& members);
std::vector<std::uint32_t> DungeonKnownRoster(nlohmann::json const& data);
void AuthorizeDungeonReturn(nlohmann::json& data);
bool DungeonReturnAuthorized(nlohmann::json const& data);
bool MemberDungeonPreempts(bool admitted, std::vector<std::uint32_t> const& requested,
    std::vector<std::uint32_t> const& participants);
}
