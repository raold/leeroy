// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyMemberDungeon.h"
#include <algorithm>
#include <cmath>
#include <set>

namespace Leeroy::Guild
{
std::string MemberDungeonAdmission(std::uint32_t map, std::uint32_t leader,
    std::vector<DungeonCandidate> const& candidates, bool ownerOnline, bool slotBusy)
{
    if (map != MemberDungeonMap) return "unsupported_dungeon";
    if (ownerOnline) return "human_online";
    if (slotBusy) return "job_busy";
    if (candidates.size() != 5) return "wrong_composition";
    std::set<std::uint32_t> distinct;
    GuildAddCounts counts{};
    bool tankLeads = false;
    for (auto const& member : candidates)
    {
        if (!member.refusal.empty()) return member.refusal;
        if (!member.guid || !distinct.insert(member.guid).second) return "wrong_composition";
        ++counts.at(static_cast<std::size_t>(member.role));
        if (member.guid == leader) tankLeads = member.role == GuildAddRole::Tank;
    }
    return tankLeads && counts == *GuildAddComposition(5) ? "" : "wrong_composition";
}

bool ValidDungeonReceipt(nlohmann::json const& data)
{
    try
    {
        if (data.at("version") != 1 || data.at("map") != MemberDungeonMap || data.at("difficulty") != 1 ||
            !data.at("stage").is_string() || !data.at("slot").is_boolean() || !data.at("settled").is_boolean() ||
            !data.at("guild").get<std::uint32_t>() || !data.at("human").get<std::uint32_t>() ||
            !(data.at("terminal").is_null() || data.at("terminal").is_object())) return false;
        auto roster = data.at("roster").get<std::vector<std::uint32_t>>();
        std::set<std::uint32_t> ids(roster.begin(),roster.end());
        if (roster.size() != 5 || ids.size() != 5 || ids.contains(0) || data.at("leader") != roster.front()) return false;
        auto const& bodies = data.at("bodies");
        if (!bodies.is_array() || (!bodies.empty() && bodies.size() != 5)) return false;
        for (auto const& body : bodies)
        {
            if (!ids.erase(body.at("guid").get<std::uint32_t>()) || !body.at("leader").is_boolean() ||
                !body.at("changed").is_boolean() || !body.at("moved").is_boolean() || !body.at("returned").is_boolean() ||
                !body.at("abandoned").is_boolean() || !body.at("map").is_number_unsigned() ||
                body.at("dungeon").get<unsigned>() > 1 || body.at("raid").get<unsigned>() > 3) return false;
            for (auto field : {"x","y","z","o"})
                if (!body.at(field).is_number() || !std::isfinite(body.at(field).get<double>())) return false;
        }
        return true;
    }
    catch (nlohmann::json::exception const&) { return false; }
}

bool DungeonBeforeMutation(nlohmann::json const& data)
{
    auto stage = data.value("stage",std::string{});
    return data.at("bodies").empty() && !data.value("group",0u) && !data.value("instance",0u) &&
        (stage == "accepted" || (stage == "grouping" && data.value("effect",std::string{}) == "observe"));
}

bool MemberDungeonPreempts(bool admitted, std::vector<std::uint32_t> const& requested,
    std::vector<std::uint32_t> const& participants)
{
    return admitted && std::any_of(requested.begin(), requested.end(), [&](auto guid) {
        return std::find(participants.begin(), participants.end(), guid) != participants.end();
    });
}
}
