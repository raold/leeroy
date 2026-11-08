// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_CLEANUP_H
#define MOD_LEEROY_REALM_CLEANUP_H

#include <algorithm>
#include <cstdint>
#include <functional>
#include <set>
#include <string>
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
// The ack error for a leave whose members include some in a Wintergrasp state: the reason and
// the first (at most 24, so the error stays well inside the wire's 256) offending guids, as
// "wg_private_state_unavailable:301,302". Empty when no member is. The mind accepts it as a
// refusal only when every named guid is in the command's manifest.
std::string LeavePvpAckError(std::vector<std::pair<std::uint32_t, WintergraspMemberFacts>> const& members);

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
