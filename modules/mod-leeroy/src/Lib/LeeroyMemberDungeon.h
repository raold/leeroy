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
bool DungeonBeforeMutation(nlohmann::json const& data);
bool MemberDungeonPreempts(bool admitted, std::vector<std::uint32_t> const& requested,
    std::vector<std::uint32_t> const& participants);
}
