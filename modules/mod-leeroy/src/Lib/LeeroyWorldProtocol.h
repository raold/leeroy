// This file is part of leeroy, released under the GNU GPL v2.
#ifndef LEEROY_WORLD_PROTOCOL_H
#define LEEROY_WORLD_PROTOCOL_H
#include "LeeroyWorldPolicy.h"
#include <nlohmann/json.hpp>
#include <optional>
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
inline bool ValidMember(nlohmann::json const& m, std::uint32_t own, std::uint32_t human)
{
    static std::set<std::string> const keys{"guid","account","guild_id","guild_name","name","race","class","gender","level","rank","online","map","zone"};
    if (!m.is_object() || m.size() != keys.size()) return false;
    for (auto const& key : keys) if (!m.contains(key)) return false;
    for (auto key : {"guid","account","guild_id"}) if (!Number(m[key],1,UINT32_MAX)) return false;
    if (m["guid"] == human || m["guild_id"] == own || !m["online"].is_boolean()) return false;
    for (auto key : {"map","zone"}) if (!Number(m[key],0,UINT32_MAX)) return false;
    if (!Number(m["race"],1,11) || !Number(m["class"],1,11) || !Number(m["gender"],0,1)
        || !Number(m["level"],1,255) || !Number(m["rank"],0,9)) return false;
    for (auto key : {"name","guild_name"})
    {
        if (!m[key].is_string()) return false;
        auto s = m[key].get<std::string>();
        auto chars = std::count_if(s.begin(),s.end(),[](unsigned char c) { return (c & 0xc0) != 0x80; });
        if (s.empty() || chars > (std::string_view(key) == "name" ? 24 : 48)
            || std::any_of(s.begin(),s.end(),[](unsigned char c) { return c < 32 || c == 127; })) return false;
    }
    return true;
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
}
#endif
