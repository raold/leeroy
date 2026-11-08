// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmCleanup.h"

namespace Leeroy::Realm
{
bool FencesLeavePvpAck(std::string const& kind, std::string const& pvpOwner)
{
    return kind == "leave_pvp" && pvpOwner.empty();
}
namespace
{
std::string NamedAckError(std::string const& prefix, std::vector<std::uint32_t> const& guids)
{
    // The whole error stays within the mind's 256 characters, with at least one name.
    constexpr std::size_t MaxLength = 256;
    std::string named;
    bool any = false;
    for (auto guid : guids)
    {
        auto const next = (named.empty() ? "" : ",") + std::to_string(guid);
        if (any && prefix.size() + named.size() + next.size() > MaxLength) break;
        named += next; any = true;
    }
    return any ? prefix + named : "";
}
}
std::string LeavePvpAckError(std::vector<LeavePvpMemberFacts> const& members)
{
    std::set<std::uint32_t> admitted;
    std::vector<std::uint32_t> wintergrasp, outside;
    for (auto const& member : members)
    {
        admitted.insert(member.guid);
        if (!WintergraspPrivateReason(member.wintergrasp).empty()) wintergrasp.push_back(member.guid);
    }
    if (!wintergrasp.empty()) return NamedAckError("wg_private_state_unavailable:", wintergrasp);
    for (auto const& member : members)
        if (std::any_of(member.cleanupMembers.begin(), member.cleanupMembers.end(),
            [&](auto guid) { return !admitted.contains(guid); })) outside.push_back(member.guid);
    return NamedAckError("cleanup_member_not_admitted:", outside);
}
std::string WintergraspPrivateReason(WintergraspMemberFacts const& facts)
{
    return facts.foreignHistory || facts.inZone || facts.inBattlefieldGroup || facts.queued || facts.invited
        || facts.inWar ? "wg_private_state_unavailable" : "";
}
std::string ValidateCleanupClosure(std::vector<std::uint32_t> const& actual,
    std::vector<std::uint32_t> const& admitted,
    std::function<std::string(std::uint32_t)> const& validate)
{
    std::set<std::uint32_t> unique(actual.begin(), actual.end());
    if (actual.empty() || actual.size() > 150 || unique.size() != actual.size() || unique.contains(0))
        return "cleanup_manifest_unavailable";
    for (auto guid : actual)
    {
        if (std::find(admitted.begin(), admitted.end(), guid) == admitted.end())
            return "cleanup_member_not_admitted";
        if (auto why = validate(guid); !why.empty()) return why;
    }
    return "";
}

std::string PrepareCleanupMutation(std::vector<std::uint32_t> const& actual,
    std::vector<std::uint32_t> const& admitted,
    std::function<std::string(std::uint32_t)> const& validate,
    std::function<std::string(std::vector<std::uint32_t> const&)> const& charge)
{
    if (auto why = ValidateCleanupClosure(actual, admitted, validate); !why.empty()) return why;
    return charge(actual);
}

GroupCleanupPlan PlanOnlineLfgRemoval(std::vector<std::uint32_t> const& roster,
    std::vector<std::uint32_t> const& linked, std::uint32_t leader,
    std::uint32_t leaving, bool remainingLeaderKeepsSolo)
{
    GroupCleanupPlan plan;
    std::set<std::uint32_t> members(roster.begin(), roster.end()), references(linked.begin(), linked.end());
    if (roster.empty() || roster.size() > 150 || members.size() != roster.size() || members.contains(0)
        || references.size() != linked.size() || members != references
        || !members.contains(leader) || !members.contains(leaving))
    {
        plan.error = "lfg_group_manifest_unavailable";
        return plan;
    }
    plan.affected = roster;
    for (auto guid : roster) if (guid != leaving) plan.remaining.push_back(guid);
    plan.leader = leaving == leader ? (plan.remaining.empty() ? 0 : plan.remaining.front()) : leader;
    plan.disbands = plan.remaining.empty() || (plan.remaining.size() == 1 && !remainingLeaderKeepsSolo);
    if (plan.disbands) { plan.remaining.clear(); plan.leader = 0; }
    return plan;
}

CleanupReadback ReadGroupCleanup(std::vector<std::uint32_t> const& expected,
    std::vector<GroupCleanupReadback> const& actual)
{
    std::set<std::uint32_t> members(expected.begin(), expected.end()), seen;
    if (members.empty() || members.size() != expected.size() || members.contains(0) || actual.size() != expected.size())
        return CleanupReadback::Unknown;
    bool pending = false;
    for (auto const& member : actual)
    {
        if (!members.contains(member.guid) || !seen.insert(member.guid).second || !member.sameLife
            || !member.groupAbsent || !member.originalGroupAbsent || !member.lfgStateNone || !member.lfgGroupAbsent)
            return CleanupReadback::Unknown;
        if (member.teleporting || member.delayedExitPending)
        {
            if (!member.expectedTeleport) return CleanupReadback::Unknown;
            pending = true;
        }
        else if (!member.arrived) return CleanupReadback::Unknown;
    }
    return pending ? CleanupReadback::Pending : CleanupReadback::Ready;
}

bool ReadQueueCleanup(std::vector<std::uint32_t> const& expected,
    std::vector<QueueCleanupReadback> const& actual)
{
    std::set<std::uint32_t> members(expected.begin(), expected.end()), seen;
    if (members.empty() || members.size() != expected.size() || members.contains(0) || actual.size() != expected.size()) return false;
    for (auto const& member : actual)
        if (!members.contains(member.guid) || !seen.insert(member.guid).second || !member.sameLife || !member.rowAbsent
            || !member.slotAbsent || !member.invitationAbsent || !member.unrelatedSlotsPreserved) return false;
    return true;
}
}
