// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_PVP_PROOF_H
#define MOD_LEEROY_REALM_PVP_PROOF_H

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <map>
#include <set>
#include <string>
#include <vector>

namespace Leeroy::Realm
{
// Eligibility history is independent of the 500 active-owner capacity. It
// records foreign native WG exposure from boot without adopting private state.
class WintergraspHistory
{
public:
    static constexpr std::size_t Capacity = 8192;
    bool TaintBody(std::uint32_t guid) { return Insert(_bodies, guid); }
    bool TaintSession(std::uintptr_t session) { return Insert(_sessions, session); }
    bool Logout(std::uint32_t guid, std::uintptr_t session)
    {
        if (!_sessions.count(session)) return true;
        if (!TaintBody(guid)) return false;
        _sessions.erase(session);
        return true;
    }
    bool Allows(std::uint32_t guid, std::uintptr_t session) const
        { return !_unavailable && !_bodies.count(guid) && !_sessions.count(session); }
    bool HasSession(std::uintptr_t session) const { return _sessions.count(session) != 0; }
private:
    template<class T> bool Insert(std::set<T>& values, T value)
    {
        if (values.count(value)) return true;
        if (values.size() == Capacity) { _unavailable = true; return false; }
        values.insert(value); return true;
    }
    bool _unavailable = false;
    std::set<std::uint32_t> _bodies;
    std::set<std::uintptr_t> _sessions;
};
// Native insertion and instance generations are independent of millisecond
// timestamps and allocator addresses. No snapshot can mint these receipts.
struct ArenaQueueIdentity
{
    std::uint64_t insertion = 0, group = 0, groupGeneration = 0;
    std::uint32_t queue = 0, team = 0, type = 0, bracket = 0, joinTime = 0;
    bool operator==(ArenaQueueIdentity const&) const = default;
};
struct ArenaMatchIdentity
{
    std::uint64_t generation = 0;
    std::uint32_t instance = 0, map = 0, inviteExpiry = 0;
    bool operator==(ArenaMatchIdentity const&) const = default;
};
enum class ArenaMemberStage { Queued, Entering, Active, Removed };
enum class ArenaProgressStage { Queued, Invited, Entering, Active, Ending };
class ArenaLineage
{
public:
    bool Bind(ArenaQueueIdentity identity, std::vector<std::uint32_t> members)
    {
        if (_bound || !identity.insertion || !identity.group || !identity.groupGeneration || !identity.team
            || !identity.queue || (identity.type != 2 && identity.type != 3 && identity.type != 5)
            || members.size() != identity.type || !SortedManifest(members)) return Fail("arena_queue_receipt");
        _identity = identity;
        _members = std::move(members);
        for (auto guid : _members) _stages.emplace(guid, ArenaMemberStage::Queued);
        _bound = true;
        return true;
    }
    static bool SortedManifest(std::vector<std::uint32_t> const& members)
    {
        return !members.empty() && members.size() <= 40 && members.front()
            && std::is_sorted(members.begin(), members.end())
            && std::adjacent_find(members.begin(), members.end()) == members.end();
    }
    bool ObserveQueue(ArenaQueueIdentity const& identity, std::vector<std::uint32_t> const& projection)
    {
        if (!_bound || !_reason.empty() || !(identity == _identity)) return Fail("arena_queue_lineage");
        std::vector<std::uint32_t> expected;
        for (auto const& [guid, stage] : _stages)
            if (stage == ArenaMemberStage::Queued) expected.push_back(guid);
        return projection == expected || Fail("arena_queue_projection");
    }
    bool Invite(ArenaMatchIdentity identity)
    {
        if (!_bound || !_reason.empty() || !identity.generation || !identity.instance || !identity.map
            || !identity.inviteExpiry) return Fail("arena_invite_receipt");
        if (_match.generation && !(_match == identity)) return Fail("arena_invite_replaced");
        _match = identity;
        return true;
    }
    bool BeginEnter(std::uint32_t guid, ArenaMatchIdentity const& match)
    {
        auto found = _stages.find(guid);
        if (!_reason.empty() || !_match.generation || !(match == _match) || found == _stages.end()
            || found->second != ArenaMemberStage::Queued) return Fail("arena_enter_lineage");
        found->second = ArenaMemberStage::Entering;
        return true;
    }
    bool Entered(std::uint32_t guid, ArenaMatchIdentity const& match, bool postStateMatches)
    {
        auto found = _stages.find(guid);
        if (!_reason.empty() || !(_match == match) || found == _stages.end()
            || found->second != ArenaMemberStage::Entering || !postStateMatches) return Fail("arena_entry_receipt");
        found->second = ArenaMemberStage::Active;
        return true;
    }
    bool Removed(std::uint32_t guid, bool exactNativeReceipt, bool sourcesAbsent)
    {
        auto found = _stages.find(guid);
        if (!_reason.empty() || found == _stages.end() || !exactNativeReceipt || !sourcesAbsent)
            return Fail("arena_remove_receipt");
        found->second = ArenaMemberStage::Removed;
        return true;
    }
    bool CanClean(bool groupGone, bool allBodiesRestored, std::uint64_t observedRevision,
                  std::uint64_t currentRevision) const
    {
        return _bound && _reason.empty() && groupGone && allBodiesRestored
            && observedRevision == currentRevision
            && std::all_of(_stages.begin(), _stages.end(), [](auto const& item)
                { return item.second == ArenaMemberStage::Removed; });
    }
    bool Fail(std::string reason) { if (_reason.empty()) _reason = std::move(reason); return false; }
    std::string const& Reason() const { return _reason; }
    bool Bound() const { return _bound; }
    ArenaQueueIdentity const& Queue() const { return _identity; }
    ArenaMatchIdentity const& Match() const { return _match; }
    std::vector<std::uint32_t> const& Members() const { return _members; }
    ArenaMemberStage Stage(std::uint32_t guid) const { return _stages.at(guid); }
    ArenaProgressStage Progress(bool invited) const
    {
        bool allActive = !_stages.empty(), anyEntered = false;
        for (auto const& [guid, stage] : _stages)
        {
            (void)guid;
            if (stage == ArenaMemberStage::Removed) return ArenaProgressStage::Ending;
            allActive = allActive && stage == ArenaMemberStage::Active;
            anyEntered = anyEntered || stage == ArenaMemberStage::Entering || stage == ArenaMemberStage::Active;
        }
        return allActive ? ArenaProgressStage::Active : anyEntered ? ArenaProgressStage::Entering
            : invited ? ArenaProgressStage::Invited : ArenaProgressStage::Queued;
    }
private:
    bool _bound = false;
    ArenaQueueIdentity _identity;
    ArenaMatchIdentity _match;
    std::vector<std::uint32_t> _members;
    std::map<std::uint32_t, ArenaMemberStage> _stages;
    std::string _reason;
};

struct PvpDestination
{
    std::uint32_t map = 0;
    float x = 0, y = 0, z = 0, o = 0;
    bool operator==(PvpDestination const&) const = default;
};
// Exact destinations in the installed BattlefieldWG::OnPlayerJoinWar. The
// effective team is sampled by the synchronous teleport hook, after JoinWar.
inline PvpDestination WintergraspWarDestination(std::uint32_t team, std::uint32_t defender)
{
    if (team == defender) return {571, 5345, 2842, 410, 3.14f};
    if (team == 1) return {571, 5025.857422f, 3674.628906f, 362.737122f, 4.135169f};
    return {571, 5101.284f, 2186.564f, 365.549f, 3.812f};
}
struct WintergraspEntryEvidence
{
    bool sameLife = false, nativeAccepted = false, scopedTeleport = false, expectedDestination = false;
    bool zoneEntryReceipt = false, arrivalReceipt = false, inWar = false, exactGroup = false;
    bool teleportFinished = false;
};
inline bool ProvesWintergraspEntry(WintergraspEntryEvidence const& evidence)
{
    return evidence.sameLife && evidence.nativeAccepted && evidence.scopedTeleport && evidence.expectedDestination
        && evidence.zoneEntryReceipt && evidence.arrivalReceipt && evidence.inWar && evidence.exactGroup
        && evidence.teleportFinished;
}

struct WintergraspCleanupEvidence
{
    bool sameLife = false, queueRemovalReceipt = false, publicSourcesAbsent = false;
    bool everExposed = false, postZoneDepartureReceipt = false, outsideZone = false;
    bool groupRestored = false, teleportFinished = false, discontinuity = false;
    std::uint64_t observedRevision = 0, currentRevision = 0;
};
inline bool ProvesWintergraspClean(WintergraspCleanupEvidence const& evidence)
{
    return evidence.sameLife && !evidence.discontinuity && evidence.queueRemovalReceipt
        && evidence.publicSourcesAbsent && evidence.groupRestored && evidence.teleportFinished
        && evidence.observedRevision == evidence.currentRevision
        && (!evidence.everExposed || (evidence.postZoneDepartureReceipt && evidence.outsideZone));
}
}
#endif
