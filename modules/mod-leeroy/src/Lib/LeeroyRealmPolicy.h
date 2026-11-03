// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_POLICY_H
#define MOD_LEEROY_REALM_POLICY_H

#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <set>
#include <string>
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

struct Kill
{
    std::uint32_t map = 0;
    std::uint32_t instanceId = 0;
    std::uint32_t bossId = 0;
    std::uint64_t atMs = 0;
    bool guildKill = false; // Observed kill by the configured guild, never simulated/canon progress.
    std::uint32_t size = 0;
    bool heroic = false;
};

class KillRing
{
public:
    static constexpr std::size_t Capacity = 32;

    // The world-thread adapter supplies actual observed encounter kills.
    void Add(Kill kill);
    std::optional<Kill> Match(std::uint32_t map, std::uint32_t instanceId, std::uint32_t bossId,
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
