// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_LFG_RECEIPTS_H
#define MOD_LEEROY_REALM_LFG_RECEIPTS_H

#include <cstddef>
#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <vector>

namespace Leeroy::Realm
{
struct LfgObservedLife
{
    std::uint32_t guid = 0;
    std::uintptr_t player = 0, session = 0;
    bool operator==(LfgObservedLife const&) const = default;
};
struct LfgApplicantReceipt
{
    std::uint64_t group = 0, birth = 0, join = 0, epoch = 0;
    std::uintptr_t object = 0;
    std::uint32_t leader = 0;
    bool queued = false;
    std::vector<LfgObservedLife> members;
    bool operator==(LfgApplicantReceipt const&) const = default;
};
// The native packet adapter supplies the complete roleCheck.roles GUID list,
// never a Group roster substituted for that producer list.
struct LfgRoleReceipt
{
    enum class State { Initial, Finished, Aborted, Other };
    State state = State::Other;
    std::uint32_t leader = 0;
    std::vector<std::uint32_t> members;
};

class LfgReceiptBook
{
public:
    static constexpr std::size_t Capacity = 64, MemberCapacity = 5;
    void SetEpoch(std::uint64_t epoch);
    // Birth is accepted only at native Group::Create, after AddMember(leader).
    // The private LFG set starts with that producer addition. Later additions
    // extend the proof only before the first join; changes retire the lineage.
    void Born(std::uint64_t group, std::uintptr_t object, LfgObservedLife leader);
    void Added(std::uint64_t group, std::uintptr_t object, LfgObservedLife member);
    void ObserveGroup(std::uint64_t group, std::vector<std::uint32_t> const& members);
    void Changed(std::uint64_t group);
    void Invited(std::uint64_t group);
    void Lifecycle(std::uint32_t guid);
    bool Join(std::uint64_t group, std::uintptr_t object, LfgObservedLife leader);
    void Packet(LfgObservedLife recipient, LfgRoleReceipt const& packet, std::uint64_t ownedAbort = 0);
    // Proposal keys are causal invalidators only. They never expose a roster.
    bool Proposal(std::vector<std::uint64_t> const& keys, std::vector<std::uint32_t> const& playerKeys = {});
    std::optional<LfgApplicantReceipt> Proof(std::uint64_t group, bool queued) const;
    bool Fence(std::string const& operation, LfgApplicantReceipt const& receipt);
    bool Fenced(std::uint64_t group, std::vector<std::uint32_t> const& members) const;
    bool Verify(std::string const& operation, LfgApplicantReceipt const& receipt) const;
    std::optional<LfgApplicantReceipt> Held(std::string const& operation, std::uint64_t group, std::uint64_t join) const;
    void Retire(std::string const& operation);
    std::vector<std::pair<std::string, std::vector<std::uint32_t>>> Owners() const;
    void Overflow();
private:
    struct Record
    {
        LfgApplicantReceipt receipt;
        bool joined = false, poisoned = false, finished = false;
        std::vector<std::uint32_t> recipients;
    };
    struct Hold { LfgApplicantReceipt receipt; bool poisoned = false; std::vector<std::uint64_t> aliases; };
    std::map<std::uint64_t, Record> _records;
    std::map<std::pair<std::string, std::uint64_t>, Hold> _holds;
    std::uint64_t _epoch = 0, _serial = 0;
    bool _exhausted = false, _aliasOverflow = false;
    void Poison(std::uint64_t group);
};
std::vector<std::uint32_t> LfgReceiptGuids(LfgApplicantReceipt const& receipt);
}
#endif
