// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#pragma once
#include "LeeroyCommands.h"

namespace Leeroy::Guild
{
void ReloadMemberDungeon();
void RegisterMemberDungeon();
bool MemberDungeonEnabled();
LeeroyCommandResult StopMemberDungeon(std::string const& start, bool ownerRequest = false);
std::string MemberDungeonFor(std::uint32_t guid);
// Exact owned origin/group/copy and all independent fences, for a validated higher-owner preview.
bool CanProjectDungeonBody(std::string const& start, std::uint32_t guid);
}
