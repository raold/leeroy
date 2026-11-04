// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_ARENA_OBSERVATION_H
#define MOD_LEEROY_REALM_ARENA_OBSERVATION_H
#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <map>
#include <optional>
#include <utility>
#include <vector>

namespace Leeroy::Realm
{
// Opaque object/session numbers are equality keys only. No stored native pointer
// is dereferenced. A lifecycle callback retires even an address-reused identity.
struct ObservedArenaLife
{
    std::uint32_t guid = 0;
    std::uintptr_t player = 0, session = 0;
    bool operator==(ObservedArenaLife const&) const = default;
};
struct ObservedArenaQueue
{
    std::uint64_t insertion = 0, epoch = 0, group = 0;
    std::uintptr_t queue = 0, row = 0, groupObject = 0;
    std::uint32_t captain = 0, team = 0, type = 0, bgType = 0, bracket = 0, joined = 0;
    std::vector<ObservedArenaLife> members;
};
struct ObservedArenaInstance
{
    std::uint64_t generation = 0, epoch = 0;
    std::uintptr_t object = 0;
    std::uint32_t id = 0, map = 0, type = 0;
    bool ended = false, ownerSourced = false;
    bool operator==(ObservedArenaInstance const& other) const
    {
        return generation == other.generation && epoch == other.epoch && object == other.object
            && id == other.id && map == other.map && type == other.type;
    }
};
struct ObservedArenaEntry
{
    ObservedArenaLife life;
    std::uint64_t group = 0;
    std::uint32_t side = 0;
};
struct ObservedArenaDestination
{
    std::uint32_t map = 0;
    float x = 0, y = 0, z = 0, orientation = 0;
    bool operator==(ObservedArenaDestination const&) const = default;
};
struct ArenaObservation
{
    ObservedArenaQueue source;
    std::optional<ObservedArenaInstance> match{};
    std::map<std::uint32_t, ObservedArenaEntry> entries{};
    std::map<std::uint32_t, ObservedArenaLife> transits{};
    ObservedArenaDestination destination{};
    std::uintptr_t teamObject = 0;
    std::vector<std::uint32_t> teamRoster{};
    std::uint32_t baseline = 0, side = 0, expiry = 0, teamCaptain = 0;
    bool verified = false, baselineAvailable = false, reported = false, poisoned = false, endCopied = false;
};
struct ObservedArenaEnd
{
    ObservedArenaInstance match;
    std::array<std::uint32_t, 2> teams{};
    std::vector<ObservedArenaEntry> members;
    std::uint32_t winner = 2;
    bool ownerSourced = false;
};

class ArenaObservationBook
{
public:
    static constexpr std::size_t Capacity = 500, FactCapacity = 128;
    void Fence(bool enabled, std::uint64_t epoch)
    {
        if (!enabled || epoch != _epoch) Clear();
        _epoch = epoch; _enabled = enabled;
    }
    void Clear() { records.clear(); instances.clear(); }
    std::size_t Size() const { return records.size() + instances.size(); }
    void Lifecycle(std::uint32_t guid)
    {
        std::erase_if(records, [=](auto const& pair) { return Contains(pair.second.source, guid); });
    }
    void GroupChanged(std::uint64_t group)
    {
        std::erase_if(records, [=](auto const& pair) { return !pair.second.endCopied && pair.second.source.group == group; });
    }
    void Leave(std::uint32_t guid)
    {
        std::erase_if(records, [=](auto const& pair)
            { return !pair.second.endCopied && Contains(pair.second.source, guid); });
    }
    // Every insertion, including an unobserved/non-rated one, supersedes the
    // old lineage for its actual members. Timestamp/roster repetition cannot
    // turn a second AddGroup into the first insertion.
    void Reinsert(std::vector<std::uint32_t> const& members)
    {
        std::erase_if(records, [&](auto const& pair)
        {
            return std::any_of(members.begin(), members.end(), [&](auto guid) { return Contains(pair.second.source, guid); });
        });
    }
    bool Add(ObservedArenaQueue source)
    {
        if (!_enabled || !source.insertion || source.epoch != _epoch || !source.queue || !source.row || !source.group
            || !source.groupObject || !source.team || !source.captain
            || (source.type != 2 && source.type != 3 && source.type != 5)
            || source.members.size() != source.type || !Contains(source, source.captain)) return false;
        std::vector<std::uint32_t> members;
        for (auto const& life : source.members)
        {
            if (!life.guid || !life.player || !life.session) return false;
            members.push_back(life.guid);
        }
        std::sort(members.begin(), members.end());
        if (std::adjacent_find(members.begin(), members.end()) != members.end()) return false;
        Reinsert(members);
        // Another roster for the same persistent team makes an old rating
        // baseline ambiguous even if no selected member overlaps.
        for (auto& [id, record] : records)
        {
            (void)id;
            if (record.source.team == source.team) record.baselineAvailable = false;
        }
        if (Size() >= Capacity || records.contains(source.insertion)) return false;
        auto insertion = source.insertion;
        records.emplace(insertion, ArenaObservation{std::move(source)});
        return true;
    }
    bool Create(ObservedArenaInstance instance)
    {
        if (!_enabled || !instance.id || !instance.object || !instance.generation || instance.epoch != _epoch) return false;
        if (instances.contains(instance.id)) Destroy(instances.at(instance.id));
        if (Size() >= Capacity) return false;
        instances.emplace(instance.id, instance); return true;
    }
    void Destroy(ObservedArenaInstance const& instance)
    {
        auto found = instances.find(instance.id);
        if (found == instances.end() || found->second.object != instance.object) return;
        auto generation = found->second.generation;
        // An already copied end remains usable after destruction; bound records
        // stay until End is reconciled. An unended destroyed match is retired.
        if (!found->second.ended)
            std::erase_if(records, [=](auto const& pair)
                { return pair.second.match && pair.second.match->generation == generation; });
        instances.erase(found);
    }
    bool Bind(std::uint64_t insertion, ObservedArenaInstance const& instance, std::uint32_t side, std::uint32_t expiry)
    {
        auto found = records.find(insertion);
        auto native = instances.find(instance.id);
        if (!_enabled || found == records.end() || !found->second.verified || found->second.poisoned || !expiry || side > 1
            || native == instances.end() || native->second != instance || instance.ended
            || instance.epoch != found->second.source.epoch || instance.type != found->second.source.type) return false;
        auto& record = found->second;
        if (record.match) return *record.match == instance && record.side == side && record.expiry == expiry;
        record.match = instance; record.side = side; record.expiry = expiry; return true;
    }
    void Enter(ObservedArenaInstance const& match, ObservedArenaEntry entry)
    {
        if (match.ended) return;
        for (auto& [id, record] : records)
        {
            (void)id;
            if (record.poisoned || !record.match || *record.match != match || record.side != entry.side || !entry.group) continue;
            auto life = std::find(record.source.members.begin(), record.source.members.end(), entry.life);
            if (life == record.source.members.end()) continue;
            if (!record.entries.empty() && record.entries.begin()->second.group != entry.group)
                { record.entries.clear(); record.poisoned = true; continue; }
            record.entries[entry.life.guid] = entry;
            record.transits.erase(entry.life.guid);
        }
    }
    void Travel(ObservedArenaLife life, std::uint32_t instance, std::uint32_t side, ObservedArenaDestination destination)
    {
        for (auto& [id, record] : records)
        {
            (void)id;
            if (record.poisoned || record.endCopied || !record.match
                || std::find(record.source.members.begin(), record.source.members.end(), life) == record.source.members.end()) continue;
            if (record.match->id != instance || record.side != side || record.destination != destination
                || record.entries.contains(life.guid) || record.transits.contains(life.guid)) record.poisoned = true;
            else record.transits[life.guid] = life;
        }
    }
    static bool AwaitingEntry(ArenaObservation const& record, ObservedArenaLife life)
    {
        auto found = record.transits.find(life.guid);
        return !record.poisoned && record.match && found != record.transits.end() && found->second == life;
    }
    static bool Matches(ArenaObservation const& record, ObservedArenaEnd const& end)
    {
        if (end.ownerSourced || end.match.ownerSourced || !record.verified || record.poisoned
            || !record.match || *record.match != end.match || record.side > 1
            || end.teams[record.side] != record.source.team || record.entries.size() != record.source.members.size()) return false;
        for (auto const& life : record.source.members)
        {
            auto copied = std::find_if(end.members.begin(), end.members.end(), [&](auto const& entry)
                { return entry.life == life && entry.side == record.side; });
            auto entry = record.entries.find(life.guid);
            if (copied == end.members.end() || entry == record.entries.end() || copied->group != entry->second.group) return false;
        }
        return true;
    }
    // Dedupe belongs to the exact create generation, and remains until destroy.
    bool End(ObservedArenaEnd const& end)
    {
        auto found = instances.find(end.match.id);
        if (!_enabled || found == instances.end() || found->second.ended || end.match.ended
            || found->second != end.match || end.match.epoch != _epoch) return false;
        found->second.ended = true;
        for (auto& [id, record] : records)
        {
            (void)id;
            if (record.match && *record.match == end.match) record.endCopied = true;
        }
        return true;
    }
    void MarkOwnerSource(ObservedArenaInstance const& match)
    {
        auto found = instances.find(match.id);
        if (found != instances.end() && found->second == match) found->second.ownerSourced = true;
    }
    static bool Contains(ObservedArenaQueue const& source, std::uint32_t guid)
    {
        return std::any_of(source.members.begin(), source.members.end(), [=](auto const& life) { return life.guid == guid; });
    }
    std::map<std::uint64_t, ArenaObservation> records;
    std::map<std::uint32_t, ObservedArenaInstance> instances;
private:
    std::uint64_t _epoch = 0;
    bool _enabled = false;
};
}
#endif
