// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyOwnerAuras.h"

#include <algorithm>

namespace Leeroy::Guild
{
namespace
{
bool Separator(char c)
{
    return c == ',' || c == ' ' || c == '\t' || c == '\r' || c == '\n';
}

bool SpellId(std::string_view token, std::uint32_t& id)
{
    if (token.empty() || token.size() > 10)
        return false;
    std::uint64_t value = 0;
    for (char c : token)
    {
        if (c < '0' || c > '9')
            return false;
        value = value * 10 + static_cast<std::uint64_t>(c - '0');
    }
    if (value == 0 || value > 0xFFFFFFFFull)
        return false;
    id = static_cast<std::uint32_t>(value);
    return true;
}
}

OwnerAuraList ParseOwnerAuras(std::string_view text)
{
    OwnerAuraList list;
    std::size_t at = 0;
    while (at < text.size())
    {
        while (at < text.size() && Separator(text[at]))
            ++at;
        std::size_t const start = at;
        while (at < text.size() && !Separator(text[at]))
            ++at;
        if (start == at)
            continue;
        std::string_view const token = text.substr(start, at - start);
        std::uint32_t id = 0;
        if (!SpellId(token, id))
            list.rejected.emplace_back(token);
        else if (std::find(list.spells.begin(), list.spells.end(), id) == list.spells.end())
            list.spells.push_back(id);
    }
    return list;
}

char const* OwnerAuraSpellName(OwnerAuraSpell check)
{
    switch (check)
    {
        case OwnerAuraSpell::Ok: return "ok";
        case OwnerAuraSpell::Unknown: return "unknown_spell";
        case OwnerAuraSpell::NotAnAura: return "not_an_aura";
    }
    return "unknown_spell";
}

OwnerAuraChecked CheckOwnerAuras(std::vector<std::uint32_t> const& listed,
    std::function<OwnerAuraSpell(std::uint32_t)> const& lookup)
{
    OwnerAuraChecked checked;
    for (std::uint32_t spell : listed)
    {
        OwnerAuraSpell const check = lookup(spell);
        if (check == OwnerAuraSpell::Ok)
            checked.spells.push_back(spell);
        else
            checked.invalid.emplace_back(spell, check);
    }
    return checked;
}

std::string_view OwnerAuraBlock(OwnerAuraState const& state)
{
    if (!state.online) return "offline";
    if (state.bot) return "bot";
    if (state.ghost) return "ghost";
    if (!state.alive) return "dead";
    if (state.teleporting) return "teleporting";
    if (state.taxi) return "taxi";
    if (state.vehicle) return "vehicle";
    if (state.battleground) return "battleground";
    return {};
}

std::vector<std::uint32_t> AurasToApply(std::vector<std::uint32_t> const& listed, std::string_view block,
    std::function<bool(std::uint32_t)> const& has)
{
    std::vector<std::uint32_t> missing;
    if (!block.empty())
        return missing;
    for (std::uint32_t spell : listed)
        if (!has(spell))
            missing.push_back(spell);
    return missing;
}

std::vector<std::uint32_t> OwnerAurasToApply(std::vector<std::uint32_t> const& listed,
    OwnerAuraState const& state, std::function<bool(std::uint32_t)> const& has)
{
    return AurasToApply(listed, OwnerAuraBlock(state), has);
}

std::string_view OwnerPetAuraBlock(OwnerAuraState const& owner, OwnerPetState const& pet)
{
    std::string_view const his = OwnerAuraBlock(owner);
    if (!his.empty()) return his;
    if (!pet.present) return "no_pet";
    if (!pet.his) return "not_his";
    if (!pet.alive) return "pet_dead";
    if (pet.charmed) return "charmed";
    return {};
}

std::vector<std::uint32_t> OwnerPetAurasToApply(std::vector<std::uint32_t> const& listed,
    OwnerAuraState const& owner, OwnerPetState const& pet, std::function<bool(std::uint32_t)> const& has)
{
    return AurasToApply(listed, OwnerPetAuraBlock(owner, pet), has);
}

std::vector<std::uint64_t> OwnerPetTargets(std::uint64_t pet, std::vector<std::uint64_t> const& guardians)
{
    std::vector<std::uint64_t> targets;
    if (pet)
        targets.push_back(pet);
    for (std::uint64_t unit : guardians)
        if (unit && std::find(targets.begin(), targets.end(), unit) == targets.end())
            targets.push_back(unit);
    return targets;
}

char const* OwnerAuraWhyName(OwnerAuraWhy why)
{
    switch (why)
    {
        case OwnerAuraWhy::Unknown: return "unknown";
        case OwnerAuraWhy::Dispelled: return "dispelled";
        case OwnerAuraWhy::Death: return "death";
        case OwnerAuraWhy::Expired: return "expired";
        case OwnerAuraWhy::Cancelled: return "cancelled";
    }
    return "unknown";
}

OwnerAuraWhy OwnerAuraReasons::Peek(std::uint32_t spell) const
{
    auto const it = _why.find(spell);
    return it == _why.end() ? OwnerAuraWhy::Unknown : it->second;
}

bool OwnerAuraLogGate::Allow(std::uint32_t spell, bool refused, std::uint64_t nowMs)
{
    auto const key = std::make_pair(spell, refused);
    auto const it = _last.find(key);
    // A clock that went back logs again rather than going quiet for good.
    if (it != _last.end() && nowMs >= it->second && nowMs - it->second < kOwnerAuraLogMs)
        return false;
    _last[key] = nowMs;
    return true;
}

bool OwnerAuraPass::Due(std::uint32_t diffMs, bool woken)
{
    _woken = _woken || woken;
    _sinceMs = diffMs >= kOwnerAuraPassMs - _sinceMs ? kOwnerAuraPassMs : _sinceMs + diffMs;
    if (_sinceMs < (_woken ? kOwnerAuraWakeGapMs : kOwnerAuraPassMs))
        return false;
    _sinceMs = 0;
    _woken = false;
    return true;
}
}
