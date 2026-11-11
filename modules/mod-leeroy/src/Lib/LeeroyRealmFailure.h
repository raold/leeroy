// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_FAILURE_H
#define MOD_LEEROY_REALM_FAILURE_H

#include <string_view>

namespace Leeroy::Realm
{
// Whole-job proof, not just the last member's refusal. Unknown executor outcomes
// never set stepUnchanged, and each completed member must explicitly prove it.
struct BodyFailureProof
{
    bool stepUnchanged = false;
    bool previousUnchanged = true;
    bool retained = false;
    bool declaredScopes = false;
    bool continuation = false;
    bool guildJoinAttempt = false;
    bool cleanupMutated = false;
};
constexpr bool ProvesNoBodyChange(BodyFailureProof const& proof)
{
    return proof.stepUnchanged && proof.previousUnchanged && !proof.retained
        && !proof.declaredScopes && !proof.continuation && !proof.guildJoinAttempt && !proof.cleanupMutated;
}

// The kinds whose members are independent bodies: an ordinary leave_pvp (queued leaves never carry a
// PvP owner token) and a rearm. One of their members can be left out without the others.
constexpr bool SkipsMembers(std::string_view kind) { return kind == "leave_pvp" || kind == "rearm"; }
// A rearm also leaves out, at its ack, a member that admission fences (his group's members are in
// every overnight rearm of the cast): nothing of it is reserved or touched, and the job's terminal
// event lists it as skipped. Only when every member is fenced is the rearm refused whole. A leave
// keeps its whole-command ack refusals, whose named form the mind reads (LeavePvpAckError).
constexpr bool LeavesOutAtAck(std::string_view kind) { return kind == "rearm"; }
// A refusal about the member itself. Realm off, the operation's own identity or holder, and a
// missing executor concern the whole job, which keeps its old rule.
constexpr bool MemberSkipReason(std::string_view reason)
{
    return !reason.empty() && reason != "realm_off" && reason != "busy:operation_identity"
        && reason != "busy:realm_owner" && reason != "unsupported";
}

// A step that skips its member ends that member's lease alone, and the job goes on with the
// others. Native accepts it only for a kind that skips members, for the member's own reason, and
// only while nothing else in the job expects, declared or awaits a transition for that body or
// touched it with a shared cleanup (an earlier member's cleanup declares a transition for each body
// it touches). Then either its executor proved the member's own step changed nothing and Tick found
// the body exactly as admitted after it, or Tick's own fence refused the member before its executor
// ever ran (no pending entry at all, so not even a step that waited). Anything less keeps today's
// rule: unproved, retained.
struct MemberSkipProof
{
    bool kind = false;             // SkipsMembers
    bool memberReason = false;     // MemberSkipReason
    bool stepRan = true;           // false: Tick's pre-step fence refused it, its executor never ran
    bool stepUnchanged = false;
    bool exact = false;
    bool expectedScopes = false;
    bool declaredScopes = false;
    bool pendingScopes = false;    // a transition is awaited for it
    bool pendingStep = false;      // it has a pending entry, even one awaiting no transition
    bool continuation = false;
    bool touchedByCleanup = false; // a shared cleanup in this job changed it
};
constexpr bool AcceptsMemberSkip(MemberSkipProof const& proof)
{
    if (!proof.kind || !proof.memberReason || proof.declaredScopes || proof.pendingScopes
        || proof.continuation || proof.touchedByCleanup)
        return false;
    return proof.stepRan ? proof.stepUnchanged && proof.exact && !proof.expectedScopes : !proof.pendingStep;
}
}
#endif
