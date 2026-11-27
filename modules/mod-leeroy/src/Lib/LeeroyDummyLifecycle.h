// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_DUMMY_LIFECYCLE_H
#define MOD_LEEROY_DUMMY_LIFECYCLE_H
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <string>
#include <string_view>
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
// The facts an unproved ownership lacks, in a fixed order, for the blocked-member log line.
inline std::string DummyOwnerMissing(DummyOwnerState const& state)
{
    std::string missing;
    auto add = [&missing](bool failed, char const* name)
    {
        if (!failed) return;
        if (!missing.empty()) missing += ',';
        missing += name;
    };
    add(!state.sameSession, "session");
    add(state.isHuman, "human");
    add(!state.isCast, "cast");
    add(state.isSelfBot, "selfbot");
    add(!state.sameGuild, "guild");
    add(state.invited, "invite");
    add(state.reserved, "reserved");
    add(!state.groupOwned, "group");
    add(!state.masterOwned, "master");
    add(state.loggingOut, "logout");
    return missing;
}
// The core's LFGPlayerScript::OnPlayerMapChanged runs each time a player is added to a map (every far
// teleport) and, outside an LFG dungeon, disbands the player's group if one member or fewer is left in it.
// A trial's solo party therefore never survives a move to another map: it is formed at the dummy.
inline bool DummySoloPartySurvivesMapEntry(std::uint32_t members, bool lfgDungeonMap)
{
    return lfgDungeonMap || members > 1;
}
// Group::Create sends the new leader an empty group list, and playerbots' stock "reset botAI"
// answers it by dropping any master outside the bot's group (the driver always is) and resetting
// its strategies. Put the driver back only while our solo party still holds the body and every
// other ownership fact still proves it; a foreign master stays an ownership change.
inline bool DummyMasterReassertable(DummyOwnerState state, bool masterReleased, bool groupReleased, bool masterEmpty)
{
    if (masterReleased || groupReleased || !masterEmpty) return false;
    state.masterOwned = true;
    return DummyOwnerProven(state);
}
// The only accepted group is our unchanged solo party, or an empty partial creation that we still own.
inline bool DummyGroupProven(bool released, bool botGrouped, bool exactIdentity, std::uint32_t members,
    bool soleMemberAndLeader, std::uint32_t invites, bool ordinaryParty, bool nonPermanentBound = false)
{
    if (released) return !botGrouped;
    return exactIdentity && ordinaryParty && !invites && !nonPermanentBound
        && ((members == 1 && botGrouped && soleMemberAndLeader) || (members == 0 && !botGrouped));
}
// Moving, per member. The trial moves the untouched body first (no party, no master), so nothing of ours is
// on it when the core may disband a one-member group at a map change (DummySoloPartySurvivesMapEntry). At the
// dummy it forms the solo party and takes the driver as master. That party's group list makes playerbots'
// stock "reset botAI" drop the driver on the bot's next AI pass (1-5 s after a teleport); the controller puts
// it back (master_reasserted). The member counts as arrived once that has happened, or after kDummySettleMs
// with no reset, so the reset does not land mid-fight and cost the member its trial (ai_reset).
inline constexpr std::uint64_t kDummySettleMs = 8000;
struct DummyMoveState
{
    bool teleporting = false, owned = false, alive = true, inCombat = false, atTrial = false, setUp = false,
        reasserted = false;
    std::uint64_t sinceSetupMs = 0;
};
enum class DummyMoveStep { Wait, Block, Died, Attacked, Setup, Settle, Arrived };
inline DummyMoveStep DecideDummyMove(DummyMoveState const& state)
{
    if (state.teleporting) return DummyMoveStep::Wait;
    if (!state.owned) return DummyMoveStep::Block;
    if (!state.alive) return DummyMoveStep::Died;
    if (state.inCombat) return DummyMoveStep::Attacked;
    if (!state.atTrial) return DummyMoveStep::Wait;
    if (!state.setUp) return DummyMoveStep::Setup;
    return state.reasserted || state.sinceSetupMs >= kDummySettleMs ? DummyMoveStep::Arrived : DummyMoveStep::Settle;
}
// The return. onTrialMap: in the world on the trial's map, outside any instance, wherever on it. A member a fight
// let walk, blink, flee or fall anywhere there is still ours to send home; on 2026-11-25 1618 ended 54 yd from the
// dummy and 24 yd below it, and the old 150 yd / 15 yd box blocked it, so it was never returned. Another map, an
// instance or a body no longer ours still blocks.
struct DummyReturnState
{
    bool prepared = false, connected = false, sameSession = false, online = false, teleporting = false,
        atOrigin = false, released = false, resetDone = false, owned = false, onTrialMap = false,
        groupReleased = true, leaderRestored = true, finalResetDone = true, loggingOut = false;
};
// In the fight, where a member is. A near teleport on the trial's map, outside any instance, on a body still ours (a
// mage's stock "blink back", a charge) is waited out: on 2026-11-27 the fight blocked 2349's Blink 35 ms in. Once it
// lands, the fight's 30 yd / 5 s rule still drops it if it ended too far. A far teleport, another map, an instance or
// a body no longer ours blocks it.
struct DummyFightPlaceState
{
    bool owned = false, nearTeleport = false, farTeleport = false, onTrialMap = false, instance = false;
};
enum class DummyFightPlace { Fight, Wait, Block };
inline DummyFightPlace DecideDummyFightPlace(DummyFightPlaceState const& state)
{
    if (!state.owned || state.farTeleport || !state.onTrialMap || state.instance) return DummyFightPlace::Block;
    return state.nearTeleport ? DummyFightPlace::Wait : DummyFightPlace::Fight;
}
// Where members stand around the dummy. Melee share a ring 4 yd apart; ranged stand outside melee reach, where a
// caster's "enemy too close for spell" (blink back, flee) never fires, and inside the fight's 30 yd drop rule.
inline constexpr double kDummyRangedRingYd = 13.0;
inline double DummyRingRadius(std::size_t count, bool ranged)
{
    if (ranged) return kDummyRangedRingYd;
    constexpr double pi = 3.14159265358979323846;
    return count > 1 ? 2.0 / std::sin(pi / static_cast<double>(count)) : 0.0;
}
// A member's word in the terminal: its cleanup error keeps the `cleanup_failed` prefix the mind reads as an unproved
// cleanup, and the first drop reason rides along (`;dropped=`) instead of being overwritten by it.
inline std::string DummyDropReport(std::string_view dropped, std::string_view cleanupError)
{
    if (cleanupError.empty()) return std::string(dropped);
    std::string report = "cleanup_failed:" + std::string(cleanupError);
    if (!dropped.empty()) report += ";dropped=" + std::string(dropped);
    return report;
}
inline DummyReturnStep DecideDummyReturn(DummyReturnState const& state)
{
    if (!state.prepared) return DummyReturnStep::Complete;
    if (!state.connected) return DummyReturnStep::Wait;
    if (!state.sameSession) return DummyReturnStep::Block;
    if (!state.online || state.teleporting || state.loggingOut) return DummyReturnStep::Wait;
    if (state.released && state.resetDone && state.groupReleased && state.leaderRestored && state.finalResetDone
        && state.atOrigin)
        return DummyReturnStep::Complete;
    if (!state.owned || (!state.atOrigin && !state.onTrialMap)) return DummyReturnStep::Block;
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
    std::string_view offReason = "training_off"; // Why enabled is false, e.g. cleanup_pending while training is on.
};

// A member still counts as held after a failed return while any of the job's control is on it:
// the group object it created, its solo party listing the member, or the driver as master of the
// same body. The release bound is kDummyReleaseMs after the cleanup_failed terminal.
inline constexpr std::uint64_t kDummyReleaseMs = 10ULL * 60 * 1000;
struct DummyHoldState
{
    bool prepared = false, returned = false, creatingGroup = false, inOwnedGroup = false, sameBody = false,
        driverMaster = false;
};
inline bool DummyMemberHeld(DummyHoldState const& state)
{
    if (!state.prepared || state.returned) return false;
    return state.creatingGroup || state.inOwnedGroup || (state.sameBody && state.driverMaster);
}
// Past the bound, each unreturned member is kept or abandoned. Kept: still held, or still being returned
// (not blocked: its teleport or reset is in progress, so the job still acts on it). Abandoned: blocked
// with none of our control left; the job never touches it again and stops fencing it. A training journal
// does not keep it: the journal fences its own body and the journal adapter restores it once the trial
// no longer fences it, whereas a kept blocked member would never be restored and the trial never end.
enum class DummyMemberFate { Done, Keep, Abandon };
inline DummyMemberFate DummyFateAfterBound(DummyHoldState const& state, bool blocked)
{
    if (!state.prepared || state.returned) return DummyMemberFate::Done;
    if (DummyMemberHeld(state) || !blocked) return DummyMemberFate::Keep;
    return DummyMemberFate::Abandon;
}
// The trial's own fence past the bound (DummyTrialOwns) covers exactly its kept members.
inline bool DummyTrialFences(DummyMemberFate fate) { return fate == DummyMemberFate::Keep; }
// A blocked member's return never resumes, yet past the bound one our solo party or the driver still holds is kept,
// so nothing ever let go of it: on 2026-11-23 1507 left the trial spot with every ownership fact intact
// (location_changed) and stayed fenced in our party under the driver for 3 h, until a battleground queue took it out.
// Past the bound, while the body is still provably ours (DummyLetGoOwned), the job lets go of its own control where
// the body stands: reset, master, party, leader flag, stock reset. The master goes before the party, unlike on a
// return: when the party's stock reset already dropped the driver, only our party proves the body (the empty master
// is reassertable), and a released party with an unreleased master proves nothing, so the leader flag and the final
// reset would be skipped. Never the teleport home its block refused, so the member stays unreturned (native's receipt
// says released, not restored). Then nothing of ours holds it and it is abandoned. Anything not provably ours
// (another session, master or group, or a teleport or logout in progress) is left alone (Wait).
inline DummyReturnStep DecideDummyLetGo(DummyReturnState const& state)
{
    if (!state.prepared) return DummyReturnStep::Complete;
    if (!state.connected || !state.sameSession || !state.online || state.teleporting || state.loggingOut
        || !state.owned)
        return DummyReturnStep::Wait;
    if (!state.resetDone) return DummyReturnStep::ResetStrategies;
    if (!state.released) return DummyReturnStep::ReleaseMaster;
    if (!state.groupReleased) return DummyReturnStep::ReleaseGroup;
    if (!state.leaderRestored) return DummyReturnStep::RestoreLeaderFlag;
    if (!state.finalResetDone) return DummyReturnStep::FinalStockReset;
    return DummyReturnStep::Complete; // Nothing of ours is left on it.
}
// Ownership for letting go: every fact proven, or every fact but the master that our own party's stock reset dropped.
inline bool DummyLetGoOwned(DummyOwnerState const& state, bool masterReleased, bool groupReleased, bool masterEmpty)
{
    return DummyOwnerProven(state) || DummyMasterReassertable(state, masterReleased, groupReleased, masterEmpty);
}
inline bool DummyBoundPassed(bool failureReported, std::uint64_t sinceFailureMs)
{
    return failureReported && sinceFailureMs >= kDummyReleaseMs;
}
enum class DummyRelease { Wait, Keep, Release };
inline DummyRelease DecideDummyRelease(bool failureReported, std::uint64_t sinceFailureMs, std::uint32_t kept)
{
    if (!DummyBoundPassed(failureReported, sinceFailureMs)) return DummyRelease::Wait;
    return kept ? DummyRelease::Keep : DummyRelease::Release;
}

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
            else if (!state.enabled)
                Stop(state.offReason.empty() ? "training_off" : std::string(state.offReason), now);
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
