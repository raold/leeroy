// This file is part of leeroy (mod-leeroy), released under GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_SOCIAL_SIT_H
#define MOD_LEEROY_SOCIAL_SIT_H
#include <cstdint>
#include <string>
#include <unordered_map>
namespace Leeroy::Realm
{
struct SocialSeat { std::string token; std::uint64_t until; std::uint32_t guild, human; };
class SocialSeats
{
public:
    bool Begin(std::uint32_t guid, std::string token, std::uint64_t now, std::uint32_t seconds,
               std::uint32_t guild, std::uint32_t human)
    {
        if (!guid || token.empty() || !guild || !human || seconds == 0 || seconds > 900 || _seats.contains(guid)) return false;
        _seats.emplace(guid, SocialSeat{std::move(token), now + seconds * 1000ULL, guild, human});
        return true;
    }
    bool End(std::uint32_t guid, std::string const& token)
    {
        auto it = _seats.find(guid);
        if (it == _seats.end() || it->second.token != token) return false;
        _seats.erase(it);
        return true;
    }
    bool Current(SocialSeat const& seat, std::uint64_t now, bool ready, std::uint32_t guild, std::uint32_t human) const
    { return ready && now < seat.until && guild == seat.guild && human == seat.human; }
    auto const& Entries() const { return _seats; }
private:
    std::unordered_map<std::uint32_t, SocialSeat> _seats;
};
}
#endif
