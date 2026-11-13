// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_RELEASE_H
#define MOD_LEEROY_REALM_RELEASE_H
#include "LeeroyRealmFailure.h"
#include <vector>

namespace Leeroy::Realm
{
inline constexpr std::uint64_t OperationWaitMs = 60000;
inline constexpr std::string_view LeaveReadbackChecks[] = {
    "offline", "owner", "teleport", "exit", "battleground", "original_group", "lfg", "group", "queue"};
constexpr bool ReadbackCheck(std::string_view check)
{
    for (auto value : LeaveReadbackChecks) if (value == check) return true;
    return false;
}
// The code that names a member a leave's cleanup recheck (MemberCleanupFault) refused when the
// readback itself sees nothing (a Wintergrasp queue, a training fence), so the failed job still
// carries readback_guid: empty only for no fault. Any other reason is a fence or another owner.
constexpr std::string_view CleanupFaultCheck(std::string_view reason)
{
    if (reason.empty()) return {};
    if (reason == "cleanup_member_offline" || reason == "busy:body_unavailable" || reason == "busy:offline")
        return "offline";
    if (reason == "wg_private_state_unavailable") return "queue";
    // The member's LFG closure: a proposal, a boot, or a closure member it cannot take along.
    if (reason.starts_with("shared_lfg") || reason.starts_with("cleanup_member_")) return "lfg";
    return "owner";
}
struct LeaveChangeProof
{
    bool ownStep = false, cleanup = false, scopes = false, continuation = false, currentStep = false;
};
constexpr bool MayHaveChanged(LeaveChangeProof const& proof)
{ return proof.ownStep || proof.cleanup || proof.scopes || proof.continuation || proof.currentStep; }
enum class LeaveReadback { Ready, Pending, Skip, Failed };
constexpr LeaveReadback JudgeLeaveReadback(std::string_view check, LeaveChangeProof const& proof, bool expired)
{
    if (check.empty()) return LeaveReadback::Ready;
    if (!ReadbackCheck(check)) return LeaveReadback::Failed;
    if (!expired && (check == "teleport" || check == "exit")) return LeaveReadback::Pending;
    return MayHaveChanged(proof) ? LeaveReadback::Failed : LeaveReadback::Skip;
}
struct ForgetFacts
{
    bool held = false, retained = false;
    std::string_view kind{};
    bool ordinary = false, scopes = false, continuation = false, guildJoins = false, teleport = false;
};
constexpr std::string_view ForgetOutcome(ForgetFacts const& facts)
{
    if (!facts.held) return "not_held";
    if (!facts.retained) return "running";
    if (!facts.ordinary || !SkipsMembers(facts.kind) || facts.scopes || facts.continuation || facts.guildJoins)
        return "kind";
    if (facts.teleport) return "busy:teleport";
    return "released";
}
template<class Release>
std::string_view ApplyForget(ForgetFacts const& facts, Release release)
{
    auto outcome = ForgetOutcome(facts);
    if (outcome == "released") release();
    return outcome;
}
template<class Changed, class Release>
std::vector<std::uint32_t> ReleaseUntouched(std::vector<std::uint32_t> const& held, Changed changed, Release release)
{
    std::vector<std::uint32_t> released;
    for (auto guid : held)
        if (!changed(guid)) { release(guid); released.push_back(guid); }
    return released;
}
}
#endif
