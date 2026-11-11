// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_NATIVE_CLEANUP_H
#define MOD_LEEROY_REALM_NATIVE_CLEANUP_H
#include "RealmModule.h"

namespace Leeroy::Realm
{
RealmStep LeaveNativeQueue(Queued& entry, std::uint32_t guid, std::uint32_t slot);
RealmStep LeaveOnlineLfgGroup(Queued& entry, std::uint32_t guid);
RealmStep LeaveObservedLfgApplicants(Queued& entry, std::uint32_t guid);
std::string OrdinaryCleanupReason(Queued const& entry);
// Refuse a leave whose Wintergrasp state is private, whose cleanup reaches outside its manifest, or
// one of whose members cannot leave its battleground group alone.
std::string LeavePvpAckReason(Queued const& entry);
// GroupDepartureReason for the battleground or battlefield group the player is in, as the leave
// step checks it before taking the player out; empty for any other group or none.
std::string BattlegroundGroupDeparture(Player* player);
// The members a leave skipped (Tick ended their leases), and the ones it still holds, without
// `skipping` (the member whose skip is being decided).
std::vector<std::uint32_t> SkippedMembers(Queued const& entry);
std::vector<std::uint32_t> HeldMembers(Queued const& entry, std::uint32_t skipping = 0);
std::string PrepareOrdinaryCleanupMutation(Queued& entry, std::uint32_t guid, std::vector<BodyScope> scopes);
// Recheck every earlier shared native mutation before releasing the operation.
// A nonempty result retains the admitted manifest through Unknown. The last
// step (`skipping` when that step skips its member) reads back every held member.
std::string VerifyCleanupReceipts(Queued const& entry, std::uint32_t skipping = 0);
}
#endif
