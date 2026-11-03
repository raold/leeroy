// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_MODULE_H
#define MOD_LEEROY_REALM_MODULE_H

#include "LeeroyCastGuard.h"
#include "LeeroyRealmCommands.h"
#include "LeeroyRealmQueue.h"
#include <functional>
#include <memory>
#include <string_view>
#include <unordered_set>

namespace Leeroy::Realm
{
using Queued = ActionQueue::Entry;
enum class StepState { Complete, Refused, Pending, Unknown };
struct BodyScope
{
    std::uint32_t map = 0, instance = 0;
    std::uint64_t group = 0;
    bool operator==(BodyScope const&) const = default;
};
struct RealmStep
{
    StepState state = StepState::Refused;
    std::string reason;
    std::uint64_t value = 0;
    // At most four exact scopes derived from this operation's known intended
    // transitions, never a blind snapshot of whatever state appeared later.
    // Original account/guild/player/AI/session and lifecycle generation remain
    // immutable. Without an explicit scope, only the pre-step scope is allowed.
    std::vector<BodyScope> expectedScopes;
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
    void Start(); // World startup, after DBC/map initialization, before commands.
    void Reload();
    void Tick(std::uint32_t diff);
    void Shutdown();
    bool RegisterExecutor(std::string kind, Executor executor, Admission admission = {});
    // A refused admission has no body mutation. Successful admission reserves
    // every exact body and snapshots identity before a later world tick acts.
    bool Accept(Queued entry, std::string& error);
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
    void RecordKill(Kill kill, std::uint64_t expectedEpoch); // Epoch-bound immutable facts, safe from map hooks.
    std::optional<Kill> MatchKill(std::uint32_t map, std::uint32_t instance, std::uint32_t boss,
                                  std::uint64_t nowMs) const;
    void NotifyBodyChanged(std::uint32_t guid); // Login/logout invalidates queued identity.
    GuardState* Guard(); // Same configured identity retains state across reload/off.
private:
    RealmModule();
    void ReconcileConfiguration();
    struct State;
    std::unique_ptr<State> _state;
};

// Called before cast refresh/repinning. Hooks record facts even with the guard
// disabled; only the world-thread pass can apply protected membership changes.
void TickCastGuard(std::uint32_t diff);
void AddCastGuardScripts();
void RegisterRealmHandlers();
}

void AddSC_leeroy_realm();
#endif
