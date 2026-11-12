// This file is part of leeroy, released under the GNU GPL v2.
#ifndef LEEROY_WORLD_GUILDS_H
#define LEEROY_WORLD_GUILDS_H
#include <algorithm>
#include <type_traits>
#include <vector>
namespace Leeroy::World
{
// AzerothCore GuildMgr exposes individual lookups, but no public enumeration.
// Keep the dependency on its protected GuildStore in this one adapter. The
// member pointer is applied to the real manager; no downcast or fake instance.
// This must run on the world thread, where GuildMgr owns the guild lifetimes.
template<class Manager>
class GuildSnapshot : public Manager
{
public:
    static auto All(Manager const& manager)
    {
        auto const& store = manager.*(&GuildSnapshot::GuildStore);
        using Pointer = typename std::remove_cvref_t<decltype(store)>::mapped_type;
        std::vector<Pointer> result;
        for (auto const& [id,guild] : store) if (guild) result.push_back(guild);
        std::sort(result.begin(),result.end(),[](auto a,auto b) { return a->GetId() < b->GetId(); });
        return result;
    }
};
}
#endif
