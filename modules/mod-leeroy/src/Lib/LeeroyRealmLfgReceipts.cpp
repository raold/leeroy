// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmLfgReceipts.h"
#include <algorithm>
#include <limits>

namespace Leeroy::Realm
{
namespace
{
bool Valid(LfgObservedLife const& life) { return life.guid && life.player && life.session; }
bool Contains(LfgApplicantReceipt const& receipt, std::uint32_t guid)
{
    return std::any_of(receipt.members.begin(), receipt.members.end(), [&](auto const& life) { return life.guid == guid; });
}
}
std::vector<std::uint32_t> LfgReceiptGuids(LfgApplicantReceipt const& receipt)
{
    std::vector<std::uint32_t> result;
    for (auto const& life : receipt.members) result.push_back(life.guid);
    std::sort(result.begin(), result.end());
    return result;
}
void LfgReceiptBook::SetEpoch(std::uint64_t epoch)
{
    if (_epoch == epoch) return;
    _epoch = epoch;
    _records.clear();
    for (auto& [operation, hold] : _holds) { (void)operation; hold.poisoned = true; }
    // Existing holders survive a configuration boundary. Lost observations
    // cannot be recovered by resetting an exhaustion flag within that epoch.
    _exhausted = false;
}
void LfgReceiptBook::Overflow()
{
    _exhausted = true; _records.clear();
    for (auto& [operation, hold] : _holds) { (void)operation; hold.poisoned = true; }
}
void LfgReceiptBook::Poison(std::uint64_t group)
{
    _records.erase(group);
    for (auto& [operation, hold] : _holds)
    {
        (void)operation;
        if (hold.receipt.group == group) hold.poisoned = true;
    }
}
void LfgReceiptBook::Born(std::uint64_t group, std::uintptr_t object, LfgObservedLife leader)
{
    if (_records.count(group)) { Poison(group); return; }
    if (_exhausted || !_epoch || !group || !object || !Valid(leader)) return;
    if (_records.size() >= Capacity || _serial == std::numeric_limits<std::uint64_t>::max()) { Overflow(); return; }
    if (Fenced(group, {leader.guid})) { Poison(group); return; }
    Record record;
    record.receipt = {group, ++_serial, 0, _epoch, object, leader.guid, false, {leader}};
    _records.emplace(group, std::move(record));
}
void LfgReceiptBook::Added(std::uint64_t group, std::uintptr_t object, LfgObservedLife member)
{
    auto found = _records.find(group);
    if (found == _records.end()) { if (Fenced(group, {member.guid})) Poison(group); return; }
    auto& record = found->second;
    if (!Valid(member) || record.joined || record.receipt.object != object
        || record.receipt.members.size() >= MemberCapacity || Contains(record.receipt, member.guid)) { Poison(group); return; }
    record.receipt.members.push_back(member);
}
void LfgReceiptBook::ObserveGroup(std::uint64_t group, std::vector<std::uint32_t> const& members)
{
    for (auto& [key, hold] : _holds)
    {
        (void)key;
        if (!group || !std::any_of(members.begin(), members.end(), [&](auto guid) { return Contains(hold.receipt, guid); })) continue;
        if (group == hold.receipt.group || std::find(hold.aliases.begin(), hold.aliases.end(), group) != hold.aliases.end()) continue;
        hold.poisoned = true;
        if (hold.aliases.size() >= MemberCapacity) { _aliasOverflow = true; continue; }
        hold.aliases.push_back(group);
    }
}
void LfgReceiptBook::Changed(std::uint64_t group) { Poison(group); }
void LfgReceiptBook::Invited(std::uint64_t group)
{
    auto const found = _records.find(group);
    if ((found != _records.end() && found->second.joined) || Fenced(group, {})) Poison(group);
}
void LfgReceiptBook::Lifecycle(std::uint32_t guid)
{
    for (auto it = _records.begin(); it != _records.end();)
        if (Contains(it->second.receipt, guid)) it = _records.erase(it); else ++it;
    for (auto& [operation, hold] : _holds)
    {
        (void)operation;
        if (Contains(hold.receipt, guid)) hold.poisoned = true;
    }
}
bool LfgReceiptBook::Join(std::uint64_t group, std::uintptr_t object, LfgObservedLife leader)
{
    if (Fenced(group, {leader.guid})) return false;
    auto found = _records.find(group);
    if (found == _records.end()) return true;
    auto& record = found->second;
    // A second attempt can abort/requeue a prior native rolecheck. Require a
    // fresh producer lineage rather than attributing its packets to the old one.
    if (record.joined || record.receipt.object != object || record.receipt.leader != leader.guid
        || std::find(record.receipt.members.begin(), record.receipt.members.end(), leader) == record.receipt.members.end()
        || _serial == std::numeric_limits<std::uint64_t>::max()) { Poison(group); return true; }
    record.joined = true; record.receipt.join = ++_serial;
    return true;
}
void LfgReceiptBook::Packet(LfgObservedLife recipient, LfgRoleReceipt const& packet, std::uint64_t ownedAbort)
{
    std::vector<std::uint64_t> candidates;
    for (auto const& [group, record] : _records)
        if (record.joined && (Contains(record.receipt, recipient.guid)
            || std::any_of(packet.members.begin(), packet.members.end(), [&](auto guid) { return Contains(record.receipt, guid); })))
            candidates.push_back(group);
    if (candidates.size() != 1)
    {
        for (auto group : candidates) Poison(group);
        // A late/unmatched packet affecting a held body invalidates its proof.
        if (!ownedAbort) Lifecycle(recipient.guid);
        return;
    }
    auto const group = candidates.front();
    auto& record = _records.at(group);
    auto members = packet.members;
    std::sort(members.begin(), members.end());
    bool const exact = packet.leader == record.receipt.leader && members == LfgReceiptGuids(record.receipt)
        && std::find(record.receipt.members.begin(), record.receipt.members.end(), recipient) != record.receipt.members.end();
    if (ownedAbort)
    {
        if (ownedAbort == record.receipt.join && !record.finished && packet.state == LfgRoleReceipt::State::Aborted && exact) return;
        Poison(group); return;
    }
    if (!exact || packet.state == LfgRoleReceipt::State::Other || packet.state == LfgRoleReceipt::State::Aborted) { Poison(group); return; }
    bool const finished = packet.state == LfgRoleReceipt::State::Finished;
    if (record.finished && !finished) { Poison(group); return; }
    if (finished != record.finished) { record.finished = finished; record.recipients.clear(); }
    if (std::find(record.recipients.begin(), record.recipients.end(), recipient.guid) == record.recipients.end())
        record.recipients.push_back(recipient.guid);
}
bool LfgReceiptBook::Proposal(std::vector<std::uint64_t> const& keys, std::vector<std::uint32_t> const& playerKeys)
{
    if (_aliasOverflow && !_holds.empty()) return false;
    if (Fenced(0, playerKeys)) return false;
    for (auto key : keys) if (Fenced(key, {})) return false;
    for (auto key : keys)
    {
        if (_records.count(key)) Poison(key);
    }
    for (auto guid : playerKeys) Lifecycle(guid);
    return true;
}
std::optional<LfgApplicantReceipt> LfgReceiptBook::Proof(std::uint64_t group, bool queued) const
{
    auto const found = _records.find(group);
    if (_exhausted || found == _records.end()) return std::nullopt;
    auto const& record = found->second;
    if (!record.joined || record.poisoned || record.finished != queued || record.recipients.size() != record.receipt.members.size())
        return std::nullopt;
    auto result = record.receipt; result.queued = queued; return result;
}
bool LfgReceiptBook::Fence(std::string const& operation, LfgApplicantReceipt const& receipt)
{
    auto proof = Proof(receipt.group, receipt.queued);
    if (operation.empty() || !proof || *proof != receipt) return false;
    auto const existing = _holds.find({operation, receipt.group});
    if (existing != _holds.end()) return !existing->second.poisoned && existing->second.receipt == receipt;
    if (_holds.size() >= Capacity || Fenced(receipt.group, LfgReceiptGuids(receipt))) return false;
    _holds.emplace(std::make_pair(operation, receipt.group), Hold{receipt, false, {}}); return true;
}
bool LfgReceiptBook::Fenced(std::uint64_t group, std::vector<std::uint32_t> const& members) const
{
    for (auto const& [operation, hold] : _holds)
    {
        (void)operation;
        if (group && (hold.receipt.group == group || std::find(hold.aliases.begin(), hold.aliases.end(), group) != hold.aliases.end())) return true;
        for (auto guid : members) if (Contains(hold.receipt, guid)) return true;
    }
    return false;
}
bool LfgReceiptBook::Verify(std::string const& operation, LfgApplicantReceipt const& receipt) const
{
    auto const found = _holds.find({operation, receipt.group});
    return found != _holds.end() && !found->second.poisoned && found->second.receipt == receipt && receipt.epoch == _epoch;
}
std::optional<LfgApplicantReceipt> LfgReceiptBook::Held(std::string const& operation, std::uint64_t group, std::uint64_t join) const
{
    auto const found = _holds.find({operation, group});
    if (found == _holds.end() || found->second.receipt.join != join || !Verify(operation, found->second.receipt)) return std::nullopt;
    return found->second.receipt;
}
void LfgReceiptBook::Retire(std::string const& operation)
{
    for (auto it = _holds.begin(); it != _holds.end();)
        if (it->first.first == operation) { _records.erase(it->second.receipt.group); it = _holds.erase(it); }
        else ++it;
    if (_holds.empty()) _aliasOverflow = false;
}
std::vector<std::pair<std::string, std::vector<std::uint32_t>>> LfgReceiptBook::Owners() const
{
    std::vector<std::pair<std::string, std::vector<std::uint32_t>>> result;
    for (auto const& [key, hold] : _holds) result.emplace_back(key.first, LfgReceiptGuids(hold.receipt));
    return result;
}
}
