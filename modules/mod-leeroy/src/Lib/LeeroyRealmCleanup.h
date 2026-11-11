// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_CLEANUP_H
#define MOD_LEEROY_REALM_CLEANUP_H

#include <algorithm>
#include <cstdint>
#include <functional>
#include <set>
#include <string>
#include <string_view>
#include <vector>

namespace Leeroy::Realm
{
// Native callers supply the actual producer roster. Admission never expands a
// reservation to fit a shared side effect discovered after admission.
std::string ValidateCleanupClosure(std::vector<std::uint32_t> const& actual,
    std::vector<std::uint32_t> const& admitted,
    std::function<std::string(std::uint32_t)> const& validate);
// Charge exactly the validated unique closure in one call. The caller must
// perform no native mutation unless this returns empty; budget refusal leaves
// both the receipt and all bodies untouched.
std::string PrepareCleanupMutation(std::vector<std::uint32_t> const& actual,
    std::vector<std::uint32_t> const& admitted,
    std::function<std::string(std::uint32_t)> const& validate,
    std::function<std::string(std::vector<std::uint32_t> const&)> const& charge);

// What a leave_pvp can tell about a member's Wintergrasp state before it changes anything.
// Wintergrasp keeps private queue, invitation and war state that no cleanup here can prove it
// restored, so a member with any of it is refused. The same answer is given at the command's ack
// (the whole leave is refused before any body is reserved or moved, so the mind releases the
// manifest) and at each step.
struct WintergraspMemberFacts
{
    bool foreignHistory = false, inZone = false, inBattlefieldGroup = false;
    bool queued = false, invited = false, inWar = false;
};
std::string WintergraspPrivateReason(WintergraspMemberFacts const& facts);
// Only an ordinary leave_pvp (no token cleanup) is fenced at its ack.
bool FencesLeavePvpAck(std::string const& kind, std::string const& pvpOwner);

// What a leave_pvp step reads of the battleground or battlefield group a member is in, before it
// takes the member out. Leaving would change the rest of that group when the member leads it,
// when its LFG queue is shared, or when the native removal would disband it.
struct GroupDepartureFacts
{
    bool listed = false;         // the group lists this member
    bool lfg = false;            // it is also an LFG group
    bool lfgBusy = false;        // its LFG state is queued, role check, proposal or boot
    std::uint32_t members = 0;   // roster size
    bool leader = false;
    std::uint32_t linked = 0;    // members linked to an online player (RemoveMember counts these)
};
// Empty, or one of shared_lfg_queue, shared_group_leader, shared_lfg_cleanup, shared_group_cleanup,
// checked in that order.
std::string GroupDepartureReason(GroupDepartureFacts const& facts);
bool IsGroupDepartureReason(std::string_view reason);
// The step's answer for a member it cannot take out, for a reason of the member's own
// (MemberSkipReason: its battleground group, its original raid group, its entry point, its LFG or
// Wintergrasp state, its lease). Skip it (end its lease, take the others out) only while nothing in
// this job has touched its body: its own step changed nothing, and no earlier member's shared
// cleanup changed it. Otherwise the old rule stands: unproved once anything in the job changed.
bool SkipsLeaveMember(std::string_view reason, bool stepChanged, bool touchedByCleanup);
// The members a leave still holds: its manifest without the members it skipped, nor `skipping`.
std::vector<std::uint32_t> LeaveMembers(std::vector<std::uint32_t> const& manifest,
    std::vector<std::uint32_t> const& skipped, std::uint32_t skipping = 0);
// The held members a leave's step rechecks before it acts and before it releases: every member up
// to the step's own (`cursor`), and a later one only once the job touched it (a shared cleanup
// declared a transition for it). A later untouched member is left to its own step, which can skip
// it; a closure that reaches it still validates it explicitly. Never the skipped, nor `skipping`.
std::vector<std::uint32_t> RecheckedMembers(std::vector<std::uint32_t> const& manifest, std::size_t cursor,
    std::vector<std::uint32_t> const& skipped, std::uint32_t skipping, std::function<bool(std::uint32_t)> const& touched);

struct LeavePvpMemberFacts
{
    std::uint32_t guid;
    WintergraspMemberFacts wintergrasp;
    // Exact affected rosters read by the step's cleanup producers, including this member.
    std::vector<std::uint32_t> cleanupMembers = {};
    // GroupDepartureReason for the battleground or battlefield group the step would see, or empty.
    std::string groupDeparture = {};
};
// The ack error for a leave whose members include some in a Wintergrasp state: the reason and
// the first offending guids that fit (the whole error stays within the mind's 256 characters), as
// "wg_private_state_unavailable:301,302". Empty when no member is. The mind accepts it as a
// refusal only when every named guid is in the command's manifest. Wintergrasp takes precedence;
// then name manifest members whose cleanup reaches outside it, using
// "cleanup_member_not_admitted:<guid,...>"; then the members who cannot leave their battleground
// group alone, under the first group departure reason any has ("shared_group_leader:1306"), all
// with the same bound.
std::string LeavePvpAckError(std::vector<LeavePvpMemberFacts> const& members);

struct GroupCleanupPlan
{
    std::vector<std::uint32_t> affected, remaining;
    std::uint32_t leader = 0;
    bool disbands = false;
    std::string error;
};
// Models ordinary LFG RemoveMember(LEAVE), including its linked-reference and
// penultimate-member branches. Roster order determines native leader choice.
// All members must be linked online; an offline roster cannot be swept away.
GroupCleanupPlan PlanOnlineLfgRemoval(std::vector<std::uint32_t> const& roster,
    std::vector<std::uint32_t> const& linked, std::uint32_t leader,
    std::uint32_t leaving, bool remainingLeaderKeepsSolo);

struct GroupCleanupReadback
{
    std::uint32_t guid = 0;
    bool sameLife = false;
    bool groupAbsent = false, originalGroupAbsent = false;
    bool lfgStateNone = false, lfgGroupAbsent = false;
    bool teleporting = false, expectedTeleport = false, arrived = false;
    bool delayedExitPending = false;
};
enum class CleanupReadback { Ready, Pending, Unknown };
CleanupReadback ReadGroupCleanup(std::vector<std::uint32_t> const& expected,
    std::vector<GroupCleanupReadback> const& actual);
struct QueueCleanupReadback
{
    std::uint32_t guid = 0;
    bool sameLife = false, rowAbsent = false, slotAbsent = false;
    bool invitationAbsent = false, unrelatedSlotsPreserved = false;
};
bool ReadQueueCleanup(std::vector<std::uint32_t> const& expected,
    std::vector<QueueCleanupReadback> const& actual);
}
#endif
