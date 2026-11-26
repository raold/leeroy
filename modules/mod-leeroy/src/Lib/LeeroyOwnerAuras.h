// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_OWNER_AURAS_H
#define MOD_LEEROY_OWNER_AURAS_H

// Leeroy.OwnerAuras: auras the owner keeps. Leeroy.OwnerPetAuras: auras his pet and his guardians keep.
// The world tick puts back a listed aura they lack; nothing here touches anyone else. Pure policy, no
// core types.

#include <cstdint>
#include <functional>
#include <map>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace Leeroy::Guild
{
inline constexpr std::uint32_t kOwnerAuraPassMs = 2000;
// A listed aura coming off him wakes the next tick, but passes stay at least this far apart, so an aura
// that cannot stay on is tried at most twice a second.
inline constexpr std::uint32_t kOwnerAuraWakeGapMs = 500;
inline constexpr std::uint64_t kOwnerAuraLogMs = 60 * 1000;

struct OwnerAuraList
{
    std::vector<std::uint32_t> spells;   // distinct, in the order written
    std::vector<std::string> rejected;   // entries that are not a positive spell id
};

// "6560, 1234" -> 6560, 1234. Blank means none; an id written twice counts once.
OwnerAuraList ParseOwnerAuras(std::string_view text);

enum class OwnerAuraSpell { Ok, Unknown, NotAnAura };
char const* OwnerAuraSpellName(OwnerAuraSpell check);

struct OwnerAuraChecked
{
    std::vector<std::uint32_t> spells;
    std::vector<std::pair<std::uint32_t, OwnerAuraSpell>> invalid;
};

// Keeps the ids the spell store knows as an aura.
OwnerAuraChecked CheckOwnerAuras(std::vector<std::uint32_t> const& listed,
    std::function<OwnerAuraSpell(std::uint32_t)> const& lookup);

struct OwnerAuraState
{
    bool online = false;        // in world with a session
    bool bot = false;           // a playerbot plays the character (not his own self-bot)
    bool alive = false;
    bool ghost = false;
    bool taxi = false;
    bool vehicle = false;
    bool battleground = false;  // a battleground or an arena
    bool teleporting = false;
};

// Why he takes no aura now ("offline", "bot", "dead", "ghost", "taxi", "vehicle", "battleground",
// "teleporting"), or empty when he can.
std::string_view OwnerAuraBlock(OwnerAuraState const& state);

// The listed spells a unit lacks, in list order; none while `block` names a reason.
std::vector<std::uint32_t> AurasToApply(std::vector<std::uint32_t> const& listed, std::string_view block,
    std::function<bool(std::uint32_t)> const& has);

// The listed spells he lacks, in list order; none while something blocks him.
std::vector<std::uint32_t> OwnerAurasToApply(std::vector<std::uint32_t> const& listed,
    OwnerAuraState const& state, std::function<bool(std::uint32_t)> const& has);

// One of his pets: the pet he controls or a guardian he summoned.
struct OwnerPetState
{
    bool present = false;   // in the world
    bool his = false;       // summoned by the owner's character
    bool alive = false;
    bool charmed = false;   // under someone else's control
};

// Why the pet takes no aura now: his own block first, then "no_pet", "not_his", "pet_dead", "charmed";
// empty when it can.
std::string_view OwnerPetAuraBlock(OwnerAuraState const& owner, OwnerPetState const& pet);

// The listed spells the pet lacks, in list order; none while something blocks him or it.
std::vector<std::uint32_t> OwnerPetAurasToApply(std::vector<std::uint32_t> const& listed,
    OwnerAuraState const& owner, OwnerPetState const& pet, std::function<bool(std::uint32_t)> const& has);

// The units a pet pass visits: his controlled pet first, then each guardian once (0 is no unit).
std::vector<std::uint64_t> OwnerPetTargets(std::uint64_t pet, std::vector<std::uint64_t> const& guardians);

enum class OwnerAuraWhy { Unknown, Dispelled, Death, Expired, Cancelled };
char const* OwnerAuraWhyName(OwnerAuraWhy why);

// The last way each listed aura came off him (or off one of his pets), kept until it is back on.
class OwnerAuraReasons
{
public:
    void Note(std::uint32_t spell, OwnerAuraWhy why) { _why[spell] = why; }
    OwnerAuraWhy Peek(std::uint32_t spell) const;
    void Forget(std::uint32_t spell) { _why.erase(spell); }
    void Clear() { _why.clear(); }

private:
    std::map<std::uint32_t, OwnerAuraWhy> _why;
};

// At most one line a minute per spell and outcome (put back, or refused).
class OwnerAuraLogGate
{
public:
    bool Allow(std::uint32_t spell, bool refused, std::uint64_t nowMs);

private:
    std::map<std::pair<std::uint32_t, bool>, std::uint64_t> _last;
};

// Whether a world tick runs a pass: every kOwnerAuraPassMs, sooner after a wake but never within
// kOwnerAuraWakeGapMs of the last pass. A wake waits for its pass.
class OwnerAuraPass
{
public:
    bool Due(std::uint32_t diffMs, bool woken);

private:
    std::uint32_t _sinceMs = kOwnerAuraPassMs;   // the first tick runs one
    bool _woken = false;
};
}
#endif
