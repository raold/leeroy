// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_DUMMY_DAMAGE_H
#define MOD_LEEROY_DUMMY_DAMAGE_H
#include <array>
#include <cstdint>
#include <vector>

namespace Leeroy::Training
{
// Conservative pre-AI positive incoming damage requests. A false positive aborts a trial rather than
// labelling an adjusted/absorbed request as verified health loss. Map writer, world-barrier reader.
class DummyDamageAlarm
{
public:
    void Reset(std::uint64_t dummy, std::vector<std::uint32_t> const& members)
    {
        _dummy = dummy;
        _members.fill(0);
        _attacked.fill(false);
        for (std::size_t i = 0; i < members.size() && i < _members.size(); ++i) _members[i] = members[i];
    }
    void Damage(std::uint64_t attacker, std::uint32_t victim, std::uint32_t requested)
    {
        if (!requested || !victim || attacker == _dummy) return;
        for (std::size_t i = 0; i < _members.size(); ++i)
            if (_members[i] == victim) _attacked[i] = true;
    }
    bool Attacked(std::uint32_t member) const
    {
        if (!member) return false;
        for (std::size_t i = 0; i < _members.size(); ++i)
            if (_members[i] == member && _attacked[i]) return true;
        return false;
    }
private:
    std::uint64_t _dummy = 0;
    std::array<std::uint32_t, 10> _members{};
    std::array<bool, 10> _attacked{};
};
}
#endif
