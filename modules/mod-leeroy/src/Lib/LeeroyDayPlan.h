// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_DAY_PLAN_H
#define MOD_LEEROY_DAY_PLAN_H

#include "LeeroyRealmIdentity.h"
#include <map>
#include <array>
#include <string_view>
#include <set>
#include <string>

namespace Leeroy::Realm
{
// A permit authorizes the scheduled sequence. RealmModule's reservations alone own the bodies.
struct DayPermit
{
    struct Member { BodyIdentity life; std::uint64_t generation; };
    struct Step
    {
        std::string kind;
        std::vector<std::uint32_t> members;
        bool released = false, succeeded = false;
        std::optional<std::array<float, 3>> destination;
        std::uint32_t map = 0;
    };
    std::string origin, token, place, activity, reason;
    std::uint64_t serial = 0, epoch = 0, ends = 0;
    std::uint32_t guild = 0, human = 0;
    std::map<std::uint32_t, Member> members;
    std::map<std::string, Step> steps;
    bool unresolved = false;
    bool Covers(std::uint32_t guid) const { return members.contains(guid); }
    bool Released() const
    {
        return !reason.empty() && std::all_of(steps.begin(), steps.end(),
            [](auto const& it) { return it.second.released; });
    }
    bool Allows(std::string const& kind, std::vector<std::uint32_t> const& guids) const
    {
        if (!reason.empty() || unresolved || guids.empty() ||
            std::any_of(guids.begin(), guids.end(), [&](auto g) { return !Covers(g); })) return false;
        auto previous = [&](std::string_view wanted, std::uint32_t g, bool proved)
        {
            return std::any_of(steps.begin(), steps.end(), [&](auto const& it)
            {
                auto const& s = it.second;
                return s.kind == wanted && (!proved || s.succeeded) &&
                    std::find(s.members.begin(), s.members.end(), g) != s.members.end();
            });
        };
        if (kind == "realm_move") return guids.size() == 1 && !previous(kind, guids[0], false);
        for (auto const& [g, body] : members)
            if (!previous("realm_move", g, true)) return false;
        if (kind == "duel")
            return activity == "duel_practice" && guids.size() == 2 && guids[0] != guids[1]
                && !previous(kind, guids[0], false);
        if (guids.size() != 1 || previous(kind, guids[0], false)) return false;
        if (activity == "fishing") return kind == "guild_fishing";
        return kind == "rpg_status" && activity != "duel_practice";
    }
};
inline std::string_view DayStatusFor(std::string_view activity)
{
    if (activity == "city" || activity == "holiday") return "wander npc";
    if (activity == "inn") return "rest";
    if (activity == "camp") return "idle";
    if (activity == "gathering") return "do gather";
    if (activity == "questing") return "do quest";
    return {};
}
inline bool DayPlaceMatches(std::string_view activity, std::string_view kind)
{
    if (activity == "duel_practice") return kind == "duel";
    if (activity == "gathering") return kind == "herb" || kind == "ore";
    if (activity == "fishing") return kind == "fish";
    if (activity == "questing") return kind == "camp";
    return (activity == "city" || activity == "inn" || activity == "camp" || activity == "holiday")
        && activity == kind;
}
}
#endif
