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
}
#endif
