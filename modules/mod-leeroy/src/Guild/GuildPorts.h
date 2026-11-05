// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_GUILD_PORTS_H
#define MOD_LEEROY_GUILD_PORTS_H

#include "LeeroyCommands.h"
#include "LeeroyGuildCommands.h"
#include "LeeroyModule.h"
#include "LeeroyProtocol.h"
#include "Realm/RealmOwnership.h"
#include "Group.h"
#include "ObjectAccessor.h"
#include "Player.h"

#include <utility>
#include <vector>

namespace Leeroy::Guild
{
inline bool RegisterCommand(std::string const& type, LeeroyCommandHandler handler)
{
    return LeeroyRegisterCommand(type, std::move(handler));
}

inline void PublishJob(JobEvent const& event)
{
    LeeroyModule::Instance().Publish(EncodeEvent(NowUnixMs(), MakeJobEvent(event)), false);
}

inline void PublishMasterLoot(uint32 guid, uint32 itemId, std::string const& name)
{
    LeeroyModule::Instance().Publish(EncodeLootWon(NowUnixMs(), guid, itemId, name, LootMethod::Master), false);
}

inline bool IsCast(uint32 guid) { return LeeroyModule::Instance().Cast().IsCast(guid); }
inline std::vector<uint32> CastMembers()
{
    auto const guids = LeeroyModule::Instance().Cast().Guids();
    return {guids.begin(), guids.end()};
}
inline uint32 HumanGuid() { return LeeroyModule::Instance().HumanGuid(); }
inline bool GuildEnabled() { return LeeroyModule::Instance().Enabled(); }
inline bool KeepCast() { return LeeroyModule::Instance().Config().keepCast; }

// World-thread exclusion, independent of Realm's enable flag. A retained or
// queued lease still owns its body. Inspect the complete roster, not only live
// GroupReferences: remove/leader/loot operations also affect offline members.
inline bool GroupHasRealmOwner(Group const* group)
{
    if (group)
        for (auto const& member : group->GetMemberSlots())
            if (Realm::RealmOwns(member.guid.GetCounter())) return true;
    return false;
}

inline Player* FindOnline(uint32 low)
{
    Player* player = ObjectAccessor::FindConnectedPlayer(ObjectGuid::Create<HighGuid::Player>(low));
    return player && player->IsInWorld() && !player->IsDuringRemoveFromWorld() && player->GetSession()
        ? player : nullptr;
}
}

#endif
