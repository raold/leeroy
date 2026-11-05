// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_IDENTITY_H
#define MOD_LEEROY_REALM_IDENTITY_H

#include <algorithm>
#include <cstdint>
#include <optional>
#include <vector>

class Player;
class PlayerbotAI;
class WorldSession;
namespace Leeroy::Realm
{
struct BodyScope
{
    std::uint32_t map = 0, instance = 0;
    std::uint64_t group = 0;
    bool operator==(BodyScope const&) const = default;
};
struct BodyIdentity
{
    std::uint32_t account = 0, guild = 0, map = 0, instance = 0;
    std::uint64_t group = 0;
    std::uint8_t classId = 0, race = 0, sex = 0;
    // Opaque correlation values only. These pointers are never dereferenced by
    // a saved lease; current core lookup is required to use a native object.
    Player* player = nullptr;
    PlayerbotAI* ai = nullptr;
    WorldSession* session = nullptr;
    bool operator==(BodyIdentity const&) const = default;
};
inline BodyScope Scope(BodyIdentity const& body) { return {body.map, body.instance, body.group}; }
inline bool SameLife(BodyIdentity first, BodyIdentity second)
{
    first.map = second.map = first.instance = second.instance = 0;
    first.group = second.group = 0;
    return first == second;
}
enum class LeaseCheck { Exact, LifeChanged, ScopeChanged };
inline LeaseCheck CheckBodyLease(BodyIdentity const& saved, std::uint64_t savedGeneration,
    std::optional<BodyIdentity> const& current, std::uint64_t currentGeneration,
    std::vector<BodyScope> const& declaredScopes)
{
    if (!current || savedGeneration != currentGeneration || !SameLife(saved, *current))
        return LeaseCheck::LifeChanged;
    if (declaredScopes.size() > 4 || (Scope(*current) != Scope(saved)
        && std::find(declaredScopes.begin(), declaredScopes.end(), Scope(*current)) == declaredScopes.end()))
        return LeaseCheck::ScopeChanged;
    return LeaseCheck::Exact;
}
}
#endif
