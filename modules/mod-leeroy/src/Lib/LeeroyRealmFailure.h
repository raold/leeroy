// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_FAILURE_H
#define MOD_LEEROY_REALM_FAILURE_H

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

// A step that skips its member ends that member's lease alone, and the job goes on with the
// others. Native accepts it only for an ordinary leave whose executor proved the member's own step
// changed nothing, whose body it found exactly as admitted after the step, and for which nothing
// else in the job expects, declared or awaits a transition (an earlier member's shared cleanup
// declares one for each body it touches). Anything less keeps today's rule: unproved, retained.
struct MemberSkipProof
{
    bool leave = false;
    bool stepUnchanged = false;
    bool exact = false;
    bool expectedScopes = false;
    bool declaredScopes = false;
    bool pendingScopes = false;
    bool continuation = false;
};
constexpr bool AcceptsMemberSkip(MemberSkipProof const& proof)
{
    return proof.leave && proof.stepUnchanged && proof.exact && !proof.expectedScopes
        && !proof.declaredScopes && !proof.pendingScopes && !proof.continuation;
}
}
#endif
