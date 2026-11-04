// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_DUMMY_LIFECYCLE_H
#define MOD_LEEROY_DUMMY_LIFECYCLE_H
#include <cstdint>
#include <string>
#include <utility>

namespace Leeroy::Training
{
inline constexpr std::uint64_t kDummyMoveMs = 20000, kDummyReturnMs = 30000, kDummyRetryMs = 2000;
enum class DummyPhase { Preparing, Moving, Fighting, Returning, Recovery, Finished };
enum class DummyStep { Prepare, Move, StartFight, Fight, Return, ReportFailure, Finish, Recovered, Wait };
enum class DummyReturnStep { Complete, Wait, Block, ResetStrategies, ReleaseGroup, RestoreLeaderFlag,
    ReleaseMaster, FinalStockReset, Teleport };
struct DummyOwnerState
{
    bool sameSession = false, isHuman = false, isCast = false, isSelfBot = false, sameGuild = false,
        invited = false, reserved = false, groupOwned = false, masterOwned = false, loggingOut = false;
};
inline bool DummyOwnerProven(DummyOwnerState const& state)
{
    return state.sameSession && !state.isHuman && state.isCast && !state.isSelfBot && state.sameGuild
        && !state.invited && !state.reserved && state.groupOwned && state.masterOwned && !state.loggingOut;
}
// The only accepted group is our unchanged solo party, or an empty partial creation that we still own.
inline bool DummyGroupProven(bool released, bool botGrouped, bool exactIdentity, std::uint32_t members,
    bool soleMemberAndLeader, std::uint32_t invites, bool ordinaryParty, bool nonPermanentBound = false)
{
    if (released) return !botGrouped;
    return exactIdentity && ordinaryParty && !invites && !nonPermanentBound
        && ((members == 1 && botGrouped && soleMemberAndLeader) || (members == 0 && !botGrouped));
}
struct DummyReturnState
{
    bool prepared = false, connected = false, sameSession = false, online = false, teleporting = false,
        atOrigin = false, released = false, resetDone = false, owned = false, atTrial = false,
        groupReleased = true, leaderRestored = true, finalResetDone = true, loggingOut = false;
};
inline DummyReturnStep DecideDummyReturn(DummyReturnState const& state)
{
    if (!state.prepared) return DummyReturnStep::Complete;
    if (!state.connected) return DummyReturnStep::Wait;
    if (!state.sameSession) return DummyReturnStep::Block;
    if (!state.online || state.teleporting || state.loggingOut) return DummyReturnStep::Wait;
    if (state.released && state.resetDone && state.groupReleased && state.leaderRestored && state.finalResetDone
        && state.atOrigin)
        return DummyReturnStep::Complete;
    if (!state.owned || (!state.atOrigin && !state.atTrial)) return DummyReturnStep::Block;
    if (!state.resetDone) return DummyReturnStep::ResetStrategies;
    if (!state.groupReleased) return DummyReturnStep::ReleaseGroup;
    if (!state.leaderRestored) return DummyReturnStep::RestoreLeaderFlag;
    if (!state.released) return DummyReturnStep::ReleaseMaster;
    if (!state.finalResetDone) return DummyReturnStep::FinalStockReset;
    return state.atOrigin ? DummyReturnStep::Complete : DummyReturnStep::Teleport;
}
struct DummyObservation
{
    bool enabled = true, humanOnline = false, allArrived = false, anyFighting = false, allReturned = false;
};

// The production controller calls this same policy before each phase's effects. A repeated cancellation
// cannot renew a deadline. A failed return reports once but keeps the recovery phase/reservation alive.
class DummyLifecycle
{
public:
    DummyLifecycle(std::uint64_t accepted, std::uint32_t seconds)
        : _accepted(accepted), _duration(seconds * 1000ULL) { }
    DummyPhase Phase() const { return _phase; }
    std::string const& Reason() const { return _reason; }
    bool TerminalFailed() const { return !_reason.empty() || _reportedFailure; }
    bool FailureReported() const { return _reportedFailure; }
    void Moving() { if (_phase == DummyPhase::Preparing) _phase = DummyPhase::Moving; }
    bool Fighting(std::uint64_t now)
    {
        if (_phase != DummyPhase::Moving || Elapsed(_accepted, now) >= kDummyMoveMs) return false;
        _phase = DummyPhase::Fighting;
        _fightStarted = now;
        return true;
    }
    void Stop(std::string reason, std::uint64_t now)
    {
        if (_phase == DummyPhase::Returning || _phase == DummyPhase::Recovery || _phase == DummyPhase::Finished) return;
        _reason = std::move(reason);
        _returnStarted = now;
        _phase = DummyPhase::Returning;
    }
    DummyStep Step(std::uint64_t now, DummyObservation const& state)
    {
        if (_phase == DummyPhase::Finished) return DummyStep::Wait;
        if (_phase != DummyPhase::Returning && _phase != DummyPhase::Recovery)
        {
            if (state.humanOnline) Stop("human_login", now);
            else if (!state.enabled) Stop("training_off", now);
        }
        if (_phase == DummyPhase::Preparing || _phase == DummyPhase::Moving)
        {
            if (Elapsed(_accepted, now) >= kDummyMoveMs) Stop("move_timeout", now);
            else if (_phase == DummyPhase::Preparing) return DummyStep::Prepare;
            else return state.allArrived ? DummyStep::StartFight : DummyStep::Move;
        }
        if (_phase == DummyPhase::Fighting)
        {
            if (!state.anyFighting) Stop("no_participants", now);
            else if (Elapsed(_fightStarted, now) >= _duration) Stop({}, now);
            else return DummyStep::Fight;
        }
        if (_phase == DummyPhase::Returning || _phase == DummyPhase::Recovery)
        {
            if (state.allReturned)
            {
                bool reported = _reportedFailure;
                _phase = DummyPhase::Finished;
                return reported ? DummyStep::Recovered : DummyStep::Finish;
            }
            if (_phase == DummyPhase::Returning && Elapsed(_returnStarted, now) >= kDummyReturnMs)
            {
                _phase = DummyPhase::Recovery;
                _reportedFailure = true;
                _retryAt = now;
                return DummyStep::ReportFailure;
            }
            if (_phase == DummyPhase::Returning || now >= _retryAt)
            {
                _retryAt = now + kDummyRetryMs;
                return DummyStep::Return;
            }
        }
        return DummyStep::Wait;
    }
private:
    static std::uint64_t Elapsed(std::uint64_t start, std::uint64_t now) { return now >= start ? now - start : 0; }
    DummyPhase _phase = DummyPhase::Preparing;
    std::uint64_t _accepted = 0, _duration = 0, _fightStarted = 0, _returnStarted = 0, _retryAt = 0;
    bool _reportedFailure = false;
    std::string _reason;
};
}
#endif
