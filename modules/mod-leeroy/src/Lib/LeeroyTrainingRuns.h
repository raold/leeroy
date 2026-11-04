// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_TRAINING_RUNS_H
#define MOD_LEEROY_TRAINING_RUNS_H
#include <cstdint>
#include <limits>
#include <map>
#include <vector>

namespace Leeroy::Training
{
struct RunScope
{
    std::uint32_t map = 0, instance = 0, guild = 0, human = 0;
    std::uint64_t group = 0;
    bool operator==(RunScope const&) const = default;
};
struct JobRunReceipt { std::uint64_t token = 0; RunScope scope; };

// Read-only game ownership. Independent of the training switch and of mutable override journals.
// Runtime removes a matching receipt only after teardown/restoration succeeds, or its scope ceases to exist.
class TrainingRuns
{
public:
    std::uint64_t NextToken()
    {
        return _next == std::numeric_limits<std::uint64_t>::max() ? 0 : _next++;
    }
    std::uint64_t Register(std::uint32_t leader, RunScope scope)
    {
        if (!leader || !scope.group || !scope.guild || !scope.human || _jobs.contains(leader) || _jobs.size() >= 128)
            return 0;
        auto token = NextToken(); // Every job gets a fresh token, including one adopting an existing control journal.
        if (!token) return 0;
        _jobs.emplace(leader, JobRunReceipt{token, scope});
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
private:
    std::uint64_t _next = 1;
    std::map<std::uint32_t, JobRunReceipt> _jobs;
};
}
#endif
