// This file is part of leeroy, released under the GNU GPL v2.
#ifndef LEEROY_WORLD_PROTOCOL_H
#define LEEROY_WORLD_PROTOCOL_H
#include "LeeroyWorldPolicy.h"
#include <nlohmann/json.hpp>
#include <optional>
#include <string_view>
#include <utility>
#include <vector>
namespace Leeroy::World
{
inline bool Token(nlohmann::json const& value)
{
    if (!value.is_string()) return false;
    auto const& s = value.get_ref<std::string const&>();
    return !s.empty() && s.size() <= 128 && std::all_of(s.begin(),s.end(),[](char c) {
        return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_' || c == '-';
    });
}
inline std::optional<std::string> RosterRequest(nlohmann::json const& message, std::string& error)
{
    error.clear();
    if (!message.contains("request") || !Token(message.at("request")))
    { error = "bad field: request"; return std::nullopt; }
    return message.at("request").get<std::string>();
}
inline std::size_t PageCount(std::size_t total) { return std::max<std::size_t>(1,(total+kPageSize-1)/kPageSize); }
inline bool Number(nlohmann::json const& value, std::uint64_t lo, std::uint64_t hi)
{
    return value.is_number_integer() && !value.is_boolean() && value >= lo && value <= hi;
}
// The first rule a roster row breaks, by name ("" when it is valid): a row native cannot vouch for.
inline std::string_view MemberProblem(nlohmann::json const& m, std::uint32_t own, std::uint32_t human)
{
    static std::set<std::string> const keys{"guid","account","guild_id","guild_name","name","race","class","gender","level","rank","online","map","zone"};
    if (!m.is_object() || m.size() != keys.size()) return "shape";
    for (auto const& key : keys) if (!m.contains(key)) return "shape";
    for (std::string_view key : {"guid","account","guild_id"}) if (!Number(m[std::string(key)],1,UINT32_MAX)) return key;
    if (m["guid"] == human) return "human";
    if (m["guild_id"] == own) return "own_guild";
    if (!m["online"].is_boolean()) return "online";
    for (std::string_view key : {"map","zone"}) if (!Number(m[std::string(key)],0,UINT32_MAX)) return key;
    if (!Number(m["race"],1,11)) return "race";
    if (!Number(m["class"],1,11)) return "class";
    if (!Number(m["gender"],0,1)) return "gender";
    if (!Number(m["level"],1,255)) return "level";
    if (!Number(m["rank"],0,9)) return "rank";
    for (std::string_view key : {"name","guild_name"})
    {
        auto const& value = m[std::string(key)];
        if (!value.is_string()) return key;
        auto const& s = value.get_ref<std::string const&>();
        auto chars = std::count_if(s.begin(),s.end(),[](unsigned char c) { return (c & 0xc0) != 0x80; });
        if (s.empty() || chars > (key == "name" ? 24 : 48)
            || std::any_of(s.begin(),s.end(),[](unsigned char c) { return c < 32 || c == 127; })) return key;
    }
    return {};
}
inline bool ValidMember(nlohmann::json const& m, std::uint32_t own, std::uint32_t human)
{
    return MemberProblem(m,own,human).empty();
}
// The rows a roster can publish, in order, and the ones left out with why: one odd member (a
// value native's own caches disagree on) must not withhold the whole world from the mind.
struct RosterRows
{
    nlohmann::json rows = nlohmann::json::array();
    std::vector<std::pair<std::uint32_t,std::string>> skipped;  // guid (0 when unreadable), rule
};
inline RosterRows ValidRows(nlohmann::json const& rows, std::uint32_t own, std::uint32_t human)
{
    RosterRows out;
    if (!rows.is_array()) return out;
    std::set<std::uint32_t> seen;
    for (auto const& m : rows)
    {
        std::uint32_t guid = 0;
        if (m.is_object() && m.contains("guid") && Number(m["guid"],1,UINT32_MAX)) guid = m["guid"].get<std::uint32_t>();
        if (auto problem = MemberProblem(m,own,human); !problem.empty()) { out.skipped.emplace_back(guid,std::string(problem)); continue; }
        if (!seen.insert(guid).second) { out.skipped.emplace_back(guid,"duplicate"); continue; }
        out.rows.push_back(m);
    }
    return out;
}
inline std::optional<nlohmann::json> RosterPage(std::string const& request, std::uint32_t own,
    std::uint32_t human, std::size_t page, std::size_t total, nlohmann::json const& members)
{
    auto pages = PageCount(total);
    if (!Token(request) || !own || !human || total > kMaxMembers || page >= pages || !members.is_array()
        || members.size() != std::min(kPageSize,total-page*kPageSize)) return std::nullopt;
    std::set<std::uint32_t> seen;
    for (auto const& m : members)
        if (!ValidMember(m,own,human) || !seen.insert(m["guid"].get<std::uint32_t>()).second) return std::nullopt;
    return nlohmann::json{{"type","world_roster"},{"request",request},{"own_guild",own},{"human",human},
        {"page",page},{"pages",pages},{"total",total},{"members",members}};
}

inline std::optional<std::vector<nlohmann::json>> RosterPages(std::string const& request,
    std::uint32_t own, std::uint32_t human, nlohmann::json const& rows)
{
    if (!rows.is_array() || rows.size() > kMaxMembers) return std::nullopt;
    std::vector<nlohmann::json> result;
    std::set<std::uint32_t> seen;
    for (std::size_t page=0; page<PageCount(rows.size()); ++page)
    {
        auto values = nlohmann::json::array();
        for (std::size_t i=page*kPageSize; i<std::min(rows.size(),(page+1)*kPageSize); ++i)
        {
            if (!ValidMember(rows[i],own,human)
                || !seen.insert(rows[i]["guid"].get<std::uint32_t>()).second) return std::nullopt;
            values.push_back(rows[i]);
        }
        auto body = RosterPage(request,own,human,page,rows.size(),values);
        if (!body) return std::nullopt;
        result.push_back(std::move(*body));
    }
    return result;  // Nothing is publishable until every page has passed validation.
}
}
#endif
