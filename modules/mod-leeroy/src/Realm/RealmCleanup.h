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
// Refuse a leave whose Wintergrasp state is private or whose cleanup reaches outside its manifest.
std::string LeavePvpAckReason(Queued const& entry);
std::string PrepareOrdinaryCleanupMutation(Queued& entry, std::uint32_t guid, std::vector<BodyScope> scopes);
// Recheck every earlier shared native mutation before releasing the operation.
// A nonempty result retains the admitted manifest through Unknown.
std::string VerifyCleanupReceipts(Queued const& entry);
}
#endif
