// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_DAY_PLAN_H
#define MOD_LEEROY_DAY_PLAN_H

#include "LeeroyRealmIdentity.h"
#include "LeeroyRealmCommands.h"
#include "LeeroyRealmContinuations.h"
#include <map>
#include <array>
#include <cmath>
#include <string_view>
#include <nlohmann/json.hpp>
#include <string>

namespace Leeroy::Realm
{
// A permit authorizes the scheduled sequence. RealmModule's reservations alone own the bodies.
struct DayPermit
{
    struct Member
    {
        BodyIdentity life;
        std::uint64_t generation;
    };
    struct Step
    {
        std::string kind;
        std::vector<std::uint32_t> members;
        bool released = false, succeeded = false;
        std::optional<std::array<float, 3>> destination{};
        std::uint32_t map = 0;
        bool InTransitTo(std::uint32_t mapId, float x, float y, float z) const
        {
            return destination && map == mapId && std::abs(x - (*destination)[0]) < 0.1f &&
                   std::abs(y - (*destination)[1]) < 0.1f && std::abs(z - (*destination)[2]) < 0.1f;
        }
    };
    std::string origin, token, place, activity, reason;
    std::uint64_t serial = 0, epoch = 0, ends = 0;
    std::uint32_t guild = 0, human = 0;
    std::map<std::uint32_t, Member> members;
    std::map<std::string, Step> steps;
    Place destination;
    bool unresolved = false;
    bool LocationMatches(Place const& current) const
    {
        return destination.name == current.name && destination.kind == current.kind && destination.map == current.map &&
               destination.zone == current.zone && destination.x == current.x && destination.y == current.y &&
               destination.z == current.z && destination.o == current.o;
    }
    bool Covers(std::uint32_t guid) const
    {
        return members.contains(guid);
    }
    bool Owns(std::uint32_t guid, std::string const& holder, std::optional<BodyIdentity> const& current,
              std::uint64_t generation, std::uint64_t activeEpoch) const
    {
        auto member = members.find(guid);
        if (member == members.end() || !current || epoch != activeEpoch || member->second.generation != generation ||
            !SameLife(member->second.life, *current))
            return false;
        if (holder.empty())
            return true;
        auto found = steps.find(holder);
        return found != steps.end() && !found->second.released &&
               std::find(found->second.members.begin(), found->second.members.end(), guid) !=
                   found->second.members.end();
    }
    bool Released() const
    {
        return !reason.empty() &&
               std::all_of(steps.begin(), steps.end(), [](auto const& it) { return it.second.released; });
    }
    bool Allows(std::string const& kind, std::vector<std::uint32_t> const& guids) const
    {
        if (!reason.empty() || unresolved || guids.empty() ||
            std::any_of(guids.begin(), guids.end(), [&](auto g) { return !Covers(g); }))
            return false;
        auto previous = [&](std::string_view wanted, std::uint32_t g, bool proved) {
            return std::any_of(steps.begin(), steps.end(), [&](auto const& it) {
                auto const& s = it.second;
                return s.kind == wanted && (!proved || s.succeeded) &&
                       std::find(s.members.begin(), s.members.end(), g) != s.members.end();
            });
        };
        if (kind == "realm_move")
            return guids.size() == 1 && !previous(kind, guids[0], false);
        for (auto const& [g, body] : members)
            if (!previous("realm_move", g, true))
                return false;
        if (kind == "duel")
            return activity == "duel_practice" && guids.size() == 2 && guids[0] != guids[1] &&
                   !previous(kind, guids[0], false);
        if (guids.size() != 1 || previous(kind, guids[0], false))
            return false;
        if (activity == "fishing")
            return kind == "guild_fishing";
        return kind == "rpg_status" && activity != "duel_practice";
    }
};
inline bool DayFishingCleaning(ContinuationOwner const& owner, std::uint64_t guildEpoch)
{
    return owner.domain == ActivityDomain::Guild && owner.kind == PvpKind::Fishing && owner.originEpoch == guildEpoch &&
           owner.stage == PvpStage::Cleaning && owner.internalCleanup;
}

inline bool DayRequestMatches(nlohmann::json const& original, nlohmann::json const& candidate,
                              std::uint64_t serial, std::uint64_t currentSerial, std::int64_t deadline,
                              std::int64_t now, bool consumed)
{
    return !consumed && serial == currentSerial && now < deadline && original == candidate;
}
// A cold-driver ticket can only authorize the same check binding and exact permit set.
inline bool DayDriverTicketMatches(nlohmann::json const& issued, nlohmann::json const& presented,
                                   std::set<std::string> const& issuedTokens, std::set<std::string> const& tokens)
{
    return !issued.is_null() && issued == presented && issuedTokens == tokens;
}
enum class DayHandoff { Waiting, Ready, Unresolved };
inline DayHandoff GuildAddDayProgress(std::vector<DayPermit const*> const& permits, std::int64_t now,
                                      std::int64_t deadline)
{
    if (now >= deadline || permits.empty())
        return DayHandoff::Unresolved;
    bool waiting = false;
    for (auto const* permit : permits)
    {
        if (!permit || (permit->unresolved && !permit->Released()))
            return DayHandoff::Unresolved;
        if (!permit->Released())
            waiting = true;
    }
    return waiting ? DayHandoff::Waiting : DayHandoff::Ready;
}

inline nlohmann::json DayStateMessage(DayPermit const& p, std::int64_t started)
{
    std::vector<std::uint32_t> members;
    std::vector<std::string> released, succeeded;
    for (auto const& [guid, lease] : p.members)
        members.push_back(guid);
    for (auto const& [id, step] : p.steps)
    {
        if (step.released)
            released.push_back(id);
        if (step.succeeded)
            succeeded.push_back(id);
    }
    return {{"type", "day_state"},
            {"origin_id", p.origin},
            {"permit", p.token},
            {"serial", p.serial},
            {"guild", p.guild},
            {"human", p.human},
            {"started_ms", started},
            {"members", members},
            {"reason", p.reason},
            {"state", p.Released()       ? "closed"
                      : p.unresolved     ? "unresolved"
                      : p.reason.empty() ? "open"
                                         : "revoking"},
            {"released", released},
            {"succeeded", succeeded}};
}

inline std::string_view DayStatusFor(std::string_view activity)
{
    if (activity == "city" || activity == "holiday" || activity == "questing")
        return "wander npc";
    if (activity == "inn")
        return "rest";
    if (activity == "camp")
        return "idle";
    if (activity == "gathering")
        return "do gather";
    return {};
}
inline bool DayPlaceMatches(std::string_view activity, std::string_view kind)
{
    if (activity == "duel_practice")
        return kind == "duel";
    if (activity == "gathering")
        return kind == "herb" || kind == "ore";
    if (activity == "fishing")
        return kind == "fish";
    if (activity == "questing")
        return kind == "camp";
    return (activity == "city" || activity == "inn" || activity == "camp" || activity == "holiday") && activity == kind;
}
} // namespace Leeroy::Realm
#endif
