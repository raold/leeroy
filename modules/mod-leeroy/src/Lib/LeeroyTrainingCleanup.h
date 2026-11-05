// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_TRAINING_CLEANUP_H
#define MOD_LEEROY_TRAINING_CLEANUP_H
#include "LeeroyTrainingRuns.h"
#include <algorithm>
#include <deque>
#include <string>
#include <string_view>
#include <utility>

namespace Leeroy::Training
{
// A run handle covers journaled mutations and the held-boss resume, not arbitrary
// group destruction, casts, item consumption, or other unjournaled legacy actions.
inline bool ScopedBotMutationAllowed(std::string_view action)
{
    return action == "co" || action == "nc" || action == "value" || action == "restore" || action == "formation";
}
inline bool ScopedRaidControlAllowed(std::string_view action, std::string_view param)
{
    return action == "dc_pause" && param == "resume";
}

struct CleanupReceipt
{
    std::string handle, jobId;
    std::uint64_t token = 0;
    std::uint32_t leader = 0;
    RunScope scope;
    std::vector<std::uint32_t> members;
    enum class Phase { Open, Restoring, Complete } phase = Phase::Open;
    bool revoked = false, restored = false;
};

// World-thread registry. Completed evidence may be evicted, never active recovery.
// Only Open admits commands; an unknown/evicted/previous-process handle never does.
class CleanupReceipts
{
public:
    explicit CleanupReceipts(std::string incarnation, std::size_t capacity = 256)
        : _incarnation(std::move(incarnation)), _capacity(capacity) { }
    std::string Register(std::string job, std::uint64_t token, std::uint32_t leader,
        RunScope scope, std::vector<std::uint32_t> members)
    {
        if (_incarnation.empty() || !token || token <= _lastToken || !leader || !scope.instance
            || !scope.group || !scope.guild || !scope.human || job.empty() || members.empty() || members.size() > 40)
            return {};
        std::sort(members.begin(), members.end());
        if (!members.front() || std::adjacent_find(members.begin(), members.end()) != members.end()
            || !std::binary_search(members.begin(), members.end(), leader)) return {};
        while (_rows.size() >= _capacity)
        {
            auto old = std::find_if(_rows.begin(), _rows.end(), [](auto const& row)
                { return row.phase == CleanupReceipt::Phase::Complete; });
            if (old == _rows.end()) return {};
            _rows.erase(old);
        }
        _lastToken = token;
        auto handle = "tr_" + _incarnation + "_" + std::to_string(token);
        _rows.push_back({handle, std::move(job), token, leader, scope, std::move(members)});
        return handle;
    }
    CleanupReceipt const* Find(std::string const& handle) const
    {
        auto found = std::find_if(_rows.begin(), _rows.end(), [&](auto const& row) { return row.handle == handle; });
        return found == _rows.end() ? nullptr : &*found;
    }
    bool Admits(std::string const& handle, std::vector<std::uint32_t> const& guids) const
    {
        auto* row = Find(handle);
        return row && !row->revoked && row->phase == CleanupReceipt::Phase::Open && !guids.empty()
            && std::all_of(guids.begin(), guids.end(), [&](auto guid)
                { return std::binary_search(row->members.begin(), row->members.end(), guid); });
    }
    void Seal(std::string const& handle)
    {
        if (auto* row = Mutable(handle); row && row->phase == CleanupReceipt::Phase::Open)
            row->phase = CleanupReceipt::Phase::Restoring;
    }
    void Revoke(std::string const& handle)
    {
        if (auto* row = Mutable(handle); row && row->phase != CleanupReceipt::Phase::Complete)
        { row->revoked = true; row->phase = CleanupReceipt::Phase::Restoring; }
    }
    bool Restored(std::string const& handle, bool queuesEmpty)
    {
        auto* row = Mutable(handle);
        if (!row || row->revoked || row->phase != CleanupReceipt::Phase::Restoring || !queuesEmpty) return false;
        return row->restored = true;
    }
    bool Complete(std::string const& handle, bool queuesEmpty, bool teardownComplete)
    {
        auto* row = Mutable(handle);
        if (!row || row->revoked || !row->restored || !queuesEmpty || !teardownComplete) return false;
        row->phase = CleanupReceipt::Phase::Complete;
        return true;
    }
private:
    CleanupReceipt* Mutable(std::string const& handle)
    { return const_cast<CleanupReceipt*>(std::as_const(*this).Find(handle)); }
    std::string _incarnation;
    std::size_t _capacity;
    std::uint64_t _lastToken = 0;
    std::deque<CleanupReceipt> _rows;
};
}
#endif
