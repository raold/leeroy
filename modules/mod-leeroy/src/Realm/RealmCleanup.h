// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_NATIVE_CLEANUP_H
#define MOD_LEEROY_REALM_NATIVE_CLEANUP_H
#include "RealmModule.h"
#include <optional>

namespace Leeroy::Realm
{
RealmStep LeaveNativeQueue(Queued& entry, std::uint32_t guid, std::uint32_t slot);
RealmStep LeaveOnlineLfgGroup(Queued& entry, std::uint32_t guid);
RealmStep LeaveObservedLfgApplicants(Queued& entry, std::uint32_t guid);
// The first rechecked member (RecheckedMembers) whose state forbids the leave's next step, and why;
// empty when none does. `skipping` is the member whose skip is being decided.
struct CleanupFault
{
    std::string reason{};
    std::uint32_t guid = 0;
};
CleanupFault OrdinaryCleanupFault(Queued const& entry, std::uint32_t skipping = 0);
std::string OrdinaryCleanupReason(Queued const& entry, std::uint32_t skipping = 0);
// Refuse a leave whose Wintergrasp state is private, whose cleanup reaches outside its manifest, or
// one of whose members cannot leave its battleground group alone.
std::string LeavePvpAckReason(Queued const& entry);
// GroupDepartureReason for the battleground or battlefield group the player is in, as the leave
// step checks it before taking the player out; empty for any other group or none.
std::string BattlegroundGroupDeparture(Player* player);
// The members a leave or a rearm skipped (Tick ended their leases), and the ones a leave still
// holds, without `skipping` (the member whose skip is being decided).
std::vector<std::uint32_t> SkippedMembers(Queued const& entry);
std::vector<std::uint32_t> HeldMembers(Queued const& entry, std::uint32_t skipping = 0);
// Whether a shared cleanup in this leave changed the member (MarkChanged, before its mutation).
bool TouchedByCleanup(Queued const& entry, std::uint32_t guid);
bool OwnStepChanged(Queued const& entry, std::uint32_t guid);
void RecordOwnStepChange(Queued& entry, std::uint32_t guid);
// A member the leave cannot take out (SkipsLeaveMember): its skip, or nothing when the member's own
// step or a shared cleanup in this job changed it. Tick reads back the remaining members after
// the final step/skip; a transient final readback waits without running that step again.
std::optional<RealmStep> SkipLeaveMember(Queued& entry, std::uint32_t guid, std::string const& why);
std::string PrepareOrdinaryCleanupMutation(Queued& entry, std::uint32_t guid, std::vector<BodyScope> scopes);
// Recheck every earlier shared native mutation before releasing the operation.
// A nonempty result retains affected bodies through Unknown. Tick separately
// reads back every held member after the final step or skip.
std::string VerifyCleanupReceipts(Queued& entry, std::uint32_t skipping = 0);
// Runs after the last member's step/skip, without replaying that step while waiting.
RealmStep FinalLeaveReadback(Queued& entry);
std::string LeaveReadbackCheck(Queued const& entry, std::uint32_t guid);
std::string LeaveReadbackFault(Queued& entry, std::uint32_t guid, std::string const& check, std::string reason);
}
#endif
