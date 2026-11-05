// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_TRAINING_TAGS_H
#define MOD_LEEROY_TRAINING_TAGS_H
#include <cstddef>
#include <cstdint>
#include <utility>

namespace Leeroy::Training
{
struct TagScope
{
    std::uint64_t key = 0, generation = 0;
    std::size_t slot = 0;
    bool operator==(TagScope const&) const = default;
};
struct SpellAttribution { std::uint32_t spell = 0, raw = 0; bool known = false; };

// Allocation-free, thread-local, single-use pair attribution. It is deliberately best-effort: the core
// callbacks expose no shared event ID. A healing path invalidates damage tags and vice versa.
class TrainingTags
{
public:
    void Clear() { _damage = {}; _heal = {}; }
    void Damage(std::uint64_t a, std::uint64_t b, TagScope scope, std::uint32_t spell)
    {
        Clear();
        _damage = Make(a, b, scope, spell, 0);
    }
    void Heal(std::uint64_t a, std::uint64_t b, TagScope scope, std::uint32_t spell, std::uint32_t raw)
    {
        Clear();
        _heal = Make(a, b, scope, spell, raw);
    }
    SpellAttribution TakeDamage(std::uint64_t a, std::uint64_t b, TagScope scope)
    {
        auto tag = _damage;
        Clear();
        return Take(tag, a, b, scope);
    }
    SpellAttribution TakeHeal(std::uint64_t a, std::uint64_t b, TagScope scope)
    {
        auto tag = _heal;
        Clear();
        return Take(tag, a, b, scope);
    }
private:
    struct Tag
    {
        std::uint64_t a = 0, b = 0;
        TagScope scope{};
        SpellAttribution value{};
    };
    static Tag Make(std::uint64_t a, std::uint64_t b, TagScope scope, std::uint32_t spell, std::uint32_t raw)
    {
        if (a > b) std::swap(a, b);
        return {a, b, scope, {spell, raw, scope.generation != 0}};
    }
    static SpellAttribution Take(Tag const& tag, std::uint64_t a, std::uint64_t b, TagScope scope)
    {
        if (a > b) std::swap(a, b);
        return tag.value.known && tag.a == a && tag.b == b && tag.scope == scope
            ? tag.value : SpellAttribution{};
    }
    Tag _damage{}, _heal{};
};
}
#endif
