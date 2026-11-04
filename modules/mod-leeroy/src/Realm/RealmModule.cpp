// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "RealmModule.h"
#include "RealmOwnership.h"
#include "RealmPorts.h"
#include "RealmGuild.h"
#include "LeeroyRealmGuild.h"
#include "Log.h"
#include <algorithm>
#include <atomic>
#include <fstream>
#include <limits>
#include <map>
#include <mutex>
#include <random>
#include <set>

namespace Leeroy::Realm
{
namespace
{
std::string BootNonce()
{
    // Refuse continuation/kill-proof creation if native entropy is unavailable.
    // A process-local serial alone must never identify a previous boot's proof.
    try
    {
        std::random_device source;
        constexpr char digits[] = "0123456789abcdef";
        std::string nonce;
        for (unsigned word = 0; word < 4; ++word)
        {
            auto value = static_cast<std::uint32_t>(source());
            for (unsigned nibble = 0; nibble < 8; ++nibble)
                nonce.push_back(digits[(value >> (nibble * 4)) & 15]);
        }
        return nonce;
    }
    catch (...) { return {}; }
}
std::optional<std::string> ReadFile(std::string const& path)
{
    std::ifstream stream(path, std::ios::binary | std::ios::ate);
    if (!stream)
        return std::nullopt;
    auto const size = stream.tellg();
    if (size < 0 || size > static_cast<std::streamoff>(kMaxCsvBytes))
        return std::nullopt;
    std::string text(static_cast<std::size_t>(size), '\0');
    stream.seekg(0);
    if (!text.empty() && !stream.read(text.data(), static_cast<std::streamsize>(text.size())))
        return std::nullopt;
    return text;
}
bool Due(std::uint32_t& remaining, std::uint32_t diff, std::uint32_t interval)
{
    if (diff < remaining)
    {
        remaining -= diff;
        return false;
    }
    remaining = interval;
    return true;
}
}

struct RealmModule::State
{
    struct Lease { BodyIdentity body; std::uint64_t generation = 0; };
    struct Reservation
    {
        std::uint64_t epoch = 0;
        std::map<std::uint32_t, Lease> bodies;
        std::map<std::uint32_t, std::vector<BodyScope>> pending;
        std::set<std::uint32_t> declaredScopes;
        std::map<std::uint32_t, GuildJoinReceipt> guildJoins;
        std::set<std::uint32_t> guildJoinAttempts;
        bool retained = false;
        std::uint64_t value = 0;
        Queued admitted;
        std::string continuation;
    };
    struct Poll { std::uint32_t interval, remaining = 0; std::function<void()> fn; };
    struct Handler { Executor execute; Admission admit; ActivityOutcome outcome{}; };
    std::atomic<bool> on{false};
    std::atomic<std::uint64_t> epoch{1};
    std::atomic<bool> guildOn{false};
    std::atomic<std::uint64_t> guildEpoch{1};
    std::uint32_t guild = 0, human = 0;
    bool disablePending = false;
    bool guildDisablePending = false;
    bool started = false;
    ActionQueue queue;
    std::map<std::string, Handler, std::less<>> executors;
    std::map<std::string, Reservation, std::less<>> reservations;
    ContinuationLedger continuations{BootNonce()};
    std::map<PvpKind, Continuation> continuationHandlers;
    bool continuationsFirst = false;
    std::map<std::uint32_t, std::string> holders;
    std::unordered_set<std::uint32_t> actors, gankers, guilds;
    std::vector<Place> places;
    std::vector<ItemRule> items;
    std::map<std::uint32_t, WeaponRecord> weapons;
    std::map<std::pair<std::uint32_t, std::uint32_t>, std::unique_ptr<GuardState>> guards;
    std::map<std::string, Poll> polls;
    std::optional<bool> humanFlag;
    std::uint32_t flagRemaining = 0, weaponsRemaining = 0, pinRemaining = 0;
    mutable std::mutex factsMutex;
    std::map<std::uint32_t, std::uint64_t> generations;
    std::vector<Kill> killInbox;
    KillRing kills;
    std::string killBoot = BootNonce(); // Unavailable entropy disables kill proof publication.
    std::uint64_t killSerial = 0; // Never reset on identity/enable boundaries.
    TickBudget* activeBudget = nullptr;
    Queued const* activeEntry = nullptr;
    std::string activeOperation;
    std::string activeContinuation;
    std::uint64_t activeRevision = 0;
    std::set<std::uint32_t> chargedBodies;
    std::deque<std::pair<Queued, RealmJob>> guildOutcomes;

    bool Enabled(ActivityDomain domain) const { return domain == ActivityDomain::Guild ? guildOn.load() : on.load(); }
    std::uint64_t EpochFor(ActivityDomain domain) const
    { return domain == ActivityDomain::Guild ? guildEpoch.load() : epoch.load(); }

    std::uint64_t Generation(std::uint32_t guid) const
    {
        std::lock_guard lock(factsMutex);
        auto it = generations.find(guid);
        return it == generations.end() ? 0 : it->second;
    }
    LeaseCheck CheckLease(Reservation const& reservation, std::uint32_t guid,
        std::optional<BodyIdentity> const& current, std::vector<BodyScope> const& scopes) const
    {
        auto const& saved = reservation.bodies.at(guid);
        auto joined = reservation.guildJoins.find(guid);
        if (joined == reservation.guildJoins.end())
            return CheckBodyLease(saved.body, saved.generation, current, Generation(guid), scopes);
        auto const& receipt = joined->second;
        auto* destination = sGuildMgr->GetGuildById(receipt.destination);
        auto const* member = destination ? destination->GetMember(PlayerGuid(guid)) : nullptr;
        auto const* character = sCharacterCache->GetCharacterCacheByGuid(PlayerGuid(guid));
        if (reservation.admitted.kind != "guild_grow" || reservation.admitted.domain != ActivityDomain::Realm
            || reservation.admitted.payload.at("guild_id").get<std::uint32_t>() != receipt.destination
            || !guilds.contains(receipt.destination) || IsRealGuild(receipt.destination)
            || !member || member->GetGUID() != PlayerGuid(guid) || member->GetRankId() != receipt.rank
            || member->GetAccountId() != saved.body.account || member->GetClass() != saved.body.classId
            || member->GetGender() != saved.body.sex || member->GetLevel() != 80
            || !character || character->GuildId != receipt.destination || character->AccountId != saved.body.account
            || IsCast(guid) || IsConfiguredGuildMember(guid) || actors.contains(guid) || CastRecruitmentProtected(guid))
            return LeaseCheck::LifeChanged;
        return CheckGuildJoinLease(receipt, reservation.admitted.id, guid, GuildId(), HumanGuid(),
            EpochFor(reservation.admitted.domain), saved.body, saved.generation, current, Generation(guid), scopes);
    }
    void DrainKills()
    {
        std::vector<std::string> lines;
        {
            std::lock_guard lock(factsMutex);
            // Moving/removing the inbox before publication makes a second
            // Tick/MatchProof drain unable to allocate or publish it again.
            auto observations = std::move(killInbox);
            killInbox.clear();
            for (auto& kill : observations)
            {
                if (!on.load() || kill.nativeEpoch != epoch.load() || kill.guildId != guild
                    || kill.humanGuid != human || !ValidKillObservation(kill)
                    || killSerial == std::numeric_limits<std::uint64_t>::max()) continue;
                auto const published = NowUnixMs(); // Independent Unix clock; never derive from steady time.
                if (published <= 0 || static_cast<std::uint64_t>(published) < kill.verifiedAtMs) continue;
                kill.killId = killBoot + ':' + std::to_string(killSerial + 1);
                kill.publishedAtMs = static_cast<std::uint64_t>(published);
                if (!ValidKillProof(kill)) continue;
                auto line = EncodeEvent(published, MakeRealmKill(kill));
                if (!kills.Add(kill)) continue;
                ++killSerial;
                lines.push_back(std::move(line)); // Exact ring record exists before any bridge publication.
            }
        }
        for (auto& line : lines)
            LeeroyModule::Instance().Publish(std::move(line), false); // Nondroppable; outside factsMutex.
    }
    void ApplyPins()
    {
        auto wanted = on.load() ? actors : std::unordered_set<std::uint32_t>{};
        for (auto const& [guid, owner] : holders)
        {
            (void)owner;
            wanted.insert(guid);
        }
        // A Realm-only disable/removal cannot unpin an outstanding foreign
        // operation that still needs a previously contributed body.
        for (auto guid : Pins().Owned("realm"))
            if (!NativeOwnershipReason(guid).empty())
                wanted.insert(guid);
        if (wanted != Pins().Owned("realm")) SetRealmPins(wanted);
    }
    void Release(std::string const& id)
    {
        auto it = reservations.find(id);
        if (it == reservations.end())
            return;
        for (auto const& [guid, lease] : it->second.bodies)
        {
            (void)lease;
            auto holder = holders.find(guid);
            if (holder != holders.end() && holder->second == id)
                holders.erase(holder);
        }
        reservations.erase(it);
    }
};

RealmModule& RealmModule::Instance() { static RealmModule module; return module; }
RealmModule::RealmModule() : _state(std::make_unique<State>()) { }
RealmModule::~RealmModule() = default;
bool RealmModule::On() const { return _state->on.load(); }
std::uint64_t RealmModule::Epoch() const { return _state->epoch.load(); }
bool RealmModule::GuildOn() const { return _state->guildOn.load(); }
std::uint64_t RealmModule::GuildEpoch() const { return _state->guildEpoch.load(); }
std::uint64_t RealmModule::BodyGeneration(std::uint32_t guid) const { return _state->Generation(guid); }

void RealmModule::Start()
{
    _state->started = true;
    Reload();
}

void RealmModule::Reload()
{
    auto const& cfg = Config();
    bool const enabled = cfg.enable && cfg.realmEnable && cfg.guildId && cfg.humanGuid;
    bool const changed = On() != enabled || _state->guild != cfg.guildId || _state->human != cfg.humanGuid;
    bool const guildEnabled = cfg.enable && cfg.guildId && cfg.humanGuid;
    bool const guildChanged = GuildOn() != guildEnabled || _state->guild != cfg.guildId || _state->human != cfg.humanGuid;
    if (guildChanged)
    {
        std::lock_guard lock(_state->factsMutex);
        _state->guildDisablePending = true;
        _state->guildOn = guildEnabled;
        ++_state->guildEpoch;
    }
    if (changed)
    {
        std::lock_guard lock(_state->factsMutex);
        _state->disablePending = true;
        _state->on = enabled;
        ++_state->epoch;
        _state->killInbox.clear();
        _state->kills = {};
        _state->humanFlag.reset();
    }
    _state->guild = cfg.guildId;
    _state->human = cfg.humanGuid;
    if (cfg.guildId && cfg.humanGuid)
    {
        auto key = std::pair{cfg.guildId, cfg.humanGuid};
        if (!_state->guards.contains(key))
            _state->guards.emplace(key, std::make_unique<GuardState>(cfg.guildId, cfg.humanGuid));
    }
    std::string error;
    // Initial config hooks precede DBC/maps. Native place checks belong to
    // world startup and later world-thread reloads, before any admission.
    auto placesText = _state->started ? ReadFile(cfg.realmPlacesFile) : std::nullopt;
    auto places = placesText ? ParsePlaces(*placesText, error) : std::nullopt;
    _state->places.clear();
    if (places)
    {
        for (auto& place : *places)
            if (ValidPlace(place)) _state->places.push_back(std::move(place));
            else LOG_ERROR("module.leeroy", "mod-leeroy: realm place {} rejected: map/coordinates/zone mismatch", place.name);
    }
    error.clear();
    auto itemsText = ReadFile(cfg.realmItemsFile);
    auto items = itemsText ? ParseItems(*itemsText, error) : std::nullopt;
    _state->items = items ? std::move(*items) : std::vector<ItemRule>{};
    ReconcileConfiguration(); // Cancels the old epoch; never runs a queued action on reload.
    LOG_INFO("module.leeroy", "mod-leeroy: realm {}, cast guard {}, places {}, items {}, max actions/tick {}",
        enabled ? "on" : "off", cfg.castGuard && cfg.enable ? "on" : "off", _state->places.size(),
        _state->items.size(), cfg.realmMaxActionsPerTick);
}

bool RealmModule::RegisterExecutor(std::string kind, Executor executor, Admission admission)
{
    if (!admission)
        admission = [](Queued const&, std::uint32_t guid)
        { return IsCast(guid) || RealmModule::Instance().IsActor(guid) ? "" : "not_actor"; };
    return IsQueued(kind) && executor
        && _state->executors.emplace(std::move(kind), State::Handler{std::move(executor), std::move(admission)}).second;
}

bool RealmModule::RegisterPvpContinuation(PvpKind kind, Continuation continuation)
{
    return continuation && _state->continuationHandlers.emplace(kind, std::move(continuation)).second;
}

bool RealmModule::RegisterGuildFishing(Executor executor, Admission admission, Continuation continuation,
    ActivityOutcome outcome)
{
    if (!executor || !admission || !continuation || !outcome
        || _state->executors.contains("guild_fishing") || _state->continuationHandlers.contains(PvpKind::Fishing)) return false;
    _state->executors.emplace("guild_fishing", State::Handler{std::move(executor), std::move(admission), std::move(outcome)});
    _state->continuationHandlers.emplace(PvpKind::Fishing, std::move(continuation));
    return true;
}

bool RealmModule::AcceptGuildFishing(std::string id, std::vector<std::uint32_t> guids, std::string& error)
{
    if (guids.empty() || guids.size() > 40) { error = "bad field: guids"; return false; }
    Queued entry{std::move(id), "guild_fishing", std::move(guids), 0, nlohmann::json::object(), ActivityDomain::Guild};
    return AcceptDomain(std::move(entry), error);
}

void RealmModule::PublishOutcome(Queued const& entry, RealmJob const& job)
{
    if (entry.domain == ActivityDomain::Realm) { PublishJob(job); return; }
    // May be called while final native/lifecycle proof guards are held. Native
    // outcome callbacks run later without those locks, never in inverse order.
    // Each admitted Guild operation produces one result. Admission counts this
    // backlog with all live/retained reservations, so even a permanently failing
    // callback cannot grow it past the shared Capacity. A prepared finite owner
    // never also publishes a configuration-drop result.
    _state->guildOutcomes.emplace_back(entry, job);
}
void RealmModule::DrainGuildOutcomes()
{
    for (unsigned count = 0; count < 20 && !_state->guildOutcomes.empty(); ++count)
    {
        auto const& [entry, job] = _state->guildOutcomes.front();
        auto handler = _state->executors.find(entry.kind);
        if (handler != _state->executors.end() && handler->second.outcome)
        {
            try { handler->second.outcome(entry, job.ok, job.reason); }
            catch (...) { return; } // Preserve the immutable result for retry.
        }
        _state->guildOutcomes.pop_front();
    }
}

std::optional<PvpOwner> RealmModule::PreparePvpContinuation(std::string_view originId, PvpKind kind,
    std::vector<std::uint32_t> const& members, std::string& error)
{ return PrepareContinuation(originId, kind, members, ActivityDomain::Realm, error); }

std::optional<PvpOwner> RealmModule::PrepareGuildFishingContinuation(std::string_view originId,
    std::vector<std::uint32_t> const& members, std::string& error)
{ return PrepareContinuation(originId, PvpKind::Fishing, members, ActivityDomain::Guild, error); }

std::optional<PvpOwner> RealmModule::PrepareContinuation(std::string_view originId, PvpKind kind,
    std::vector<std::uint32_t> const& members, ActivityDomain domain, std::string& error)
{
    error.clear();
    auto it = _state->reservations.find(originId);
    if (!_state->activeEntry || _state->activeOperation != originId || it == _state->reservations.end()
        || !_state->continuationHandlers.contains(kind) || !_state->Enabled(domain)
        || it->second.admitted.domain != domain || it->second.epoch != _state->EpochFor(domain)
        || !ContinuationLedger::SameManifest(it->second.admitted.guids, members))
    { error = "busy:operation_identity"; return std::nullopt; }
    for (auto guid : members)
        if (!(error = MutationReason(guid, originId)).empty()) return std::nullopt;
    auto owner = _state->continuations.Prepare(std::string(originId), kind, members, _state->EpochFor(domain),
        _state->guild, _state->human, _state->reservations.size(), error, domain);
    if (owner)
    {
        it->second.continuation = owner->token;
        _state->activeContinuation = owner->token;
        _state->activeRevision = owner->revision;
    }
    return owner;
}

std::optional<PvpOwner> RealmModule::GetPvpOwner(std::string_view token) const
{ return _state->continuations.Get(token); }
std::optional<PvpOwner> RealmModule::FindPvpOwner(std::string_view originId) const
{ return _state->continuations.FindOrigin(originId); }

bool RealmModule::RequestPvpCleanup(std::string cleanupId, std::string_view token,
    std::vector<std::uint32_t> const& members, std::string& error)
{
    // The same admission id cannot become a cleanup controller or control a
    // second owner. All checks precede the absorbing application fence.
    if (_state->reservations.contains(cleanupId)) { error = "busy:duplicate_operation"; return false; }
    for (auto const& [id, reservation] : _state->reservations)
    {
        (void)id;
        if (reservation.continuation.empty() || reservation.continuation == token) continue;
        auto owner = GetPvpOwner(reservation.continuation);
        if (owner && owner->cleanupId == cleanupId) { error = "busy:duplicate_operation"; return false; }
    }
    if (!_state->continuations.RequestCleanup(token, cleanupId, members, On(), _state->guild, _state->human, error))
        return false;
    auto owner = GetPvpOwner(token);
    if (owner && owner->stage == PvpStage::Clean)
        PublishJob({cleanupId, "leave_pvp", true, "", 0, owner->members.size(), owner->token});
    return true;
}

std::string RealmModule::ValidateContinuation(std::string_view token, std::uint32_t guid,
    ContinuationMode mode) const
{
    auto owner = GetPvpOwner(token);
    if (!owner) return "busy:operation_identity";
    if (auto reason = _state->continuations.Validate(token, mode, _state->Enabled(owner->domain),
        _state->EpochFor(owner->domain), _state->guild, _state->human);
        !reason.empty()) return reason;
    auto it = owner ? _state->reservations.find(owner->originId) : _state->reservations.end();
    if (it == _state->reservations.end() || it->second.continuation != token || it->second.retained
        || Holder(guid) != owner->originId) return "busy:operation_identity";
    auto lease = it->second.bodies.find(guid);
    if (lease == it->second.bodies.end()) return "busy:operation_identity";
    auto current = Identity(guid, true);
    auto scopes = it->second.pending.find(guid);
    auto check = CheckBodyLease(lease->second.body, lease->second.generation, current, _state->Generation(guid),
        scopes == it->second.pending.end() ? std::vector<BodyScope>{} : scopes->second);
    if (check == LeaseCheck::LifeChanged) return "busy:body_identity_changed";
    if (check == LeaseCheck::ScopeChanged) return "busy:body_scope_changed";
    auto reason = NativeMutationReason(guid);
    // Transit is permitted only inside an explicitly declared continuation
    // scope with the exact original connected body. Foreign owners never waive.
    if (reason == "busy:body_unavailable" && scopes != it->second.pending.end()
        && current->player && current->player->IsBeingTeleported() && !IsSelfBot(current->player)) return "";
    return reason;
}

bool RealmModule::DeclareContinuationScopes(std::string_view token, std::uint64_t revision,
    std::uint32_t guid, std::vector<BodyScope> scopes)
{
    if (_state->activeContinuation != token || _state->activeRevision != revision || scopes.size() > 4)
        return false;
    auto owner = GetPvpOwner(token);
    if (!owner || owner->revision != revision) return false;
    auto mode = owner->stage == PvpStage::Cleaning ? ContinuationMode::Cleanup : ContinuationMode::Application;
    if (!_state->continuations.Validate(token, mode, _state->Enabled(owner->domain),
        _state->EpochFor(owner->domain), _state->guild, _state->human).empty())
        return false;
    auto it = _state->reservations.find(owner->originId);
    if (it == _state->reservations.end() || it->second.retained || Holder(guid) != owner->originId) return false;
    auto lease = it->second.bodies.find(guid);
    auto current = Identity(guid, true);
    if (lease == it->second.bodies.end() || !current || !SameLife(*current, lease->second.body)
        || _state->Generation(guid) != lease->second.generation || !NativeOwnershipReason(guid).empty()) return false;
    it->second.pending[guid] = std::move(scopes);
    return true;
}

std::string RealmModule::ChargeContinuationBodies(std::string_view token,
    std::vector<std::uint32_t> const& members)
{
    if (!_state->activeBudget || _state->activeContinuation != token || members.empty())
        return "busy:operation_identity";
    auto owner = GetPvpOwner(token);
    if (!owner || owner->revision != _state->activeRevision) return "busy:operation_identity";
    auto mode = owner->stage == PvpStage::Cleaning ? ContinuationMode::Cleanup : ContinuationMode::Application;
    std::set<std::uint32_t> unique(members.begin(), members.end());
    if (unique.size() != members.size() || unique.contains(0)) return "bad field: affected_bodies";
    std::size_t extra = 0;
    for (auto guid : unique)
    {
        if (auto reason = ValidateContinuation(token, guid, mode); !reason.empty()) return reason;
        if (!_state->chargedBodies.contains(guid)) ++extra;
    }
    if (_state->chargedBodies.size() + extra > _state->activeBudget->Cap()) return "action_cost_exceeds_cap";
    if (!_state->activeBudget->Take(static_cast<std::uint32_t>(extra))) return "busy:tick_budget";
    _state->chargedBodies.insert(unique.begin(), unique.end());
    return "";
}

std::string RealmModule::Holder(std::uint32_t guid) const
{
    auto it = _state->holders.find(guid);
    return it == _state->holders.end() ? "" : it->second;
}

std::string RealmModule::MutationReason(std::uint32_t guid, std::string_view operation) const
{
    auto owner = Holder(guid);
    if (!owner.empty() && owner != operation)
        return "busy:realm_owner";
    if (!operation.empty())
    {
        if (auto reason = ValidateOwnedBody(guid, operation); !reason.empty())
            return reason;
    }
    auto reason = NativeMutationReason(guid);
    if (reason == "busy:body_unavailable" && !operation.empty())
    {
        auto const& reservation = _state->reservations.at(std::string(operation));
        auto const& lease = reservation.bodies.at(guid);
        auto current = Identity(guid, true);
        if (reservation.pending.contains(guid) && current && SameLife(*current, lease.body)
            && _state->Generation(guid) == lease.generation)
            return ""; // Only exact pending worldport; scope is checked by the queue before the callback.
    }
    return reason;
}

std::string RealmModule::ValidateOwnedBody(std::uint32_t guid, std::string_view operation) const
{
    auto it = _state->reservations.find(operation);
    if (it != _state->reservations.end() && !it->second.continuation.empty())
        return ValidateContinuation(it->second.continuation, guid, ContinuationMode::Application);
    if (operation.empty() || it == _state->reservations.end() || Holder(guid) != operation
        || it->second.retained || it->second.epoch != _state->EpochFor(it->second.admitted.domain))
        return "busy:operation_identity";
    auto lease = it->second.bodies.find(guid);
    if (lease == it->second.bodies.end()) return "busy:operation_identity";
    auto pending = it->second.pending.find(guid);
    auto current = Identity(guid, pending != it->second.pending.end());
    auto check = _state->CheckLease(it->second, guid, current,
        pending == it->second.pending.end() ? std::vector<BodyScope>{} : pending->second);
    if (check == LeaseCheck::LifeChanged) return "busy:body_identity_changed";
    if (check == LeaseCheck::ScopeChanged) return "busy:body_scope_changed";
    return "";
}

bool RealmOwns(std::uint32_t guid) { return !RealmModule::Instance().Holder(guid).empty(); }

std::string RealmModule::ChargeAffectedBodies(std::string_view operation,
    std::vector<std::uint32_t> const& guids)
{
    if (!_state->activeBudget || !_state->activeEntry || operation.empty()
        || operation != _state->activeOperation || guids.empty())
        return "busy:operation_identity";
    std::set<std::uint32_t> unique(guids.begin(), guids.end());
    if (unique.size() != guids.size() || unique.contains(0)) return "bad field: affected_bodies";
    std::size_t extra = 0;
    for (auto guid : unique)
    {
        if (auto reason = MutationReason(guid, operation); !reason.empty()) return reason;
        auto handler = _state->executors.find(_state->activeEntry->kind);
        if (handler == _state->executors.end()) return "unsupported";
        if (auto reason = handler->second.admit(*_state->activeEntry, guid); !reason.empty()) return reason;
        if (!_state->chargedBodies.contains(guid)) ++extra;
    }
    if (_state->chargedBodies.size() + extra > _state->activeBudget->Cap())
        return "action_cost_exceeds_cap";
    if (!_state->activeBudget->Take(static_cast<std::uint32_t>(extra))) return "busy:tick_budget";
    _state->chargedBodies.insert(unique.begin(), unique.end());
    return "";
}

std::string RealmModule::DeclareAffectedBodyScopes(std::string_view operation, std::uint32_t guid,
    std::vector<BodyScope> scopes)
{
    if (!_state->activeEntry || !_state->activeContinuation.empty() || operation.empty()
        || operation != _state->activeOperation || !_state->chargedBodies.contains(guid)
        || scopes.size() > 4) return "busy:operation_identity";
    if (auto why = MutationReason(guid, operation); !why.empty()) return why;
    auto found = _state->reservations.find(operation);
    if (found == _state->reservations.end() || !found->second.bodies.contains(guid)) return "busy:operation_identity";
    found->second.pending[guid] = std::move(scopes);
    found->second.declaredScopes.insert(guid);
    return "";
}

RealmStep RealmModule::PerformGuildGrowJoin(std::string_view operation, std::uint32_t guid,
    std::uint32_t destination)
{
    auto* active = _state->activeEntry;
    auto found = _state->reservations.find(operation);
    if (!active || operation.empty() || operation != _state->activeOperation
        || active->kind != "guild_grow" || active->domain != ActivityDomain::Realm
        || !_state->activeContinuation.empty() || !_state->chargedBodies.contains(guid)
        || active->cursor >= active->guids.size() || active->guids[active->cursor] != guid
        || found == _state->reservations.end()) return {StepState::Refused, "busy:operation_identity"};
    auto& reservation = found->second;
    auto const& admitted = reservation.admitted;
    if (admitted.kind != "guild_grow" || admitted.domain != ActivityDomain::Realm || admitted.id != active->id
        || admitted.guids != active->guids || admitted.payload != active->payload
        || admitted.payload.at("guild_id").get<std::uint32_t>() != destination
        || !reservation.bodies.contains(guid) || reservation.guildJoinAttempts.contains(guid))
        return {StepState::Refused, "busy:operation_identity"};
    if (auto why = MutationReason(guid, operation); !why.empty()) return {StepState::Refused, why};
    auto handler = _state->executors.find("guild_grow");
    if (handler == _state->executors.end()) return {StepState::Refused, "unsupported"};
    if (auto why = handler->second.admit(*active, guid); !why.empty()) return {StepState::Refused, why};
    auto* guild = sGuildMgr->GetGuildById(destination);
    auto const ranks = guild ? RealmGuildRankCount(*guild) : 0;
    auto const& lease = reservation.bodies.at(guid);
    if (!On() || destination == GuildId() || !_state->guilds.contains(destination) || !guild
        || IsRealGuild(destination) || !ranks || ranks > GUILD_RANKS_MAX_COUNT || lease.body.guild != 0)
        return {StepState::Refused, "refused_guild"};
    GuildJoinReceipt intended{std::string(operation), guid, destination, GuildId(), HumanGuid(),
        reservation.epoch, lease.generation, lease.body, static_cast<std::uint8_t>(ranks - 1)};
    // Install the attempted-call marker before native entry. Exceptions or
    // reentrant configuration changes must retain this exact whole manifest.
    reservation.guildJoinAttempts.insert(guid);
    bool const added = guild->AddMember(PlayerGuid(guid), GUILD_RANK_NONE);
    if (sGuildMgr->GetGuildById(destination) != guild || !On() || reservation.epoch != Epoch()
        || GuildId() != intended.configuredGuild || HumanGuid() != intended.human
        || !_state->guilds.contains(destination) || IsRealGuild(destination))
        return {StepState::Unknown, "guild_join_authority_changed"};
    auto current = Identity(guid);
    auto const* member = guild->GetMember(PlayerGuid(guid));
    auto const* character = sCharacterCache->GetCharacterCacheByGuid(PlayerGuid(guid));
    if (!added && !member && character && character->GuildId == 0
        && CheckBodyLease(lease.body, lease.generation, current, _state->Generation(guid), {}) == LeaseCheck::Exact)
    {
        reservation.guildJoinAttempts.erase(guid);
        return {StepState::Refused, "refused"};
    }
    if (!character || character->AccountId != lease.body.account || !member
        || member->GetGUID() != PlayerGuid(guid) || member->GetClass() != lease.body.classId
        || member->GetGender() != lease.body.sex || member->GetLevel() != 80
        || !ProveGuildJoin(intended, added, member != nullptr, member ? member->GetAccountId() : 0,
            member ? member->GetRankId() : 0, character->GuildId, current, _state->Generation(guid)))
        return {StepState::Unknown, "guild_join_unproved"};
    reservation.guildJoins.emplace(guid, std::move(intended));
    return {StepState::Complete, "", reservation.guildJoins.size()};
}

bool RealmModule::Accept(Queued entry, std::string& error)
{
    if (entry.domain != ActivityDomain::Realm || entry.kind == "guild_fishing")
    { error = "unsupported"; return false; }
    return AcceptDomain(std::move(entry), error);
}

bool RealmModule::AcceptDomain(Queued entry, std::string& error)
{
    error.clear();
    if (!_state->Enabled(entry.domain))
    { error = entry.domain == ActivityDomain::Guild ? "guild_off" : "realm_off"; return false; }
    if (!_state->started) { error = "realm_not_started"; return false; }
    if (entry.kind == "leave_pvp" && entry.payload.is_object() && entry.payload.contains("pvp_owner"))
    {
        auto const& token = entry.payload.at("pvp_owner");
        if (!token.is_string()) { error = "bad field: pvp_owner"; return false; }
        if (!token.get_ref<std::string const&>().empty())
            return RequestPvpCleanup(entry.id, token.get<std::string>(), entry.guids, error);
    }
    if (!_state->executors.contains(entry.kind)) { error = "unsupported"; return false; }
    for (auto const& [completed, outcome] : _state->guildOutcomes)
    {
        (void)outcome;
        if (completed.id != entry.id) continue;
        if (completed.domain == entry.domain && completed.kind == entry.kind
            && completed.guids == entry.guids && completed.payload == entry.payload) return true;
        error = "busy:duplicate_operation"; return false;
    }
    if (auto existing = _state->reservations.find(entry.id); existing != _state->reservations.end())
    {
        auto const& admitted = existing->second.admitted;
        if (admitted.domain == entry.domain && admitted.kind == entry.kind
            && admitted.guids == entry.guids && admitted.payload == entry.payload)
            return true; // Exact retry never schedules a second native action.
        error = "busy:duplicate_operation"; return false;
    }
    if (_state->continuations.CommandIdReserved(entry.id))
    { error = "operation_receipt_available"; return false; }
    // Unknown/pending work can leave the runnable queue while its exact lease
    // remains retained. Bound all outstanding reservations, not only runnable
    // entries, so repeated unknown outcomes cannot grow native ownership forever.
    if (_state->queue.Size() >= ActionQueue::Capacity
        || _state->reservations.size() + _state->guildOutcomes.size() >= ActionQueue::Capacity)
    { error = "busy:queue_full"; return false; }
    if (_state->reservations.contains(entry.id)) { error = "busy:duplicate_operation"; return false; }
    State::Reservation reservation;
    reservation.epoch = _state->EpochFor(entry.domain);
    reservation.admitted = entry;
    for (auto guid : entry.guids)
    {
        if (!(error = _state->executors.at(entry.kind).admit(entry, guid)).empty())
            return false;
        if (!(error = MutationReason(guid)).empty())
            return false;
        auto body = Identity(guid);
        if (!body) { error = "busy:body_identity"; return false; }
        reservation.bodies.emplace(guid, State::Lease{*body, _state->Generation(guid)});
    }
    auto const id = entry.id;
    // No yield splits admission, reservation and deferred queue insertion.
    for (auto const& [guid, lease] : reservation.bodies)
    {
        (void)lease;
        _state->holders.emplace(guid, id);
    }
    _state->reservations.emplace(id, std::move(reservation));
    _state->ApplyPins();
    if (std::any_of(entry.guids.begin(), entry.guids.end(), [](auto guid) { return !IsPinned(guid); }))
    {
        _state->Release(id);
        _state->ApplyPins();
        error = "pin_rejected";
        return false;
    }
    if (!_state->queue.Push(std::move(entry)))
    {
        _state->Release(id);
        _state->ApplyPins();
        error = "bad field: queued_operation";
        return false;
    }
    return true;
}

bool RealmModule::SetActors(ActorsCmd const& command, std::string& error)
{
    if (!On()) { error = "realm_off"; return false; }
    std::unordered_set<std::uint32_t> actors(command.guids.begin(), command.guids.end());
    if (actors.size() != command.guids.size() || actors.size() > 16 || command.guilds.size() > 3)
    { error = "bad field: actors"; return false; }
    for (auto guid : actors)
    {
        if (!guid || guid == HumanGuid() || IsCast(guid) || IsConfiguredGuildMember(guid))
        { error = "cast"; return false; }
        if (!IsOnRandomBotAccount(guid)) { error = "not_bot"; return false; }
        if (!_state->actors.contains(guid) && !(error = MutationReason(guid)).empty())
            return false;
    }
    for (auto guid : command.gankers)
        if (!actors.contains(guid)) { error = "bad field: gankers"; return false; }
    for (auto guild : command.guilds)
        if (!guild || guild == GuildId() || IsRealGuild(guild)) { error = "real_guild"; return false; }
    auto oldActors = _state->actors;
    _state->actors = actors;
    _state->ApplyPins();
    if (std::any_of(actors.begin(), actors.end(), [](auto guid) { return !IsPinned(guid); }))
    {
        _state->actors = std::move(oldActors);
        _state->ApplyPins();
        error = "pin_rejected";
        return false;
    }
    _state->gankers = {command.gankers.begin(), command.gankers.end()};
    _state->guilds = {command.guilds.begin(), command.guilds.end()};
    LOG_INFO("module.leeroy", "mod-leeroy: realm actors {}, individually pinned {}", actors.size(), actors.size());
    return true;
}

bool RealmModule::IsActor(std::uint32_t guid) const
{
    return _state->actors.contains(guid) && !IsConfiguredGuildMember(guid);
}
bool RealmModule::Watched(std::uint32_t guid) const { return guid == HumanGuid() || IsCast(guid) || Pins().Watched(guid); }
std::unordered_set<std::uint32_t> const& RealmModule::Gankers() const { return _state->gankers; }
std::unordered_set<std::uint32_t> const& RealmModule::RealmGuilds() const { return _state->guilds; }
std::vector<Place> const& RealmModule::Places() const { return _state->places; }
std::vector<ItemRule> const& RealmModule::Items() const { return _state->items; }
WeaponRecord const* RealmModule::Weapons(std::uint32_t guid) const
{
    auto it = _state->weapons.find(guid);
    return it == _state->weapons.end() ? nullptr : &it->second;
}
bool RealmModule::RecordWeapons(std::uint32_t guid, std::string_view operation)
{
    if (!MutationReason(guid, operation).empty() || (!IsCast(guid) && !IsActor(guid)))
        return false;
    auto now = ReadWeapons(FindOnline(guid));
    if (!ShouldRecord(now))
        return false;
    _state->weapons[guid] = {now.mainHand, now.offHand, now.ranged};
    return true;
}
GuardState* RealmModule::Guard()
{
    auto it = _state->guards.find({_state->guild, _state->human});
    return it == _state->guards.end() ? nullptr : it->second.get();
}
void RealmModule::PublishJob(RealmJob const& job)
{
    // Off-tick lifetime checks and configuration fencing still update retained
    // owners, but cannot emit unsolicited Realm events. Explicit owner queries
    // publish their read-only snapshot directly and remain available while off.
    if (On()) Publish(MakeRealmJob(job));
}
bool RealmModule::RegisterPoll(std::string name, std::uint32_t intervalMs, std::function<void()> poll)
{
    return !name.empty() && intervalMs && poll && _state->polls.size() < 16
        && _state->polls.emplace(std::move(name), State::Poll{intervalMs, 0, std::move(poll)}).second;
}
void RealmModule::RecordKill(Kill kill)
{
    std::lock_guard lock(_state->factsMutex);
    // No caller can supply its own proof id/publication time. Only the world
    // drain completes a proof after actual map-thread DONE verification.
    if (On() && Epoch() == kill.nativeEpoch && kill.killId.empty() && !kill.publishedAtMs
        && ValidKillObservation(kill) && _state->killInbox.size() < 128)
        _state->killInbox.push_back(std::move(kill));
}
std::optional<Kill> RealmModule::MatchProof(AnnounceCmd const& request, std::uint64_t nowMs) const
{
    _state->DrainKills();
    if (!On() || request.nativeEpoch != Epoch() || request.guildId != GuildId()
        || request.humanGuid != HumanGuid()) return std::nullopt;
    return _state->kills.MatchProof(request.killId, request, nowMs);
}
void RealmModule::NotifyBodyChanged(std::uint32_t guid)
{
    if (!guid || !Watched(guid))
        return;
    std::lock_guard lock(_state->factsMutex);
    ++_state->generations[guid];
}

void RealmModule::TickContinuations(TickBudget& budget)
{
    bool const advancePriority = budget.Left() == budget.Cap();
    _state->continuations.Visit(20, [&](PvpOwner const& owner)
    {
        auto found = _state->reservations.find(owner.originId);
        if (found == _state->reservations.end()) return;
        auto& reservation = found->second;
        if (owner.stage == PvpStage::Unknown || reservation.retained) return;
        auto unknown = [&](std::string reason)
        {
            reservation.retained = true;
            _state->continuations.Unknown(owner.token, reason);
            PublishOutcome(reservation.admitted, {owner.cleanupId.empty() ? owner.originId : owner.cleanupId,
                owner.cleanupId.empty() ? reservation.admitted.kind : "leave_pvp", false, std::move(reason),
                0, 0, owner.token});
        };
        // Check unchanged lifetime before the adapter reconciles an exact native
        // transition. Scope reconciliation needs its causal native receipts and
        // is checked for the entire manifest after the callback.
        for (auto const& [guid, lease] : reservation.bodies)
        {
            auto current = Identity(guid, true);
            if (!current || !SameLife(*current, lease.body) || _state->Generation(guid) != lease.generation
                || Holder(guid) != owner.originId)
            { unknown("body_identity_changed"); return; }
        }
        if (!_state->Enabled(owner.domain) || (owner.applicationFenced && owner.cleanupId.empty()
            && !owner.internalCleanup)) return;
        auto executor = _state->continuationHandlers.find(owner.kind);
        if (executor == _state->continuationHandlers.end()) { unknown("continuation_unavailable"); return; }
        struct ActiveContinuation
        {
            State& state;
            ActiveContinuation(State& owner, TickBudget& budget, Queued const& entry, PvpOwner const& continuation)
                : state(owner)
            {
                state.activeBudget = &budget;
                state.activeEntry = &entry;
                state.activeOperation = continuation.originId;
                state.activeContinuation = continuation.token;
                state.activeRevision = continuation.revision;
                state.chargedBodies.clear(); // Observations cost no mutation units.
            }
            ~ActiveContinuation()
            {
                state.activeBudget = nullptr;
                state.activeEntry = nullptr;
                state.activeOperation.clear();
                state.activeContinuation.clear();
                state.activeRevision = 0;
                state.chargedBodies.clear();
            }
        } active(*_state, budget, reservation.admitted, owner);
        PvpContinuationStep result;
        try { result = executor->second(owner); }
        catch (...) { unknown("operation_outcome_unknown"); return; }
        if (result.stage == PvpStage::Unknown) { unknown(result.reason); return; }
        auto mode = owner.stage == PvpStage::Cleaning ? ContinuationMode::Cleanup : ContinuationMode::Application;
        for (auto guid : owner.members)
            if (auto reason = ValidateContinuation(owner.token, guid, mode); !reason.empty())
            { unknown(std::move(reason)); return; }
        if (result.stage == PvpStage::Clean)
        {
            auto const admitted = reservation.admitted;
            // The adapter guard remains alive through native identity checks,
            // terminal receipt allocation and the one holder-map release.
            // Missing/stale proof cannot release even an apparently empty body.
            if (!result.clean || result.clean->token != owner.token
                || !_state->continuations.CanRelease(*result.clean, bool(result.clean->quiescence)))
            { unknown("cleanup_proof_unavailable"); return; }
            try
            {
                // Match the map/session lifecycle writer while the native
                // inbox guard is held. No generation can change between this
                // final comparison and erasing the exact holder manifest.
                std::lock_guard lifecycle(_state->factsMutex);
                for (auto const& [guid, lease] : reservation.bodies)
                {
                    auto generation = _state->generations.find(guid);
                    if ((generation == _state->generations.end() ? 0 : generation->second) != lease.generation)
                    { unknown("body_identity_changed"); return; }
                }
                if (!_state->continuations.Commit(*result.clean))
                { unknown("cleanup_proof_changed"); return; }
                _state->Release(owner.originId);
            }
            catch (...) { unknown("cleanup_receipt_unavailable"); return; }
            result.clean->quiescence.reset();
            PublishOutcome(admitted, {owner.domain == ActivityDomain::Guild ? owner.originId : owner.cleanupId,
                owner.domain == ActivityDomain::Guild ? admitted.kind : "leave_pvp", true, "", 0,
                owner.members.size(), owner.token});
            _state->ApplyPins();
            return;
        }
        if (!_state->continuations.Advance(owner.token, owner.revision, result.stage, std::move(result.reason)))
            unknown("continuation_transition_unknown");
    }, advancePriority);
}

void RealmModule::Tick(std::uint32_t diff)
{
    _state->DrainKills();
    ReconcileConfiguration();
    if (!On() && !GuildOn())
    {
        TickBudget noMutations(1);
        TickContinuations(noMutations); // Only same-life checks run while disabled.
        if (Due(_state->pinRemaining, diff, 1000)) _state->ApplyPins();
        DrainGuildOutcomes();
        return;
    }
    TickBudget budget(Config().realmMaxActionsPerTick);
    // Rotate which lane receives an untouched mutation budget. A large atomic
    // closure cannot be starved forever by ordinary one-body work (or vice versa).
    _state->continuationsFirst = !_state->continuationsFirst;
    if (_state->continuationsFirst) TickContinuations(budget);
    _state->queue.Drain(budget, [&](Queued& entry)
    {
        auto found = _state->reservations.find(entry.id);
        if (found == _state->reservations.end() || entry.cursor >= entry.guids.size())
        {
            PublishOutcome(entry, {entry.id, entry.kind, false, "operation_identity"});
            return true;
        }
        auto& reservation = found->second;
        auto const guid = entry.guids[entry.cursor];
        auto guildManifestClean = [&]
        {
            if (reservation.guildJoinAttempts.empty()) return true;
            if (entry.kind != "guild_grow" || reservation.bodies.size() != reservation.admitted.guids.size()
                || entry.guids != reservation.admitted.guids
                || reservation.retained || reservation.epoch != _state->EpochFor(entry.domain)) return false;
            return GuildJoinManifestClean(reservation.admitted.guids, reservation.guildJoinAttempts,
                reservation.guildJoins, [&](std::uint32_t member)
            {
                return reservation.bodies.contains(member) && Holder(member) == entry.id
                    && NativeMutationReason(member).empty()
                    && _state->CheckLease(reservation, member, Identity(member), {}) == LeaseCheck::Exact;
            });
        };
        auto finish = [&](std::string reason, bool retain, bool refused = false)
        {
            auto const knownJoins = reservation.guildJoins.size();
            if (!refused && !reservation.declaredScopes.empty()) retain = true;
            if (!guildManifestClean()) retain = true;
            if (!reservation.continuation.empty())
            {
                _state->continuations.Unknown(reservation.continuation, reason);
                _state->continuations.Detach(reservation.continuation);
                retain = true; // A prepared native source needs exact cleanup.
            }
            if (retain) reservation.retained = true;
            else _state->Release(entry.id);
            PublishOutcome(entry, {entry.id, entry.kind, false, std::move(reason), guid, knownJoins});
            return true;
        };
        bool const pending = reservation.pending.contains(guid);
        auto const lease = reservation.bodies.find(guid);
        auto before = Identity(guid, pending);
        auto reason = MutationReason(guid, entry.id);
        if (!reason.empty()) return finish(reason, pending);
        if (!before || lease == reservation.bodies.end()
            || _state->CheckLease(reservation, guid, before,
                pending ? reservation.pending.at(guid) : std::vector<BodyScope>{}) != LeaseCheck::Exact)
            return finish("body_identity_changed", pending);
        auto executor = _state->executors.find(entry.kind);
        if (executor == _state->executors.end()) return finish("unsupported", pending);
        if (auto admission = executor->second.admit(entry, guid); !admission.empty())
            return finish(admission, pending);
        RealmStep result;
        struct ActiveStep
        {
            State& state;
            ActiveStep(State& owner, TickBudget& budget, Queued const& entry, std::uint32_t current)
                : state(owner)
            {
                state.activeBudget = &budget;
                state.activeEntry = &entry;
                state.activeOperation = entry.id;
                state.chargedBodies = {current};
            }
            ~ActiveStep()
            {
                state.activeBudget = nullptr;
                state.activeEntry = nullptr;
                state.activeOperation.clear();
                state.activeContinuation.clear();
                state.activeRevision = 0;
                state.chargedBodies.clear();
            }
        } active(*_state, budget, entry, guid);
        try { result = executor->second.execute(entry, guid); }
        catch (...) { return finish("operation_outcome_unknown", true); }
        if (!reservation.continuation.empty())
        {
            if (result.state != StepState::Detached)
                return finish(result.reason.empty() ? "continuation_disposition_unknown" : result.reason, true);
            for (auto const& [member, saved] : reservation.bodies)
            {
                (void)saved;
                if (auto why = ValidateContinuation(reservation.continuation, member, ContinuationMode::Application);
                    !why.empty()) return finish(std::move(why), true);
            }
            _state->continuations.Detach(reservation.continuation);
            return true; // Stable reservation, holders and pins never leave.
        }
        if (result.state == StepState::Detached) return finish("continuation_unprepared", true);
        auto after = Identity(guid, result.state == StepState::Pending);
        auto expectedScopes = result.expectedScopes;
        if (reservation.declaredScopes.contains(guid))
            for (auto const& scope : reservation.pending.at(guid))
                if (std::find(expectedScopes.begin(), expectedScopes.end(), scope) == expectedScopes.end())
                    expectedScopes.push_back(scope);
        // The pre-step full lease check already ran. Post-step scope comes
        // only from the executor's explicit transition receipt below.
        auto afterReason = NativeMutationReason(guid);
        if (Holder(guid) != entry.id || reservation.retained
            || reservation.epoch != _state->EpochFor(entry.domain))
            afterReason = "busy:operation_identity";
        // The first TeleportTo may make the original Player transiently out of
        // world before its Pending receipt is saved. No other failed fence is
        // waived, and stable identity plus exact expected scope still follows.
        if (afterReason == "busy:body_unavailable" && result.state == StepState::Pending && after
            && SameLife(*after, lease->second.body) && _state->Generation(guid) == lease->second.generation)
            afterReason.clear();
        if (expectedScopes.size() > 4 || !after || !afterReason.empty()
            || _state->CheckLease(reservation, guid, after, expectedScopes) != LeaseCheck::Exact)
            return finish("operation_transition_unknown", true);
        if (result.state == StepState::Unknown)
            return finish(result.reason.empty() ? "native_cleanup_unproved" : result.reason, true);
        if (result.state == StepState::Pending)
        {
            reservation.pending[guid] = std::move(expectedScopes);
            return false;
        }
        if (result.state == StepState::Refused)
            return finish(result.reason.empty() ? "refused" : result.reason, false, true);
        if (!reservation.declaredScopes.contains(guid)) reservation.pending.erase(guid);
        reservation.value = result.value;
        ++entry.cursor;
        if (entry.cursor < entry.guids.size())
            return false;
        if (!guildManifestClean()) return finish("guild_manifest_unproved", true);
        auto value = reservation.value;
        _state->Release(entry.id);
        PublishOutcome(entry, {entry.id, entry.kind, true, "", entry.guids.size() == 1 ? guid : 0, value});
        return true;
    });
    if (!_state->continuationsFirst) TickContinuations(budget);
    DrainGuildOutcomes();
    if (!On()) return; // Guild availability never enables Realm polling/events.
    if (Due(_state->flagRemaining, diff, 1000))
    {
        auto human = Human();
        if (!human.online) _state->humanFlag.reset();
        else if (!_state->humanFlag || *_state->humanFlag != Flagged(human))
        {
            _state->humanFlag = Flagged(human);
            Publish(MakePvpFlag(HumanGuid(), *_state->humanFlag));
        }
    }
    if (Due(_state->weaponsRemaining, diff, 60000))
    {
        auto guids = CastMembers();
        guids.insert(_state->actors.begin(), _state->actors.end());
        for (auto guid : guids)
            RecordWeapons(guid);
        _state->ApplyPins(); // Recheck retained foreign exclusions only after their producer clears them.
    }
    for (auto& [name, poll] : _state->polls)
    {
        (void)name;
        if (Due(poll.remaining, diff, poll.interval))
            poll.fn();
    }
}

void RealmModule::ReconcileConfiguration()
{
    bool const realmChanged = _state->disablePending;
    bool const guildChanged = _state->guildDisablePending;
    if (!realmChanged && !guildChanged) return;
    _state->disablePending = _state->guildDisablePending = false;
    for (auto const& [id, reservation] : _state->reservations)
    {
        (void)id;
        if (reservation.continuation.empty()) continue;
        auto owner = GetPvpOwner(reservation.continuation);
        if (realmChanged && owner && owner->domain == ActivityDomain::Realm && !owner->cleanupId.empty())
            PublishJob({owner->cleanupId, "leave_pvp", false, "realm_off", 0, 0, owner->token});
    }
    if (realmChanged) _state->continuations.FenceDomain(ActivityDomain::Realm, "realm_off");
    if (guildChanged) _state->continuations.FenceDomain(ActivityDomain::Guild, "guild_off");
    _state->queue.DropIf([&](Queued const& entry)
    { return entry.domain == ActivityDomain::Guild ? guildChanged : realmChanged; }, "configuration_changed",
        [&](Queued const& entry, std::string const&)
    {
        auto it = _state->reservations.find(entry.id);
        RealmJob outcome{entry.id, entry.kind, false,
            entry.domain == ActivityDomain::Guild ? "guild_off" : "realm_off"};
        if (it != _state->reservations.end() && !it->second.continuation.empty())
        {
            _state->continuations.Detach(it->second.continuation);
            if (entry.domain == ActivityDomain::Guild) return; // Finite cleanup owns its one terminal outcome.
        }
        else if (it != _state->reservations.end())
            outcome = CancelGuildJoinReservation(std::move(outcome),
                !it->second.pending.empty() || it->second.retained,
                it->second.guildJoinAttempts, it->second.guildJoins,
                [&] { it->second.retained = true; }, [&] { _state->Release(entry.id); });
        else
            _state->Release(entry.id);
        PublishOutcome(entry, outcome);
    });
    // Actor/guild declarations belong to the old enable/configured-identity
    // epoch. An enable or identity switch requires a fresh declaration.
    if (realmChanged)
    {
        _state->actors.clear();
        _state->gankers.clear();
        _state->guilds.clear();
        _state->weapons.clear();
    }
    _state->ApplyPins();
}

void RealmModule::Shutdown()
{
    {
        std::lock_guard lock(_state->factsMutex);
        _state->on = false;
        _state->guildOn = false;
        ++_state->epoch;
        ++_state->guildEpoch;
        _state->disablePending = true;
        _state->guildDisablePending = true;
        _state->killInbox.clear();
        _state->kills = {};
    }
    ReconcileConfiguration(); // Retain unresolved operations; never fake restoration or run another action.
}

void RegisterRealmContinuationHandlers()
{
    RegisterCommand("get_realm_pvp", [](nlohmann::json const& message) -> LeeroyCommandResult
    {
        std::string error;
        auto command = ParseGetRealmPvp(message, error);
        if (!command) return {false, error};
        auto& module = RealmModule::Instance();
        auto owner = command->pvpOwner.empty() ? module.FindPvpOwner(command->originId)
            : module.GetPvpOwner(command->pvpOwner);
        if (!owner || owner->domain != ActivityDomain::Realm) return {false, "owner_unavailable"};
        // Exact read-only query works while disabled, so retained owners remain
        // discoverable. No wire status or caller-local state can authorize release.
        Publish(MakeRealmPvp(message.at("id").get<std::string>(), *owner));
        return {true, ""};
    });
}
}
