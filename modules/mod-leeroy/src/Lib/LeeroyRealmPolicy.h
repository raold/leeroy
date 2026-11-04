// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_POLICY_H
#define MOD_LEEROY_REALM_POLICY_H

#include "LeeroyRealmAttempts.h"

#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <unordered_set>
#include <vector>

namespace Leeroy::Realm
{
struct BodyState
{
    bool online = false;
    bool dead = false;
    bool combat = false;
    bool flight = false;
    bool raidGroup = false;
    bool pvpOrLfgGroup = false;
    bool humanGroup = false;
    bool instance = false;
    bool battleground = false;
    bool arena = false;
    bool lfg = false;
};

enum class Purpose { Move, Status, LeavePvp, Rearm, WgJoin, Duel, Craft, Bank, Equip, Mail };

// Gameplay eligibility only. Even an empty reason never authorizes a mutation:
// runtime adapters separately require the exact current body lease and complete
// previous-owner cleanup proof. LeavePvp/Rearm waivers cannot bypass that fence.
std::string BusyReason(BodyState const& state, Purpose purpose);

enum class LfgPhase { None, Queued, RoleCheck, Proposal, RaidBrowser, Dungeon, FinishedDungeon, Boot };
enum class LfgExit { None, LeaveQueue, LeaveGroup };
LfgExit DecideLfgExit(LfgPhase phase, bool inLfgGroup);

struct HumanPvp
{
    bool online = false;
    bool manualFlag = false;
    bool battleground = false;
    bool arena = false;
    bool wgWar = false;
    std::uint32_t zone = 0;
};

// Contested-zone automatic PvP flags are deliberately not an input.
bool Flagged(HumanPvp const& human);
bool GankerMoveAllowed(HumanPvp const& human, std::uint32_t destZone);

struct KillIdentity
{
    std::uint64_t nativeEpoch = 0;
    std::uint32_t guildId = 0, humanGuid = 0;
    std::uint32_t map = 0;
    std::uint32_t instanceId = 0;
    std::uint32_t bossId = 0;
    std::int32_t difficulty = -1;
    std::uint32_t size = 0;
    bool heroic = false;
    bool operator==(KillIdentity const&) const = default;
};

// Immutable map-thread observation, completed with id/publication time only by
// the world-thread inbox drain. Unix and steady timestamps are never converted.
struct Kill : KillIdentity
{
    std::string killId;
    std::uint64_t atMs = 0, verifiedMs = 0; // Independent steady clock.
    std::uint64_t killedAtMs = 0, verifiedAtMs = 0, publishedAtMs = 0; // Unix ms.
    std::uint64_t fightStartedAtMs = 0; // Unix chronology only; never used for elapsed time.
    std::int64_t fightMs = -1; // Checked steady elapsed; (0, -1) is unavailable.
    std::uint32_t attempts = 0; // AttemptLedger's exact count, 1..kMaxAttempts; 0 is unknown.
    bool humanPresent = false;
    std::vector<std::uint32_t> castMembers; // Sorted, unique, present native cast only.
    bool operator==(Kill const&) const = default;
};

inline constexpr std::size_t kMaxKillPlayers = 128;
inline constexpr std::uint64_t kKillVerifyWithinMs = 5000;
inline constexpr std::uint64_t kMaxFightMs = 3600000;
inline constexpr std::uint64_t kMaxFightStartedAtMs = 253402300799999;
bool ValidFightDuration(std::uint64_t startedAtMs, std::int64_t elapsedMs,
    std::uint64_t killedAtMs, bool humanPresent);
bool ValidKillId(std::string_view id);
bool ValidRaidMode(std::int32_t difficulty, std::uint32_t size, bool heroic);
bool ValidKillObservation(Kill const& kill);
bool ValidKillProof(Kill const& kill);

struct PresentKillPlayer { std::uint32_t guid = 0, guildId = 0; };
struct KillParticipants
{
    bool humanPresent = false;
    std::vector<std::uint32_t> castMembers;
};
// Caller must supply the COMPLETE map-player scan. Overflow/duplicate/unknown
// player identity refuses the whole observation, including a human-only kill.
std::optional<KillParticipants> QualifyKillParticipants(std::vector<PresentKillPlayer> const& present,
    std::unordered_set<std::uint32_t> const& capturedCast, std::uint32_t guild, std::uint32_t human);

// Pure observation state. The adapter supplies actual state from the same map
// thread; an intent alone never confirms a start or a completed encounter.
enum class FightState { Resting, Progress, Done, Other };
struct FightContext : KillIdentity
{
    std::uintptr_t mapIdentity = 0; // Identity only; never dereferenced here.
    std::uint64_t lifecycle = 0;
    std::uint8_t spawnMode = 0;
    bool operator==(FightContext const&) const = default;
};
struct FightDuration
{
    std::uint64_t startedAtMs = 0;
    std::int64_t elapsedMs = -1;
    bool operator==(FightDuration const&) const = default;
};
// Lives only inside the existing bounded PendingKill until its DONE verifier.
// The adapter cancels that whole candidate on any subsequent same-boss hook.
struct FightTerminal
{
    FightContext context;
    std::uint64_t generation = 0, startMs = 0, atMs = 0;
    FightDuration duration;
};
class FightStarts
{
public:
    static constexpr std::size_t Capacity = 128;
    void Clear();
    void ForgetMap(std::uintptr_t mapIdentity);
    void ForgetBoss(std::uintptr_t mapIdentity, std::uint32_t boss);
    void Synchronize(std::uint64_t epoch, std::uint32_t guild, std::uint32_t human, std::uint64_t nowMs);
    // Returns the immutable start generation only for a terminal intent with an
    // already confirmed start, or a unique candidate confirmed by actual Progress.
    std::uint64_t Intent(FightContext const&, FightState old, FightState next, FightState actual,
        std::uint64_t nowMs, std::uint64_t unixMs, bool humanPresent);
    void ObserveActual(FightContext const&, FightState actual, std::uint64_t nowMs);
    FightTerminal CaptureTerminal(FightContext const&, std::uint64_t generation,
        std::uint64_t terminalMs, std::uint64_t terminalUnixMs, bool humanPresent);
    FightDuration Finish(FightTerminal&, FightContext const& current, FightState actual, std::uint64_t nowMs);
    std::vector<FightContext> Contexts(std::uintptr_t mapIdentity) const;
    std::size_t Size() const { return _starts.size(); }

private:
    struct Start
    {
        FightContext context;
        std::uint64_t generation, atMs, unixMs, lastMs;
        bool confirmed = false;
    };
    void Prune(std::uint64_t nowMs);
    std::vector<Start> _starts;
    std::uint64_t _generation = 0;
};

class KillRing
{
public:
    static constexpr std::size_t Capacity = 32;

    // The world-thread adapter supplies actual observed encounter kills.
    bool Add(Kill kill);
    // There is intentionally no tuple-based lookup or current-mode default.
    std::optional<Kill> MatchProof(std::string_view killId, KillIdentity const& expected,
        std::uint64_t nowMs, std::uint64_t windowMs = 600000) const;

private:
    std::array<Kill, Capacity> _kills{};
    std::size_t _next = 0;
    std::size_t _count = 0;
};

class TickBudget
{
public:
    explicit TickBudget(std::uint32_t cap);
    bool Take();
    bool Take(std::uint32_t count); // Atomic: a failed full-closure charge consumes nothing.
    std::uint32_t Left() const;
    std::uint32_t Cap() const { return _cap; }
    void Reset();

private:
    std::uint32_t _cap;
    std::uint32_t _left;
};

struct WeaponRecord
{
    std::uint32_t mainHand = 0;
    std::uint32_t offHand = 0;
    std::uint32_t ranged = 0;
};

struct WeaponNow
{
    std::uint32_t mainHand = 0;
    std::uint32_t offHand = 0;
    std::uint32_t ranged = 0;
    bool mainIsPole = false;
};

enum class WeaponSlot { Main, Off, Ranged };
struct RearmStep
{
    WeaponSlot slot;
    std::uint32_t item; // Exact inventory item GUID low, not a template ID.
    bool operator==(RearmStep const&) const = default;
};

bool ShouldRecord(WeaponNow const& now);
// Only distinct recorded items present in bags and absent from equipped slots.
// Ambiguous duplicate recorded GUIDs are omitted. This neither creates missing
// items nor proves slot compatibility: runtime revalidates lease, inventory and
// normal CanEquipItem constraints immediately before applying each step.
std::vector<RearmStep> PlanRearm(WeaponRecord const& record, WeaponNow const& now,
                               std::set<std::uint32_t> const& bagItems);
}

#endif
