// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyGuildAddPolicy.h"

#include <algorithm>
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

    GuildAddCounts needed = *composition;
    std::map<std::uint8_t, std::uint32_t> classes;
    for (auto const& member : connected)
    {
        auto& slots = needed[Index(member.role)];
        if (slots)
            --slots;
        ++classes[member.cls];
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
    std::vector<bool> taken(candidates.size());
    for (std::size_t role = 0; role < kGuildAddRoles; ++role)
        for (std::uint32_t slot = 0; slot < plan.open[role]; ++slot)
        {
            std::size_t best = candidates.size();
            for (std::size_t i = 0; i < candidates.size(); ++i)
                if (!taken[i] && candidates[i].guid && Index(candidates[i].role) == role
                    && (best == candidates.size() || classes[candidates[i].cls] < classes[candidates[best].cls]))
                    best = i;
            if (best == candidates.size())
                break;
            taken[best] = true;
            ++classes[candidates[best].cls];
            plan.picks.push_back({candidates[best].guid, candidates[best].role});
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
