// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_FAILURE_H
#define MOD_LEEROY_REALM_FAILURE_H

#include <cstddef>
#include <cstdint>
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
// A rearm also leaves out, at its ack, a member that admission fences for a reason of its own (his
// group's members are in every overnight rearm of the cast): nothing of it is reserved or touched,
// and the job's terminal event lists it as skipped. Only when every member is fenced, or a fence is
// the job's own, is the rearm refused whole. A leave keeps its whole-command ack refusals, whose
// named form the mind reads (LeavePvpAckError).
constexpr bool LeavesOutAtAck(std::string_view kind) { return kind == "rearm"; }

// The refusals about one member itself: its own body, lease, fences and state, or the closure its
// own step would take along. An allow-list: anything else (Realm off, the operation's identity or
// holder, a training cleanup pending anywhere, LFG switched off, the tick budget, a malformed field,
// a reason added later) concerns the whole job, which keeps its old rule. A job-wide fence named a
// member reason would skip every member after it and then strand the job at its final readback.
inline constexpr std::string_view MemberSkipReasons[] = {
    // Tick's pre-step fence and admission (MutationReason without the job's own, Identity, admit).
    "busy:body_identity_changed", "busy:body_scope_changed", "body_identity_changed", "busy:body_identity",
    "busy:training_owner", "busy:training_state", "not_bot", "busy:body_unavailable", "busy:raid_owner",
    "not_actor", "not_cast",
    // Eligible: BusyReason's "busy:<reason>", then its own.
    "busy:offline", "busy:dead", "busy:combat", "busy:flight", "busy:raid_group", "busy:human_group",
    "busy:instance", "busy:battleground", "busy:arena", "busy:lfg", "busy:teleport", "busy:duel",
    // A rearm's step before any swap.
    "weapon_record_unavailable", "recorded_weapon_unavailable", "equipment_changed",
    "cannot_equip_recorded_weapon", "offhand_occupied",
    // A leave's own step and its recheck of the member.
    "shared_lfg_queue", "shared_group_leader", "shared_lfg_cleanup", "shared_group_cleanup",
    "entry_point_unavailable", "battleground_group_changed", "battleground_receipt_unavailable",
    "shared_lfg_proposal", "shared_lfg_participants_unavailable", "wg_private_state_unavailable",
    "leave_cleanup_unproved", "body_unavailable", "cleanup_member_offline", "cleanup_member_busy",
    "cleanup_member_not_admitted", "cleanup_manifest_unavailable", "action_cost_exceeds_cap",
    // The leave's cleanup producers, before they charge or declare anything.
    "queue_identity_unavailable", "queue_identity_changed", "queue_receipt_unavailable", "queue_type_changed",
    "queue_manifest_unavailable", "queue_template_unavailable", "queue_lineage_unavailable",
    "queue_lineage_changed", "lfg_applicant_phase_changed", "lfg_applicant_member_changed",
    "lfg_group_unavailable", "lfg_group_manifest_unavailable", "lfg_member_lineage_changed"};
constexpr bool MemberSkipReason(std::string_view reason)
{
    for (auto allowed : MemberSkipReasons)
        if (allowed == reason) return true;
    return false;
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

// What Tick does with the member its own pre-step fence (holder, lease, identity, admission) refused,
// before that member's executor ran: the old rule (finish the job; it is released with changed:false
// when nothing changed yet, else as before), a skip, or for a leave's last member the job's final
// readback of the members still held first, then the skip.
enum class FenceOutcome { OldRule, Skip, ReadbackThenSkip };
struct FenceFacts
{
    std::string_view kind{};
    std::string_view reason{};
    bool lease = false;            // the member's lease is still in the reservation
    bool pending = false;          // any pending entry: its step ran before, or a transition is declared
    bool declared = false;
    bool touchedByCleanup = false;
    bool continuation = false;
    bool last = false;             // the manifest's last member
};
constexpr FenceOutcome DecideFence(FenceFacts const& facts)
{
    MemberSkipProof proof;
    proof.kind = SkipsMembers(facts.kind);
    proof.memberReason = MemberSkipReason(facts.reason);
    proof.stepRan = false;
    proof.pendingStep = facts.pending;
    proof.declaredScopes = facts.declared;
    proof.continuation = facts.continuation;
    proof.touchedByCleanup = facts.touchedByCleanup;
    if (!facts.lease || !AcceptsMemberSkip(proof)) return FenceOutcome::OldRule;
    return facts.kind == "leave_pvp" && facts.last ? FenceOutcome::ReadbackThenSkip : FenceOutcome::Skip;
}

// A done job names its member only for a one-member command; members a rearm left out at its ack
// do not shrink the command the mind sent, so the admitted manifest decides.
constexpr std::uint32_t DoneJobGuid(std::size_t admittedManifest, std::uint32_t last)
{
    return admittedManifest == 1 ? last : 0;
}
}
#endif
