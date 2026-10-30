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
    std::uint32_t guid;
    std::string name;
    std::uint8_t race, cls, gender, level, rank;
    bool online;
    std::int32_t map;      // -1 when offline
    std::uint32_t zone;
    std::int32_t specTab;  // -1 when offline
};

bool IsCastEligible(std::uint32_t guid, std::uint8_t level, std::uint32_t humanGuid);
nlohmann::json ToJson(CastMember const& m);
std::string EncodeCast(std::int64_t ts, std::vector<CastMember> members);   // sorted by guid
}

#endif
