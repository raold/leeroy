// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyCastModel.h"
#include "LeeroyProtocol.h"
#include <algorithm>
#include <utility>

namespace Leeroy
{
bool IsCastEligible(std::uint32_t guid, std::uint8_t level, std::uint32_t humanGuid)
{
    return level >= kCastLevel && guid != humanGuid;
}

nlohmann::json ToJson(CastMember const& m)
{
    nlohmann::json j = nlohmann::json::object();
    j["guid"] = m.guid;
    j["name"] = m.name;
    j["race"] = m.race;
    j["class"] = m.cls;
    j["gender"] = m.gender;
    j["level"] = m.level;
    j["rank"] = m.rank;
    j["online"] = m.online;
    j["map"] = m.map;
    j["zone"] = m.zone;
    j["spec_tab"] = m.specTab;
    return j;
}

std::string EncodeCast(std::int64_t ts, std::vector<CastMember> members)
{
    std::sort(members.begin(), members.end(),
        [](CastMember const& a, CastMember const& b) { return a.guid < b.guid; });
    nlohmann::json list = nlohmann::json::array();
    for (CastMember const& m : members)
        list.push_back(ToJson(m));
    nlohmann::json j = nlohmann::json::object();
    j["v"] = kProtocolVersion;
    j["type"] = "cast";
    j["ts"] = ts;
    j["members"] = std::move(list);
    return DumpLine(j);
}
}
