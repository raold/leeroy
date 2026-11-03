// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmContinuations.h"
#include <algorithm>
#include <iomanip>
#include <limits>
#include <sstream>

namespace Leeroy::Realm
{
namespace
{
bool ValidManifest(std::vector<std::uint32_t> const& members)
{
    return !members.empty() && members.size() <= 150 && members.front() != 0
        && std::adjacent_find(members.begin(), members.end()) == members.end();
}
}
ContinuationLedger::ContinuationLedger(std::string bootNonce) : _nonce(std::move(bootNonce)) { }
bool ContinuationLedger::ValidToken(std::string_view token)
{
    return token.size() == 49 && token[32] == '-' && std::all_of(token.begin(), token.end(), [](char c)
    { return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || c == '-'; })
        && token.find('-') == 32 && token.rfind('-') == 32;
}
bool ContinuationLedger::SameManifest(std::vector<std::uint32_t> first, std::vector<std::uint32_t> second)
{
    std::sort(first.begin(), first.end());
    std::sort(second.begin(), second.end());
    return ValidManifest(first) && ValidManifest(second) && first == second;
}
std::optional<ContinuationOwner> ContinuationLedger::Prepare(std::string originId, ContinuationKind kind,
    std::vector<std::uint32_t> members, std::uint64_t epoch, std::uint32_t guild, std::uint32_t human,
    std::size_t totalReservations, std::string& error)
{
    error.clear();
    std::sort(members.begin(), members.end());
    if (originId.empty() || originId.size() > 128 || !ValidManifest(members) || !epoch || !guild || !human)
    { error = "bad field: continuation"; return std::nullopt; }
    if (auto old = FindOrigin(originId))
    {
        if (old->stage == PvpStage::Clean) { error = "operation_receipt_available"; return std::nullopt; }
        if (old->kind == kind && old->members == members && old->originEpoch == epoch
            && old->guild == guild && old->human == human) return old;
        error = "busy:duplicate_operation"; return std::nullopt;
    }
    if (!totalReservations || totalReservations > ActionQueue::Capacity || _owners.size() >= totalReservations)
    { error = "busy:queue_full"; return std::nullopt; }
    if (_serial == std::numeric_limits<std::uint64_t>::max() || _nonce.size() != 32
        || !ValidToken(_nonce + "-0000000000000001"))
    { error = "owner_token_unavailable"; return std::nullopt; }
    std::ostringstream token;
    token << _nonce << '-' << std::hex << std::setfill('0') << std::setw(16) << ++_serial;
    ContinuationOwner owner{token.str(), std::move(originId), kind, epoch, 1, guild, human, std::move(members)};
    _owners.emplace(owner.token, Record{owner});
    return owner;
}
std::optional<ContinuationOwner> ContinuationLedger::Get(std::string_view token) const
{
    auto it = _owners.find(token);
    if (it != _owners.end()) return it->second.owner;
    for (auto const& record : _terminal) if (record.owner.token == token) return record.owner;
    return std::nullopt;
}
std::optional<ContinuationOwner> ContinuationLedger::FindOrigin(std::string_view originId) const
{
    for (auto const& [token, record] : _owners)
        if (record.owner.originId == originId) return record.owner;
    for (auto const& record : _terminal) if (record.owner.originId == originId) return record.owner;
    return std::nullopt;
}
bool ContinuationLedger::CommandIdReserved(std::string_view id) const
{
    auto matches = [&](Record const& record)
    {
        return record.owner.originId == id || record.owner.cleanupId == id
            || std::find(record.canceledControllers.begin(), record.canceledControllers.end(), id)
                != record.canceledControllers.end();
    };
    for (auto const& [token, record] : _owners) { (void)token; if (matches(record)) return true; }
    return std::any_of(_terminal.begin(), _terminal.end(), matches);
}
bool ContinuationLedger::Detach(std::string_view token)
{
    auto it = _owners.find(token);
    if (it == _owners.end()) return false;
    it->second.detached = true;
    return true;
}
bool ContinuationLedger::IsDetached(std::string_view token) const
{
    auto it = _owners.find(token);
    return it != _owners.end() && it->second.detached;
}
bool ContinuationLedger::Bump(ContinuationOwner& owner)
{
    if (owner.revision == std::numeric_limits<std::uint64_t>::max())
    {
        owner.applicationFenced = true;
        owner.stage = PvpStage::Unknown;
        owner.reason = "owner_revision_exhausted";
        return false;
    }
    ++owner.revision;
    return true;
}
std::string ContinuationLedger::Validate(std::string_view token, ContinuationMode mode, bool enabled,
    std::uint64_t epoch, std::uint32_t guild, std::uint32_t human) const
{
    auto it = _owners.find(token);
    if (it == _owners.end()) return "busy:operation_identity";
    auto const& owner = it->second.owner;
    if (!enabled) return "realm_off";
    if (owner.guild != guild || owner.human != human) return "busy:owner_authority";
    if (owner.stage == PvpStage::Unknown) return "busy:owner_unknown";
    if (mode == ContinuationMode::Cleanup)
        return owner.stage == PvpStage::Cleaning && !owner.cleanupId.empty() ? "" : "busy:cleanup_controller";
    return !owner.applicationFenced && owner.originEpoch == epoch ? "" : "busy:application_fenced";
}
bool ContinuationLedger::RequestCleanup(std::string_view token, std::string cleanupId,
    std::vector<std::uint32_t> members, bool enabled, std::uint32_t guild, std::uint32_t human, std::string& error)
{
    error.clear();
    auto owner = Get(token);
    if (!owner) { error = "owner_unavailable"; return false; }
    if (cleanupId.empty() || cleanupId.size() > 128 || !SameManifest(owner->members, std::move(members)))
    { error = "bad field: cleanup_manifest"; return false; }
    if (!enabled) { error = "realm_off"; return false; }
    if (guild != owner->guild || human != owner->human) { error = "busy:owner_authority"; return false; }
    auto conflicts = [&](Record const& other)
    {
        return other.owner.originId == cleanupId || (other.owner.token != token && other.owner.cleanupId == cleanupId)
            || std::find(other.canceledControllers.begin(), other.canceledControllers.end(), cleanupId)
                != other.canceledControllers.end();
    };
    for (auto const& [key, record] : _owners)
    {
        (void)key;
        if (conflicts(record)) { error = "busy:duplicate_operation"; return false; }
    }
    for (auto const& other : _terminal)
        if (conflicts(other)) { error = "busy:duplicate_operation"; return false; }
    if (owner->stage == PvpStage::Clean)
    {
        if (owner->cleanupId == cleanupId) return true;
        error = "busy:cleanup_controller"; return false;
    }
    if (owner->stage == PvpStage::Unknown) { error = "busy:owner_unknown"; return false; }
    if (!owner->cleanupId.empty())
    {
        if (owner->cleanupId == cleanupId) return true;
        error = "busy:cleanup_controller"; return false;
    }
    auto& current = _owners.at(owner->token).owner;
    current.applicationFenced = true;
    current.cleanupId = std::move(cleanupId);
    current.stage = PvpStage::Cleaning;
    current.reason.clear();
    if (!Bump(current)) { error = current.reason; return false; }
    return true;
}
bool ContinuationLedger::Advance(std::string_view token, std::uint64_t revision, PvpStage stage, std::string reason)
{
    auto it = _owners.find(token);
    if (it == _owners.end() || it->second.owner.revision != revision) return false;
    auto& owner = it->second.owner;
    if (stage == PvpStage::Unknown) { Unknown(token, std::move(reason)); return true; }
    if (owner.stage == PvpStage::Unknown || stage == PvpStage::Clean) return false;
    // Cleanup deliberately fences application, but can wait on budget or native
    // worldport progress. Diagnostic changes invalidate older cleanup proofs;
    // repeated identical observations do not consume another revision.
    if (owner.stage == stage)
    {
        if (stage == PvpStage::Cleaning)
        {
            if (!owner.applicationFenced || owner.cleanupId.empty()) return false;
        }
        else if (owner.applicationFenced) return false;
        reason = reason.substr(0, 128);
        if (owner.reason == reason) return true;
        owner.reason = std::move(reason);
        return Bump(owner);
    }
    if (owner.applicationFenced || stage == PvpStage::Cleaning
        || static_cast<unsigned>(stage) < static_cast<unsigned>(owner.stage)) return false;
    owner.stage = stage;
    owner.reason = reason.substr(0, 128);
    return Bump(owner);
}
void ContinuationLedger::Unknown(std::string_view token, std::string reason)
{
    auto it = _owners.find(token);
    if (it == _owners.end() || it->second.owner.stage == PvpStage::Unknown) return;
    auto& owner = it->second.owner;
    owner.stage = PvpStage::Unknown;
    owner.applicationFenced = true;
    owner.reason = reason.empty() ? "native_cleanup_unproved" : reason.substr(0, 128);
    Bump(owner);
}
void ContinuationLedger::FenceAll(std::string reason)
{
    for (auto& [token, record] : _owners)
    {
        (void)token;
        auto& owner = record.owner;
        if (owner.stage == PvpStage::Unknown) continue;
        owner.applicationFenced = true;
        if (!owner.cleanupId.empty())
        {
            if (record.canceledControllers.size() == 16)
            { Unknown(owner.token, "cleanup_history_exhausted"); continue; }
            record.canceledControllers.push_back(owner.cleanupId);
        }
        // The old controller remains a reserved id even after its failed job.
        owner.cleanupId.clear();
        if (owner.stage == PvpStage::Cleaning) owner.stage = PvpStage::Ending;
        owner.reason = reason.substr(0, 128);
        Bump(owner);
    }
}
bool ContinuationLedger::CanCommit(ContinuationCleanReceipt const& receipt) const
{
    auto it = _owners.find(receipt.token);
    if (it == _owners.end()) return false;
    auto const& owner = it->second.owner;
    return owner.stage == PvpStage::Cleaning && owner.applicationFenced && !owner.cleanupId.empty()
        && owner.cleanupId == receipt.cleanupId && owner.revision == receipt.revision
        && SameManifest(owner.members, receipt.members);
}
bool ContinuationLedger::Commit(ContinuationCleanReceipt const& receipt)
{
    if (!CanCommit(receipt)) return false;
    auto record = _owners.at(receipt.token);
    auto& owner = record.owner;
    if (!Bump(owner)) { Unknown(receipt.token, owner.reason); return false; }
    owner.stage = PvpStage::Clean;
    owner.reason.clear();
    if (_terminal.size() == ActionQueue::Capacity) _terminal.pop_front();
    _terminal.push_back(std::move(record));
    _owners.erase(receipt.token);
    return true;
}
bool ContinuationLedger::CanRelease(ContinuationCleanReceipt const& receipt, bool nativeQuiescent) const
{ return nativeQuiescent && CanCommit(receipt); }
void ContinuationLedger::Visit(std::size_t limit, std::function<void(ContinuationOwner const&)> const& visit,
    bool advancePriority)
{
    if (!visit || _owners.empty()) return;
    limit = std::min({limit, std::size_t{20}, _owners.size()});
    std::vector<std::string> tokens;
    auto next = _owners.upper_bound(_cursor);
    for (std::size_t count = 0; count < _owners.size() && tokens.size() < limit; ++count)
    {
        if (next == _owners.end()) next = _owners.begin();
        if (next->second.detached) tokens.push_back(next->first);
        ++next;
    }
    // Rotate first opportunity even when every owner fits in this observation
    // batch. Otherwise the same first owner can consume the entire mutation
    // budget on every tick. Every detached owner becomes first within Capacity
    // visits; the mutation and observation bounds remain independent.
    if (advancePriority && !tokens.empty()) _cursor = tokens.front();
    for (auto const& token : tokens)
    {
        auto it = _owners.find(token);
        if (it != _owners.end()) { auto snapshot = it->second.owner; visit(snapshot); }
    }
}
std::string_view PvpStageName(PvpStage stage)
{
    switch (stage)
    {
        case PvpStage::Acquiring: return "acquiring";
        case PvpStage::Queued: return "queued";
        case PvpStage::Invited: return "invited";
        case PvpStage::Entering: return "entering";
        case PvpStage::Active: return "active";
        case PvpStage::Ending: return "ending";
        case PvpStage::Cleaning: return "cleaning";
        case PvpStage::Clean: return "clean";
        case PvpStage::Unknown: return "unknown";
    }
    return "unknown";
}
std::string_view PvpKindName(PvpKind kind)
{
    switch (kind)
    {
        case PvpKind::Wintergrasp: return "wintergrasp";
        case PvpKind::RatedArena: return "rated_arena";
        case PvpKind::Fishing: return "fishing";
        case PvpKind::Duel: return "duel";
    }
    return "unknown";
}
}
