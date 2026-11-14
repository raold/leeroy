// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_OFFLINE_RAID_POLICY_H
#define MOD_LEEROY_OFFLINE_RAID_POLICY_H

#include "LeeroyRealmCleanup.h"

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace Leeroy::Guild
{
struct OfflineRaidLimits
{
    std::uint32_t maxWipes, maxWipesPerBoss, timeoutMs, minMembers, maxRestarts;
};

struct OfflineRaidState
{
    std::uint32_t elapsedMs;
    bool humanOnline, leaderPresent;
    std::uint32_t membersPresent, wipes, maxWipesOnOneBoss, bossesDone, bossesTotal;
    bool dcEnabled;
    std::uint32_t dcOffMs, restarts;
};

enum class Verdict { Continue, Restart, Done };
struct Decision { Verdict verdict; std::string reason; };

Decision Decide(OfflineRaidState const& state, OfflineRaidLimits const& limits);
bool IsWipe(std::uint8_t oldState, std::uint8_t newState);
std::uint32_t AdvanceElapsed(std::uint32_t elapsed, std::uint32_t diff);

// A new copy keeps each boss at TO_BE_DECIDED (5) until that boss's own script loads with its grid, so
// unloaded bosses are fresh. Only a recorded kill (DONE, 3) or a completed-encounter credit marks a used copy.
bool FreshEncounters(std::vector<std::uint8_t> const& bossStates, std::uint32_t completedEncounterMask);

// Dungeon-clear finds a raid's bosses by their static spawns. A map with none (Trial of the Crusader:
// its herald's event summons every boss) has an empty roster, and `dc on` refuses it with "No bosses
// found for this map" on every try, so setup names it at once instead of waiting out its timer.
inline constexpr char const* OfflineNoDcBosses = "dc_no_bosses";
std::string DcRosterRefusal(std::size_t bosses);

// Dungeon-clear's leader run as the offline job reads it on a Running tick. In an offline raid only a
// path dungeon-clear cannot pass pauses it (the job turns WaitAtBoss off and nobody types `dc pause`):
// Door is its auto-pause in front of a door it cannot open, Paused any other hold.
enum class DcPhase { Unknown, Off, On, Paused, Door };
DcPhase ReadDcPhase(bool leaderPresent, bool enabled, bool paused, bool pausedAtDoor);
char const* DcPhaseCode(DcPhase phase); // The heartbeat's dc_state: unknown, off, on, paused, door.

// A hold out of every boss fight ends the run after this long, naming it (dc_door_blocked or
// dc_paused). Nothing in an offline raid opens such a door: on 2026-11-13 Ulduar's Flame Leviathan
// gate, which only the siege-vehicle event opens, held a 25-man run until the mind's 30-minute
// watchdog stopped it. An engaged or unreadable encounter, or any other phase, resets the clock.
inline constexpr std::uint32_t OfflineDcHoldStopMs = 300000;
struct DcHold
{
    std::uint32_t heldMs = 0;
    std::string Observe(DcPhase phase, std::optional<bool> bossEngaged, std::uint32_t diffMs);
};

// Empty error admits. Repeated stops during our teardown are no-ops; a finished or foreign job is refused.
std::string OfflineStopError(bool connected, bool owned, bool finished, bool tearingDown,
    bool stopped, std::optional<bool> bossEngaged);
bool BossProgress(std::uint8_t before, std::uint8_t after);

// A running offline raid may recall an unchanged, still-owned follower. These are
// per-member, per-run bounds; arriving never replenishes the attempt budget.
enum class RecallAction { Wait, Summon, Arrived };
struct OfflineRecall
{
    std::uint32_t absentMs = 0, sinceAttemptMs = 0, attempts = 0;
    bool pending = false;
    RecallAction Observe(bool inside, bool safe, std::uint32_t diffMs);
};
inline constexpr std::uint32_t OfflineRecallGraceMs = 30000;
inline constexpr std::uint32_t OfflineRecallRetryMs = 60000;
inline constexpr std::uint32_t OfflineRecallAttempts = 3;

// Recall is only sent while the raid is quiet: a known, unengaged encounter, the
// job's driver, no Realm owner in the group, and a leader and group out of combat.
struct RecallQuiet
{
    bool encounterKnown = false, bossEngaged = false, driver = false, realmGroup = false;
    bool leaderAi = false, leaderMasterIsDriver = false, leaderAlive = false, leaderGhost = false;
    bool leaderCombat = false, leaderTransport = false, leaderVehicle = false, memberCombat = false;
};
bool RecallQuietOk(RecallQuiet const& quiet);

// One absent follower's body. The adapter fills it only for an owned body that is
// not already inside; inDungeon short-circuits before any destination lookup.
struct RecallBody
{
    bool inWorld = false, teleporting = false, alive = false, ghost = false, combat = false;
    bool flight = false, transport = false, vehicle = false, pendingBind = false, inDungeon = false;
    bool difficultyMatches = false, destinationMatches = false;
};
bool RecallBodySafe(RecallBody const& body);

struct RecallPvp
{
    bool duel = false, battleground = false, battlegroundQueue = false, arenaObserved = false;
    Realm::WintergraspMemberFacts wintergrasp{};
};
bool RecallPvpBusy(RecallPvp const& pvp);

// The PvP facts need the battlefield manager and the arena observer, so they are
// read last and only for a body every cheaper check already admits.
template<class ReadPvp>
bool RecallSafe(RecallQuiet const& quiet, bool owned, RecallBody const& body, ReadPvp&& readPvp)
{
    return RecallQuietOk(quiet) && owned && RecallBodySafe(body) && !RecallPvpBusy(readPvp());
}

// The driver is a separate self-mastered bot outside the raid group. Stock playerbots
// (ResetAiAction on an empty group list or a leader change) clears a master that is
// not in the bot's group, and FindNewMaster finds no master in an all-bot raid, so a
// member can be left masterless: then it thinks on the slow path, is no longer
// protected from an arena captain's gather, and teardown cannot prove it is ours.
// Restore the driver only for a body this job still holds whose master is empty; a
// non-empty foreign master is someone else's control and stays an ownership change.
struct MasterFacts
{
    bool driver = false, assigned = false, released = false, sameBody = false, inJobGroup = false;
    bool masterIsDriver = false, masterEmpty = false, foreignLease = false;
};
bool ReassertDriverMaster(MasterFacts const& facts);

// A restart needs every remaining roster member back at the entrance. After this
// grace an absent member is dropped from the restart gate when the members present
// still meet the minimum (Decide's too_few rule); otherwise the run stops too_few.
// The leader is never dropped: its absence fails the restart at its own timeout.
inline constexpr std::uint32_t OfflineRestartAbsentMs = 30000;
enum class RestartRoster { Wait, DropAbsent, TooFew };
RestartRoster DecideRestartRoster(std::uint32_t stageElapsedMs, std::uint32_t absent, std::uint32_t present,
    bool leaderPresent, std::uint32_t minMembers);

// Teardown: once cleanup has failed (60 s), a member this job no longer holds (other
// group, other master, other body) stops blocking the members it does hold, unless it
// carries a training journal that only a restore on that body can clear. Benchmarks
// keep their stricter recovery and never abandon a participant.
inline constexpr std::uint32_t OfflineCleanupFailMs = 60000;
bool AbandonLostMember(bool benchmark, bool lost, bool trainingJournal, std::uint32_t stageElapsedMs);

// After cleanup_failed was reported, an ordinary offline raid keeps its native slot
// for this long, then releases it unless a member is still provably held (connected,
// same body, in the job's group under the job's driver). Benchmarks never release.
inline constexpr std::uint32_t OfflineSlotReleaseMs = 600000;
enum class SlotRelease { Keep, Wait, Release };
SlotRelease DecideSlotRelease(bool benchmark, bool failedReported, std::uint32_t sinceReportMs,
    std::uint32_t heldMembers);

// Configuration may release the cast pins while a job is running. These checks also apply during setup/restarts,
// before the running-only policy can meaningfully count members or inspect encounter state.
std::string OfflineRaidStopReason(bool humanOnline, bool moduleEnabled, bool keepCast,
    bool driverPresent, std::uint32_t elapsedMs, std::uint32_t timeoutMs, bool instanceStrategies = true);
}

#endif
