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
