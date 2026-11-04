// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_MODULE_H
#define MOD_LEEROY_REALM_MODULE_H

#include "LeeroyCastGuard.h"
#include "LeeroyRealmCommands.h"
#include "LeeroyRealmQueue.h"
#include "LeeroyRealmContinuations.h"
#include "LeeroyRealmIdentity.h"
#include <functional>
#include <memory>
#include <string_view>
#include <unordered_set>

namespace Leeroy::Realm
{
using Queued = ActionQueue::Entry;
enum class StepState { Complete, Refused, Pending, Unknown, Detached };
struct RealmStep
{
    StepState state = StepState::Refused;
    std::string reason{};
    std::uint64_t value = 0;
    // At most four exact scopes derived from this operation's known intended
    // transitions, never a blind snapshot of whatever state appeared later.
    // Original account/guild/player/AI/session and lifecycle generation remain
    // immutable. Without an explicit scope, only the pre-step scope is allowed.
    std::vector<BodyScope> expectedScopes{};
};

// The native adapter owns the lock represented by this guard. It must hold its
// inbox/revision lock from final proof through reservation release. No native
// mutation runs under this guard. Lock order: adapter inbox, then module facts;
// hooks must not acquire the adapter inbox while holding module facts.
struct CleanupQuiescence { virtual ~CleanupQuiescence() = default; };
struct PvpCleanProof : ContinuationCleanReceipt
{
    std::shared_ptr<CleanupQuiescence> quiescence;
};
struct PvpContinuationStep
{
    PvpStage stage = PvpStage::Acquiring;
    std::string reason{};
    std::optional<PvpCleanProof> clean{};
};

// World-thread only except Watched/NotifyBodyChanged/RecordKill. Executors do
// one bounded body step; Complete proves that step finished, Refused proves no
// outstanding mutation, Pending retains work, Unknown retains its reservation.
// Pending executors must validate their own exact native operation/expected
// transitions; generic terminal events, timeouts and feature-off are not proof.
class RealmModule
{
public:
    using Executor = std::function<RealmStep(Queued&, std::uint32_t)>;
    // Registered native policy, never a wire-supplied eligibility bypass. Empty
    // means the default current cast/actor policy. Rechecked before every step.
    using Admission = std::function<std::string(Queued const&, std::uint32_t)>;
    static RealmModule& Instance();
    ~RealmModule();
    bool On() const;
    std::uint64_t Epoch() const; // Any-thread enable/configured-identity boundary.
    bool GuildOn() const;
    std::uint64_t GuildEpoch() const;
    void Start(); // World startup, after DBC/map initialization, before commands.
    void Reload();
    void Tick(std::uint32_t diff);
    void Shutdown();
    bool RegisterExecutor(std::string kind, Executor executor, Admission admission = {});
    // A refused admission has no body mutation. Successful admission reserves
    // every exact body and snapshots identity before a later world tick acts.
    bool Accept(Queued entry, std::string& error);
    using ActivityOutcome = std::function<void(Queued const&, bool clean, std::string const& reason)>;
    bool RegisterGuildFishing(Executor, Admission, std::function<PvpContinuationStep(PvpOwner const&)>, ActivityOutcome);
    bool AcceptGuildFishing(std::string id, std::vector<std::uint32_t> guids, std::string& error);
    std::optional<PvpOwner> PrepareGuildFishingContinuation(std::string_view originId,
        std::vector<std::uint32_t> const& members, std::string& error);
    std::string MutationReason(std::uint32_t guid, std::string_view operation = {}) const;
    // Exact lifetime/generation and original or declared pending scope. This
    // checks every referenced body, including a duel's non-current target.
    std::string ValidateOwnedBody(std::uint32_t guid, std::string_view operation) const;
    // Before an indivisible multi-body mutation inside the current executor.
    // Includes the current body, validates all exact leases and consumes any
    // additional budget atomically. This grants no scope-transition authority.
    // busy:tick_budget means wait without mutating; action_cost_exceeds_cap
    // means this operation cannot fit the configured whole-tick budget.
    std::string ChargeAffectedBodies(std::string_view operation, std::vector<std::uint32_t> const& guids);
    std::string DeclareAffectedBodyScopes(std::string_view operation, std::uint32_t guid,
        std::vector<BodyScope> scopes);
    // Active exact guild_grow only. One synchronous native add with a causal
    // receipt kept in the existing whole-manifest reservation. The original
    // identity stays immutable; no other command gains a guild-change waiver.
    RealmStep PerformGuildGrowJoin(std::string_view operation, std::uint32_t guid, std::uint32_t destination);
    using Continuation = std::function<PvpContinuationStep(PvpOwner const&)>;
    bool RegisterPvpContinuation(PvpKind kind, Continuation continuation);
    // Active executor only, before the first mutation. The manifest must equal
    // the admitted reservation. Detached parks that entry without publishing a
    // successful queue job; the adapter publishes only proved native enrollment.
    std::optional<PvpOwner> PreparePvpContinuation(std::string_view originId, PvpKind kind,
        std::vector<std::uint32_t> const& members, std::string& error);
    std::optional<PvpOwner> GetPvpOwner(std::string_view token) const;
    std::optional<PvpOwner> FindPvpOwner(std::string_view originId) const;
    bool RequestPvpCleanup(std::string cleanupId, std::string_view token,
        std::vector<std::uint32_t> const& members, std::string& error);
    std::string ValidateContinuation(std::string_view token, std::uint32_t guid, ContinuationMode mode) const;
    std::string ChargeContinuationBodies(std::string_view token, std::vector<std::uint32_t> const& members);
    // Native causal proof only, never a wire snapshot. The adapter may declare
    // a core-created group after its exact synchronous creation receipt. This
    // does not alter immutable identity/generation or the owner revision.
    bool DeclareContinuationScopes(std::string_view token, std::uint64_t revision, std::uint32_t guid,
        std::vector<BodyScope> scopes);
    std::string Holder(std::uint32_t guid) const;
    bool SetActors(ActorsCmd const& actors, std::string& error);
    bool IsActor(std::uint32_t guid) const;
    bool Watched(std::uint32_t guid) const;
    std::unordered_set<std::uint32_t> const& Gankers() const;
    std::unordered_set<std::uint32_t> const& RealmGuilds() const;
    std::vector<Place> const& Places() const;
    std::vector<ItemRule> const& Items() const;
    WeaponRecord const* Weapons(std::uint32_t guid) const;
    bool RecordWeapons(std::uint32_t guid, std::string_view operation = {});
    void PublishJob(RealmJob const& job);
    bool RegisterPoll(std::string name, std::uint32_t intervalMs, std::function<void()> poll);
    void RecordKill(Kill kill); // Verified epoch-bound immutable observation, safe from map hooks.
    // World thread: drains once, then matches only the exact current proof.
    std::optional<Kill> MatchProof(AnnounceCmd const& request, std::uint64_t nowMs) const;
    void NotifyBodyChanged(std::uint32_t guid); // Login/logout invalidates queued identity.
    std::uint64_t BodyGeneration(std::uint32_t guid) const; // Read-only immediate-call life fence.
    GuardState* Guard(); // Same configured identity retains state across reload/off.
private:
    RealmModule();
    void ReconcileConfiguration();
    void TickContinuations(TickBudget& budget);
    bool AcceptDomain(Queued entry, std::string& error);
    void PublishOutcome(Queued const& entry, RealmJob const& job);
    void DrainGuildOutcomes();
    std::optional<PvpOwner> PrepareContinuation(std::string_view originId, PvpKind kind,
        std::vector<std::uint32_t> const& members, ActivityDomain domain, std::string& error);
    struct State;
    std::unique_ptr<State> _state;
};

// Called before cast refresh/repinning. Hooks record facts even with the guard
// disabled; only the world-thread pass can apply protected membership changes.
void TickCastGuard(std::uint32_t diff);
// Includes current/former protected members and verified kicks awaiting the
// next guard pass. Unknown guard facts conservatively exclude recruitment.
bool CastRecruitmentProtected(std::uint32_t guid);
// Guard facts were lost (a full queue or an unknown command result).
bool CastGuardFactsUnknown();
// A confirmed kick of this guid not yet drained into the guard.
bool CastGuardKickFactPending(std::uint32_t guid);
void AddCastGuardScripts();
void RegisterRealmHandlers();
void RegisterRealmContinuationHandlers();
}

void AddSC_leeroy_realm();
#endif
