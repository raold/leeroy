// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_TRAINING_RUNS_H
#define MOD_LEEROY_TRAINING_RUNS_H
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <map>
#include <optional>
#include <utility>
#include <vector>

namespace Leeroy::Training
{
struct RunScope
{
    std::uint32_t map = 0, instance = 0, guild = 0, human = 0;
    std::uint64_t group = 0;
    bool operator==(RunScope const&) const = default;
};
inline constexpr std::size_t kMaxRunMembers = 40;

// Immutable admission membership. A default/legacy manifest is unknown, never
// evidence that an arbitrary body is free. Runtime admission requires Capture.
class RunManifest
{
public:
    static std::optional<RunManifest> Capture(std::uint32_t leader, std::vector<std::uint32_t> members)
    {
        if (!leader || members.empty() || members.size() > kMaxRunMembers) return std::nullopt;
        std::sort(members.begin(), members.end());
        if (!members.front() || !std::binary_search(members.begin(), members.end(), leader)
            || std::adjacent_find(members.begin(), members.end()) != members.end()) return std::nullopt;
        RunManifest result;
        result._members = std::move(members);
        result._known = true;
        return result;
    }
    bool Known() const { return _known; }
    std::vector<std::uint32_t> const& Members() const { return _members; }
    bool Owns(std::uint32_t guid) const
    {
        return guid && (!_known || std::binary_search(_members.begin(), _members.end(), guid));
    }
private:
    bool _known = false;
    std::vector<std::uint32_t> _members;
};

inline bool RunOwnsBody(RunManifest const& manifest, RunScope const& scope, std::uint32_t guid,
    std::uint64_t currentGroup = 0, bool currentMember = false)
{
    return guid && (manifest.Owns(guid) || (scope.group && currentGroup == scope.group && currentMember));
}

struct JobRunReceipt { std::uint64_t token = 0; RunScope scope; RunManifest manifest; };

// Read-only game ownership. Independent of the training switch and of mutable override journals.
// Runtime removes a matching receipt only after exact teardown/restoration succeeds.
class TrainingRuns
{
public:
    std::uint64_t NextToken()
    {
        return _next == std::numeric_limits<std::uint64_t>::max() ? 0 : _next++;
    }
    std::uint64_t Register(std::uint32_t leader, RunScope scope, RunManifest manifest = {})
    {
        if (!leader || !scope.group || !scope.guild || !scope.human || _jobs.contains(leader) || _jobs.size() >= 128
            || (manifest.Known() && !manifest.Owns(leader)))
            return 0;
        auto token = NextToken(); // Every job gets a fresh token, including one adopting an existing control journal.
        if (!token) return 0;
        _jobs.emplace(leader, JobRunReceipt{token, scope, std::move(manifest)});
        return token;
    }
    JobRunReceipt const* Find(std::uint32_t leader) const
    {
        auto found = _jobs.find(leader);
        return found == _jobs.end() ? nullptr : &found->second;
    }
    bool End(std::uint32_t leader, std::uint64_t token)
    {
        auto found = _jobs.find(leader);
        if (found == _jobs.end()) return true;
        if (!token || found->second.token != token) return false;
        _jobs.erase(found);
        return true;
    }
    std::vector<std::uint32_t> Leaders() const
    {
        std::vector<std::uint32_t> leaders;
        for (auto const& [leader, job] : _jobs) leaders.push_back(leader);
        return leaders;
    }
    bool Owns(std::uint32_t guid) const
    {
        if (!guid) return false;
        for (auto const& [leader, job] : _jobs)
            if (guid == leader || job.manifest.Owns(guid)) return true;
        return false;
    }
private:
    std::uint64_t _next = 1;
    std::map<std::uint32_t, JobRunReceipt> _jobs;
};
}
#endif
