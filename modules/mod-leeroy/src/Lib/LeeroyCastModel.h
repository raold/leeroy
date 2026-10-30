// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_LEEROY_LEEROY_CAST_MODEL_H
#define MOD_LEEROY_LEEROY_CAST_MODEL_H

#include <cstdint>
#include <string>
#include <vector>
#include <nlohmann/json.hpp>

namespace Leeroy
{
inline constexpr std::uint8_t kCastLevel = 80;

struct CastMember
{
    std::uint32_t guid = 0;
    std::string name;
    std::uint8_t race = 0, cls = 0, gender = 0, level = 0, rank = 0;
    bool online = false;
    std::int32_t map = -1;     // -1 when offline
    std::uint32_t zone = 0;
    std::int32_t specTab = -1; // -1 when offline
};

bool IsCastEligible(std::uint32_t guid, std::uint8_t level, std::uint32_t humanGuid);
nlohmann::json ToJson(CastMember const& m);
std::string EncodeCast(std::int64_t ts, std::vector<CastMember> members);   // sorted by guid
}

#endif
