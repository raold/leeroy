// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_RAID_RECOVERY_H
#define MOD_LEEROY_RAID_RECOVERY_H
#include <cstdint>

namespace Leeroy::Guild
{
// Group join/leave unbind non-permanent saves. No pre-existing temporary bind may enter a benchmark;
// after entry only this exact new trial bind is allowed, plus unrelated permanent saves that core preserves.
inline bool BenchmarkBindAllowed(std::uint32_t map, std::uint32_t instance, std::uint32_t difficulty,
    bool permanent, std::uint32_t targetMap, std::uint32_t ownedInstance = 0, std::uint32_t ownedDifficulty = 0)
{
    if (!instance) return false;
    if (targetMap && map == targetMap)
        return ownedInstance && instance == ownedInstance && difficulty == ownedDifficulty;
    return permanent;
}
// Addresses are identity tokens only: never dereferenced or kept alive. Logout revokes the receipt even if
// an allocator later reuses all three addresses. A driver may have no AI, but always needs a Player/session.
struct RaidBodyLease
{
    std::uintptr_t player = 0, ai = 0, session = 0;
    bool revoked = false;
    bool Matches(std::uintptr_t currentPlayer, std::uintptr_t currentAi, std::uintptr_t currentSession) const
    {
        return !revoked && player && session && currentPlayer == player && currentAi == ai && currentSession == session;
    }
    void Revoke() { revoked = true; }
};

enum class RaidCleanupReport { None, Complete, Failed };
// Proof-bearing cleanup retains ownership after the reporting deadline. A sent
// return that never arrives must become retryable without resetting that deadline.
inline bool RaidReturnReady(bool retainOwnership, bool issued, std::uint32_t elapsed, std::uint32_t retryAt)
{
    return retainOwnership ? elapsed >= retryAt : !issued;
}
class RaidFinalReset
{
public:
    template<class Reset> void Run(Reset&& reset)
    {
        if (_complete) return;
        reset(); // An exception leaves this independent receipt incomplete for a later controlled retry.
        _complete = true;
    }
    template<class Reset> bool TryRun(Reset&& reset)
    {
        try { Run(reset); return true; }
        catch (...) { return false; } // Keep the fault local so other released group members still get processed.
    }
    bool Complete() const { return _complete; }
private:
    bool _complete = false;
};

class RaidCleanupTerminal
{
public:
    RaidCleanupReport Observe(bool complete, std::uint32_t elapsedMs)
    {
        if (_reported || (!complete && elapsedMs < 60000)) return RaidCleanupReport::None;
        _reported = true;
        return complete ? RaidCleanupReport::Complete : RaidCleanupReport::Failed;
    }
    bool Reported() const { return _reported; }
private:
    bool _reported = false;
};
}
#endif
