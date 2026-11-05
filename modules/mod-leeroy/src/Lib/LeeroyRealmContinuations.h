// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_CONTINUATIONS_H
#define MOD_LEEROY_REALM_CONTINUATIONS_H

#include "LeeroyRealmQueue.h"
#include <map>
#include <optional>
#include <string_view>

namespace Leeroy::Realm
{
// The native reservation/holder map remains the sole body owner. This bounded
// ledger describes continuations of those reservations, never additional locks.
enum class ContinuationKind { Wintergrasp, RatedArena, Fishing, Duel };
using PvpKind = ContinuationKind;
enum class PvpStage { Acquiring, Queued, Invited, Entering, Active, Ending, Cleaning, Clean, Unknown };
enum class ContinuationMode { Application, Cleanup };
struct ContinuationOwner
{
    std::string token, originId;
    ContinuationKind kind = ContinuationKind::Wintergrasp;
    std::uint64_t originEpoch = 0, revision = 1;
    std::uint32_t guild = 0, human = 0;
    std::vector<std::uint32_t> members;
    PvpStage stage = PvpStage::Acquiring;
    bool applicationFenced = false;
    std::string reason{}, cleanupId{};
    ActivityDomain domain = ActivityDomain::Realm;
    bool internalCleanup = false;
};
using PvpOwner = ContinuationOwner;
struct ContinuationCleanReceipt
{
    std::string token, cleanupId;
    std::uint64_t revision = 0;
    std::vector<std::uint32_t> members;
};

// All methods are world-thread only. The caller supplies the actual total of
// runnable + detached + unknown reservations, including this admitted owner.
class ContinuationLedger
{
public:
    explicit ContinuationLedger(std::string bootNonce);
    std::optional<ContinuationOwner> Prepare(std::string originId, ContinuationKind kind,
        std::vector<std::uint32_t> members, std::uint64_t epoch, std::uint32_t guild, std::uint32_t human,
        std::size_t totalReservations, std::string& error, ActivityDomain domain = ActivityDomain::Realm);
    std::optional<ContinuationOwner> Get(std::string_view token) const;
    std::optional<ContinuationOwner> FindOrigin(std::string_view originId) const;
    bool Detach(std::string_view token);
    bool IsDetached(std::string_view token) const;
    std::string Validate(std::string_view token, ContinuationMode mode, bool enabled,
        std::uint64_t epoch, std::uint32_t guild, std::uint32_t human) const;
    bool RequestCleanup(std::string_view token, std::string cleanupId, std::vector<std::uint32_t> members,
        bool enabled, std::uint32_t guild, std::uint32_t human, std::string& error);
    // Same-stage waiting changes only the bounded diagnostic and its revision;
    // an identical diagnostic is idempotent. Cleaning may wait while fenced.
    bool Advance(std::string_view token, std::uint64_t revision, PvpStage stage, std::string reason = {});
    void Unknown(std::string_view token, std::string reason);
    void FenceAll(std::string reason);
    void FenceDomain(ActivityDomain domain, std::string reason);
    bool CanCommit(ContinuationCleanReceipt const& receipt) const;
    bool CanRelease(ContinuationCleanReceipt const& receipt, bool nativeQuiescent) const;
    bool Commit(ContinuationCleanReceipt const& receipt);
    bool CommandIdReserved(std::string_view id) const;
    // Round robin with a fixed observation bound. A snapshot contains at most
    // limit tokens, so a callback may commit/remove its own owner safely.
    void Visit(std::size_t limit, std::function<void(ContinuationOwner const&)> const& visit,
        bool advancePriority = true);
    std::size_t Size() const { return _owners.size(); }
    std::size_t TerminalSize() const { return _terminal.size(); }
    static bool ValidToken(std::string_view token);
    static bool SameManifest(std::vector<std::uint32_t> first, std::vector<std::uint32_t> second);
private:
    struct Record
    {
        ContinuationOwner owner;
        bool detached = false;
        // Never evict a canceled controller while its source can still act.
        // Exhaustion becomes Unknown instead of accepting an old command id.
        std::vector<std::string> canceledControllers{};
    };
    std::string _nonce, _cursor;
    std::uint64_t _serial = 0;
    std::map<std::string, Record, std::less<>> _owners;
    std::deque<Record> _terminal;
    static bool Bump(ContinuationOwner& owner);
};
std::string_view PvpStageName(PvpStage stage);
std::string_view PvpKindName(PvpKind kind);
}
#endif
