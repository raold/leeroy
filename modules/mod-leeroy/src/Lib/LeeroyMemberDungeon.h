// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#pragma once

#include "LeeroyGuildAddPolicy.h"
#include <chrono>

namespace Leeroy::Guild
{
inline constexpr std::uint32_t MemberDungeonMap = 602;
struct DungeonCandidate
{
    std::uint32_t guid;
    GuildAddRole role;
    std::string refusal;
};
std::string MemberDungeonAdmission(std::uint32_t map, std::uint32_t leader,
    std::vector<DungeonCandidate> const& candidates, bool ownerOnline, bool slotBusy);
bool ValidDungeonReceipt(nlohmann::json const& data);
bool DungeonStoredEvidence(std::string const& id, std::uint64_t revision, std::uint64_t slot,
    std::uint64_t settled, nlohmann::json const& data);
nlohmann::json DungeonTerminalUpdate(nlohmann::json const& verified, nlohmann::json const& firstTerminal,
    bool returned, std::int64_t now);
std::optional<std::uint64_t> DungeonFailureAge(nlohmann::json const& verified, std::int64_t now);
bool DungeonWriteAllowed(std::string const& id, std::uint64_t revision, nlohmann::json const& verified,
    nlohmann::json const& next, std::int64_t now, std::uint32_t handbackGuid = 0);
enum class DungeonLocation { Foreign, Origin, Copy };
DungeonLocation DungeonLocationOf(nlohmann::json const& body, std::uint32_t savedInstance,
    std::uint32_t map, std::uint32_t instance, float x, float y, float z);

// The world undo and its durable receipt happen once. Character saves may become visible later.
class DungeonReturnProof
{
public:
    template<class Checkpoint, class WorldReturned, class StoreReturned>
    bool Observe(std::chrono::steady_clock::time_point now, Checkpoint save, WorldReturned world, StoreReturned store)
    {
        if (!_started) _started = save("return_roster");
        if (!_started) return false;
        bool const justFinished = !_recorded && world() && save("returned");
        if (justFinished) _recorded = true;
        if (justFinished || ((!_recorded || !_proved) && now >= _nextCheck))
        {
            _nextCheck = now + std::chrono::seconds(1);
            _proved = store();
        }
        return _recorded && _proved;
    }
private:
    bool _started = false, _recorded = false, _proved = false;
    std::chrono::steady_clock::time_point _nextCheck = std::chrono::steady_clock::time_point::min();
};
enum class DungeonWriteRead { Unavailable, Previous, Committed, Conflict };
struct DungeonWriteProof
{
    std::uint64_t revision;
    nlohmann::json before, after;
    DungeonWriteRead Observe(std::uint64_t seenRevision, nlohmann::json const* seen) const;
};
bool DungeonFirstGroupActor(nlohmann::json const& data, std::uint32_t guid);
bool DungeonBeforeMutation(nlohmann::json const& data);
bool DungeonHandbackRecorded(nlohmann::json const& data, std::string const& start, std::uint32_t guid);
void ProjectDungeonAddBody(GuildAddBody& body, bool exactOwner);
bool DungeonEntrySource(bool exactOrigin, bool fighting, bool owned);
bool DungeonHandback(bool changed, bool proved, bool abandoned);
bool DungeonCanReleaseSlot(bool failed, std::uint64_t sinceReport, bool held);
bool DungeonMatchesGroup(nlohmann::json const& data, std::uint32_t group, std::uint32_t leader,
    std::vector<std::uint32_t> const& members);
std::vector<std::uint32_t> DungeonKnownRoster(nlohmann::json const& data);
void AuthorizeDungeonReturn(nlohmann::json& data);
bool DungeonReturnAuthorized(nlohmann::json const& data);
bool MemberDungeonPreempts(bool admitted, std::vector<std::uint32_t> const& requested,
    std::vector<std::uint32_t> const& participants);
}
