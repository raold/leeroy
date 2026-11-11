// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_QUEUE_H
#define MOD_LEEROY_REALM_QUEUE_H

#include "LeeroyRealmPolicy.h"
#include <deque>
#include <functional>
#include <string>
#include <string_view>
#include <vector>
#include <nlohmann/json.hpp>

namespace Leeroy::Realm
{
enum class ActivityDomain { Realm, Guild };
class ActionQueue
{
public:
    struct Entry
    {
        std::string id, kind;
        std::vector<std::uint32_t> guids;
        std::size_t cursor = 0;
        nlohmann::json payload;
        ActivityDomain domain = ActivityDomain::Realm; // Trusted native admission only.
    };
    static constexpr std::size_t Capacity = 500;
    bool Push(Entry entry);
    // One budget unit per callback. A completed entry is removed; otherwise
    // an advanced cursor continues under the budget, and a waiting head yields
    // until next tick. Callbacks must never recursively mutate this queue.
    void Drain(TickBudget& budget, std::function<bool(Entry&)> const& step);
    void DropAll(std::string const& reason, std::function<void(Entry const&, std::string const&)> const& dropped);
    void DropIf(std::function<bool(Entry const&)> const& predicate, std::string const& reason,
        std::function<void(Entry const&, std::string const&)> const& dropped);
    std::size_t Size() const { return _entries.size(); }
private:
    std::deque<Entry> _entries;
};

// Admission of a body move's manifest, in its order. `fence` gives a member's reason, or "" when it
// may be reserved (the caller reserves it there). Every kind but a rearm is refused whole at its
// first fenced member (`error`, nothing more is asked). A rearm leaves out a member fenced for a
// reason of its own (MemberSkipReason) and is refused whole at a fence that is the job's own, or
// when every member is fenced (with the first reason).
struct AckPartition
{
    std::vector<std::uint32_t> admitted{}, fenced{};
    std::string error{};
};
AckPartition PartitionAtAck(std::string_view kind, std::vector<std::uint32_t> const& manifest,
    std::function<std::string(std::uint32_t)> const& fence);
}
#endif
