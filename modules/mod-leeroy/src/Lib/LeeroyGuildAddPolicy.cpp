// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyGuildAddPolicy.h"

#include <algorithm>
#include <cstdlib>
#include <initializer_list>
#include <iterator>
#include <string_view>
#include <utility>

namespace Leeroy::Guild
{
namespace
{
std::size_t Index(GuildAddRole role) { return static_cast<std::size_t>(role); }

char Lower(char c) { return c >= 'A' && c <= 'Z' ? static_cast<char>(c - 'A' + 'a') : c; }

bool EqualI(std::string_view a, std::string_view b)
{
    return a.size() == b.size() && std::equal(a.begin(), a.end(), b.begin(),
        [](char x, char y) { return Lower(x) == Lower(y); });
}

// The core and playerbots both split on single spaces only.
std::string_view Word(std::string_view& rest)
{
    while (!rest.empty() && rest.front() == ' ')
        rest.remove_prefix(1);
    auto const end = rest.find(' ');
    auto const word = rest.substr(0, end);
    rest.remove_prefix(end == std::string_view::npos ? rest.size() : end);
    return word;
}

std::string Count(std::uint32_t n, char const* one, char const* many)
{
    return std::to_string(n) + " " + (n == 1 ? one : many);
}

// "a", "a and b", "a, b and c".
std::string Join(std::vector<std::string> const& items)
{
    std::string list;
    for (std::size_t i = 0; i < items.size(); ++i)
        list += (i == 0 ? "" : i + 1 == items.size() ? " and " : ", ") + items[i];
    return list;
}

std::string Where(GuildAddPlace const& place)
{
    return std::to_string(place.cap) + "-man " + (place.raid ? "raid" : "dungeon");
}

using B = GuildAddBuff;
constexpr std::uint8_t kWarrior = 1, kPaladin = 2, kHunter = 3, kRogue = 4, kPriest = 5, kDeathKnight = 6,
    kShaman = 7, kMage = 8, kWarlock = 9, kDruid = 11;

constexpr GuildAddBuffs Bits(std::initializer_list<GuildAddBuff> buffs)
{
    GuildAddBuffs bits = 0;
    for (auto buff : buffs)
        bits |= GuildAddBuffBit(buff);
    return bits;
}

// What every member of a class brings, whatever the tree.
GuildAddBuffs ClassBuffs(std::uint8_t cls)
{
    switch (cls)
    {
        case kWarrior: return Bits({B::ArmorReduction});                // Sunder Armor
        case kPaladin: return Bits({B::Kings});                         // Blessing of Kings
        case kRogue: return Bits({B::ArmorReduction});                  // Expose Armor
        case kPriest: return Bits({B::Fortitude});                      // Power Word: Fortitude
        case kShaman: return Bits({B::Bloodlust});                      // Bloodlust / Heroism
        case kMage: return Bits({B::ArcaneIntellect});                  // Arcane Intellect
        case kWarlock: return Bits({B::SpellDamageTaken});              // Curse of the Elements
        case kDruid: return Bits({B::MarkOfTheWild, B::BattleRes});     // Mark of the Wild, Rebirth
        // Hunters and death knights bring theirs by tree; Raise Ally raises a ghoul in 3.3.5, not the member.
        default: return 0;
    }
}

// What a talent tree (the client's order) adds to its class's buffs in 3.3.5, after the talent that gives each.
struct Tree
{
    std::uint8_t cls;
    std::int8_t tab;
    GuildAddBuffs buffs;
};
constexpr Tree kTrees[] = {
    {kWarrior, 0, Bits({B::BleedDamage, B::PhysicalDamageTaken})},       // Arms: Trauma, Blood Frenzy
    {kWarrior, 1, Bits({B::PhysicalCrit})},                              // Fury: Rampage
    // Retribution: Judgements of the Wise, Sanctified Retribution, Heart of the Crusader.
    {kPaladin, 2, Bits({B::Replenishment, B::Damage, B::CritTaken})},
    {kHunter, 0, Bits({B::Damage})},                                     // Beast Mastery: Ferocious Inspiration
    {kHunter, 1, Bits({B::AttackPower})},                                // Marksmanship: Trueshot Aura
    {kHunter, 2, Bits({B::Replenishment})},                              // Survival: Hunting Party
    {kRogue, 0, Bits({B::CritTaken})},                                   // Assassination: Master Poisoner
    {kRogue, 1, Bits({B::PhysicalDamageTaken})},                         // Combat: Savage Combat
    {kPriest, 2, Bits({B::Replenishment, B::SpellHit})},                 // Shadow: Vampiric Touch, Misery
    {kDeathKnight, 0, Bits({B::AttackPower})},                           // Blood: Abomination's Might
    {kDeathKnight, 1, Bits({B::MeleeHaste})},                            // Frost: Improved Icy Talons
    {kDeathKnight, 2, Bits({B::SpellDamageTaken})},                      // Unholy: Ebon Plaguebringer
    // Elemental: Elemental Oath; Totem of Wrath's spell power and its 3% crit taken.
    {kShaman, 0, Bits({B::SpellCrit, B::CritTaken, B::SpellPower})},
    {kShaman, 1, Bits({B::MeleeHaste, B::AttackPower})},                 // Enhancement: Imp. Windfury, Unleashed Rage
    {kMage, 0, Bits({B::Damage})},                                       // Arcane: Arcane Empowerment
    {kMage, 2, Bits({B::Replenishment})},                                // Frost: Enduring Winter
    {kWarlock, 1, Bits({B::SpellPower})},                                // Demonology: Demonic Pact
    {kWarlock, 2, Bits({B::Replenishment})},                             // Destruction: Improved Soul Leech
    // Balance: Moonkin Aura, Earth and Moon, Improved Faerie Fire.
    {kDruid, 0, Bits({B::SpellCrit, B::SpellDamageTaken, B::SpellHit})},
    {kDruid, 1, Bits({B::PhysicalCrit, B::BleedDamage})},                // Feral: Leader of the Pack, Mangle
};

bool Melee(std::uint8_t cls, std::int8_t tab, GuildAddRole role)
{
    if (role != GuildAddRole::Damage)
        return role == GuildAddRole::Tank;
    switch (cls)
    {
        case kWarrior: case kPaladin: case kRogue: case kDeathKnight: return true;
        case kShaman: case kDruid: return tab == 1;   // Enhancement, Feral
        default: return false;
    }
}

GuildAddHealer HealerKind(std::uint8_t cls, std::int8_t tab)
{
    switch (cls)
    {
        case kPaladin: return tab == 0 ? GuildAddHealer::Tank : GuildAddHealer::None;            // Beacon of Light
        // Discipline: Power Word: Shield and Divine Aegis; Holy: Circle of Healing.
        case kPriest:
            return tab == 0 ? GuildAddHealer::Absorb : tab == 1 ? GuildAddHealer::RaidPriest : GuildAddHealer::None;
        case kShaman: return tab == 2 ? GuildAddHealer::RaidShaman : GuildAddHealer::None;       // Chain Heal
        case kDruid: return tab == 2 ? GuildAddHealer::RaidDruid : GuildAddHealer::None;         // Wild Growth
        default: return GuildAddHealer::None;
    }
}

bool IsRaidHealer(GuildAddHealer kind)
{
    return kind == GuildAddHealer::RaidShaman || kind == GuildAddHealer::RaidDruid
        || kind == GuildAddHealer::RaidPriest;
}

// What the missing line names, in GuildAddBuff order: Bloodlust, Replenishment and the % buffs and debuffs.
constexpr char const* kNamed[] = {"Bloodlust/Heroism", "Replenishment", "20% melee haste", "5% spell crit",
    "10% attack power", "3% damage", "5% physical crit", "13% spell damage taken", "30% bleed damage",
    "4% physical damage taken", "3% spell hit", "3% crit taken"};
static_assert(std::size(kNamed) == static_cast<std::size_t>(GuildAddBuff::CritTaken) + 1);

std::int64_t Coverage(GuildAddBuffs buffs)
{
    std::int64_t weight = 0;
    for (std::size_t i = 0; i < kGuildAddBuffs; ++i)
        if (buffs & GuildAddBuffBit(static_cast<GuildAddBuff>(i)))
            weight += GuildAddBuffWeight(static_cast<GuildAddBuff>(i));
    return weight;
}

// A healer template's slot: which kinds of healer it wants.
enum class Slot : std::uint8_t { TankOrAbsorb, Tank, Absorb, Raid };

// In order of need, so a raid that can take only some of its healers still mixes them.
std::vector<Slot> HealerTemplate(std::uint32_t size)
{
    switch (size)
    {
        case 5: return {Slot::TankOrAbsorb};
        case 10: return {Slot::Tank, Slot::Absorb, Slot::Raid};
        // Two Holy paladins, one Discipline priest, three raid healers.
        case 25: return {Slot::Tank, Slot::Absorb, Slot::Raid, Slot::Tank, Slot::Raid, Slot::Raid};
        // The 25's scaled to ten healers: three, two and five.
        case 40: return {Slot::Tank, Slot::Absorb, Slot::Raid, Slot::Tank, Slot::Raid, Slot::Absorb, Slot::Raid,
            Slot::Tank, Slot::Raid, Slot::Raid};
        default: return {};
    }
}

bool Fits(Slot slot, GuildAddHealer kind)
{
    switch (slot)
    {
        case Slot::TankOrAbsorb: return kind == GuildAddHealer::Tank || kind == GuildAddHealer::Absorb;
        case Slot::Tank: return kind == GuildAddHealer::Tank;
        case Slot::Absorb: return kind == GuildAddHealer::Absorb;
        default: return IsRaidHealer(kind);
    }
}

// A candidate's place in one pick, compared in order: lower comes first.
using Rank = std::array<std::int64_t, 5>;
}

std::uint32_t GuildAddBuffWeight(GuildAddBuff buff)
{
    if (buff <= GuildAddBuff::Replenishment)
        return 100;
    return buff <= GuildAddBuff::CritTaken ? 10 : 1;
}

GuildAddSpec GuildAddSpecFacts(std::uint8_t cls, std::int8_t tab, GuildAddRole role)
{
    GuildAddSpec spec;
    spec.buffs = ClassBuffs(cls);
    for (auto const& tree : kTrees)
        if (tree.cls == cls && tree.tab == tab)
            spec.buffs |= tree.buffs;
    spec.melee = Melee(cls, tab, role);
    if (role == GuildAddRole::Healer)
        spec.healer = HealerKind(cls, tab);
    return spec;
}

std::string GuildAddMissingLine(GuildAddBuffs covered)
{
    std::vector<std::string> missing;
    for (std::size_t i = 0; i < std::size(kNamed); ++i)
        if (!(covered & GuildAddBuffBit(static_cast<GuildAddBuff>(i))))
            missing.push_back(kNamed[i]);
    return missing.empty() ? std::string() : "Still missing: " + Join(missing);
}

std::optional<GuildAddCounts> GuildAddComposition(std::uint32_t size)
{
    switch (size)
    {
        case 5: return GuildAddCounts{1, 1, 3};
        case 10: return GuildAddCounts{2, 3, 5};
        case 25: return GuildAddCounts{3, 6, 16};
        case 40: return GuildAddCounts{4, 10, 26};
        default: return std::nullopt;
    }
}

std::uint32_t ParseGuildAddCommand(std::vector<std::string> const& completions)
{
    if (completions.size() != 1)
        return 0;
    std::string_view rest = completions.front();
    if (!EqualI(Word(rest), "playerbots") || !EqualI(Word(rest), "bot"))
        return 0;
    auto const sub = Word(rest);
    for (std::uint32_t size : {5u, 10u, 25u, 40u})
        if (sub == "add" + std::to_string(size))
            return size;
    return 0;
}

bool GuildAddIntercepts(GuildAddGate const& gate)
{
    return !gate.console && gate.enabled && gate.human && gate.invoker == gate.human && gate.guild && gate.cast;
}

std::string GuildAddSizeRefusal(std::uint32_t size, GuildAddPlace const& place)
{
    if (!place.cap || size <= place.cap || !GuildAddComposition(size))
        return {};
    std::uint32_t fit = 0;
    std::vector<std::string> outside;
    for (std::uint32_t each : {5u, 10u, 25u, 40u})
        if (each <= place.cap)
            fit = each;
        else
            outside.push_back("add" + std::to_string(each));
    std::string const here = !fit ? std::string("no add command fits here")
        : "add" + std::to_string(fit) + (place.raid ? " fits here" : " tops this party up");
    return "You are in a " + Where(place) + ": " + here + "; " + Join(outside)
        + (outside.size() == 1 ? " works" : " work") + " outside or in " + (place.raid ? "a bigger raid" : "a raid");
}

std::string GuildAddEntryRefusal(std::uint32_t members, GuildAddPlace const& place)
{
    if (!place.cap || members <= place.cap)
        return {};
    return "Your group of " + std::to_string(members) + " does not fit this " + Where(place)
        + ", so nobody was brought in";
}

std::string GuildAddSkipReason(GuildAddBody const& body)
{
    if (!body.online) return "offline";
    if (!body.cast) return "not_cast";
    if (!body.fence.empty()) return body.fence;
    if (body.otherTeam) return "other_team";
    if (body.dead) return "dead";
    if (body.inCombat) return "in_combat";
    if (body.grouped) return "grouped";
    if (body.inBattleground) return "in_bg";
    if (body.queued) return "queued";
    if (body.teleporting) return "teleporting";
    if (body.inFlight) return "in_flight";
    if (body.elsewhere) return "in_instance";
    if (body.locked) return "locked";
    return {};
}

GuildAddPlan PlanGuildAdd(std::uint32_t size, std::uint32_t members, std::vector<GuildAddMember> const& connected,
    std::vector<GuildAddCandidate> candidates)
{
    GuildAddPlan plan;
    plan.size = size;
    plan.members = std::max<std::uint32_t>(members, static_cast<std::uint32_t>(connected.size()));
    auto const composition = GuildAddComposition(size);
    if (!composition)
        return plan;

    // What his group has already: classes (all and tanking), healer kinds, buffs and the damage's melee/ranged split.
    GuildAddCounts needed = *composition;
    std::array<std::uint32_t, 256> classes{}, tankClasses{};
    std::array<std::uint32_t, static_cast<std::size_t>(GuildAddHealer::RaidPriest) + 1> healers{};
    std::vector<GuildAddHealer> hisHealers;
    std::int64_t melee = 0, ranged = 0;
    for (auto const& member : connected)
    {
        auto& slots = needed[Index(member.role)];
        if (slots)
            --slots;
        ++classes[member.cls];
        auto const spec = GuildAddSpecFacts(member.cls, member.tab, member.role);
        plan.covered |= spec.buffs;
        if (member.role == GuildAddRole::Tank)
            ++tankClasses[member.cls];
        else if (member.role == GuildAddRole::Healer)
        {
            ++healers[static_cast<std::size_t>(spec.healer)];
            hisHealers.push_back(spec.healer);
        }
        else
            ++(spec.melee ? melee : ranged);
    }
    if (plan.members >= size)
    {
        plan.full = true;
        return plan;
    }

    std::uint32_t budget = size - plan.members;
    for (std::size_t role = 0; role < kGuildAddRoles; ++role)
    {
        plan.open[role] = std::min(needed[role], budget);
        budget -= plan.open[role];
    }

    std::sort(candidates.begin(), candidates.end(),
        [](GuildAddCandidate const& a, GuildAddCandidate const& b) { return a.guid < b.guid; });
    candidates.erase(std::unique(candidates.begin(), candidates.end(),
        [](GuildAddCandidate const& a, GuildAddCandidate const& b) { return a.guid == b.guid; }), candidates.end());
    std::vector<GuildAddSpec> specs;
    for (auto const& candidate : candidates)
        specs.push_back(GuildAddSpecFacts(candidate.cls, candidate.tab, candidate.role));
    std::vector<bool> taken(candidates.size());
    GuildAddBuffs covered = plan.covered;

    std::size_t const none = candidates.size();
    // The free candidate of a role that ranks lowest; guid ends every rank, so there is one answer.
    auto best = [&](GuildAddRole role, auto fits, auto rank)
    {
        std::size_t pick = none;
        Rank pickRank{};
        for (std::size_t i = 0; i < candidates.size(); ++i)
        {
            if (taken[i] || !candidates[i].guid || candidates[i].role != role || !fits(i))
                continue;
            Rank const candidateRank = rank(i);
            if (pick == none || candidateRank < pickRank)
            {
                pick = i;
                pickRank = candidateRank;
            }
        }
        return pick;
    };
    auto take = [&](std::size_t i)
    {
        auto const& candidate = candidates[i];
        auto const& spec = specs[i];
        taken[i] = true;
        ++classes[candidate.cls];
        covered |= spec.buffs;
        if (candidate.role == GuildAddRole::Tank)
            ++tankClasses[candidate.cls];
        else if (candidate.role == GuildAddRole::Healer)
            ++healers[static_cast<std::size_t>(spec.healer)];
        else
            ++(spec.melee ? melee : ranged);
        plan.picks.push_back({candidate.guid, candidate.role, spec.buffs});
    };
    auto any = [](std::size_t) { return true; };
    auto gear = [&](std::size_t i) { return -static_cast<std::int64_t>(candidates[i].gear); };
    auto guid = [&](std::size_t i) { return static_cast<std::int64_t>(candidates[i].guid); };
    auto cls = [&](std::size_t i) { return static_cast<std::int64_t>(classes[candidates[i].cls]); };

    // Tanks: a tank class not yet tanking, then gear.
    for (std::uint32_t slot = 0; slot < plan.open[Index(GuildAddRole::Tank)]; ++slot)
    {
        auto const i = best(GuildAddRole::Tank, any, [&](std::size_t c)
            { return Rank{tankClasses[candidates[c].cls], gear(c), cls(c), guid(c), 0}; });
        if (i == none)
            break;
        take(i);
    }

    // Healers: the template's slots his own healers do not fill, first each from its own kind (raid healers mixed),
    // then whatever is still empty from the best gear left.
    auto slots = HealerTemplate(size);
    for (auto kind : hisHealers)
        if (auto mine = std::find_if(slots.begin(), slots.end(), [kind](Slot slot) { return Fits(slot, kind); });
            mine != slots.end())
            slots.erase(mine);
    slots.resize(std::min<std::size_t>(slots.size(), plan.open[Index(GuildAddRole::Healer)]));
    std::vector<bool> filled(slots.size());
    for (std::size_t s = 0; s < slots.size(); ++s)
    {
        auto const i = best(GuildAddRole::Healer, [&](std::size_t c) { return Fits(slots[s], specs[c].healer); },
            [&](std::size_t c)
            {
                auto const kind = static_cast<std::size_t>(specs[c].healer);
                std::int64_t const mix = slots[s] == Slot::Raid ? healers[kind] : 0;
                return Rank{mix, gear(c), cls(c), guid(c), 0};
            });
        if (i != none)
        {
            take(i);
            filled[s] = true;
        }
    }
    for (std::size_t s = 0; s < slots.size(); ++s)
        if (!filled[s])
        {
            auto const i = best(GuildAddRole::Healer, any,
                [&](std::size_t c) { return Rank{gear(c), cls(c), guid(c), 0, 0}; });
            if (i == none)
                break;
            take(i);
        }

    // Damage: the most weight of buffs nobody brings yet, then gear, then the melee/ranged split nearest even.
    for (std::uint32_t slot = 0; slot < plan.open[Index(GuildAddRole::Damage)]; ++slot)
    {
        auto const i = best(GuildAddRole::Damage, any, [&](std::size_t c)
            {
                std::int64_t const split = specs[c].melee ? melee + 1 - ranged : melee - ranged - 1;
                return Rank{-Coverage(specs[c].buffs & ~covered), gear(c), std::abs(split), cls(c), guid(c)};
            });
        if (i == none)
            break;
        take(i);
    }
    return plan;
}

std::vector<std::string> GuildAddMessages(GuildAddPlan const& plan, GuildAddCounts const& added,
    std::uint32_t notBrought, std::map<std::string, std::uint32_t> const& unavailable)
{
    if (plan.full)
        return {"Your group already has " + std::to_string(plan.members) + " members"};

    std::vector<std::string> messages;
    messages.push_back("Adding " + std::to_string(added[0]) + " tanks, " + std::to_string(added[1]) + " healers and "
        + std::to_string(added[2]) + " damage from the guild for a " + std::to_string(plan.size) + "-man group");

    GuildAddCounts empty{};
    std::uint32_t total = 0;
    for (std::size_t role = 0; role < kGuildAddRoles; ++role)
    {
        empty[role] = plan.open[role] > added[role] ? plan.open[role] - added[role] : 0;
        total += empty[role];
    }
    if (total)
    {
        std::vector<std::string> roles;
        if (empty[0]) roles.push_back(Count(empty[0], "tank", "tanks"));
        if (empty[1]) roles.push_back(Count(empty[1], "healer", "healers"));
        if (empty[2]) roles.push_back(Count(empty[2], "damage", "damage"));
        messages.push_back(Count(total, "slot", "slots") + " left empty: no free guild member for " + Join(roles));

        std::vector<std::pair<std::string, std::uint32_t>> reasons(unavailable.begin(), unavailable.end());
        std::stable_sort(reasons.begin(), reasons.end(),
            [](auto const& a, auto const& b) { return a.second > b.second; });
        std::string tally;
        for (auto const& [reason, count] : reasons)
            if (count)
                tally += (tally.empty() ? "" : ", ") + std::to_string(count) + " " + reason;
        if (!tally.empty())
            messages.push_back("Not free: " + tally);
    }
    if (notBrought)
        messages.push_back(Count(notBrought, "member", "members") + " joined but could not be brought to you");
    return messages;
}
}
