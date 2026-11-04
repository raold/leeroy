// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyPinSet.h"
#include <mutex>
#include <utility>

namespace Leeroy
{
void PinSet::Set(std::string_view owner, std::unordered_set<std::uint32_t> guids, std::uint8_t flags)
{
    if (owner.empty()) return;
    std::unique_lock lock(_mutex);
    if (guids.empty()) _owners.erase(std::string(owner));
    else _owners.insert_or_assign(std::string(owner), Entry{std::move(guids), flags});
}
std::unordered_set<std::uint32_t> PinSet::Union(std::unordered_set<std::uint32_t> const& cast) const
{
    auto result = cast;
    std::shared_lock lock(_mutex);
    for (auto const& [owner, entry] : _owners) result.insert(entry.guids.begin(), entry.guids.end());
    return result;
}
std::unordered_set<std::uint32_t> PinSet::Owned(std::string_view owner) const
{
    std::shared_lock lock(_mutex);
    auto it = _owners.find(owner);
    return it == _owners.end() ? std::unordered_set<std::uint32_t>{} : it->second.guids;
}
bool PinSet::Flag(std::uint32_t guid, std::uint8_t flag) const
{
    std::shared_lock lock(_mutex);
    for (auto const& [owner, entry] : _owners)
        if ((entry.flags & flag) && entry.guids.contains(guid)) return true;
    return false;
}
bool PinSet::Muted(std::uint32_t guid) const { return Flag(guid, kPinMute); }
bool PinSet::Watched(std::uint32_t guid) const { return Flag(guid, kPinWatch); }
PinSet& Pins() { static PinSet pins; return pins; }
}
void LeeroySetExtraPins(std::string_view owner, std::unordered_set<std::uint32_t> guids, std::uint8_t flags)
{
    Leeroy::Pins().Set(owner, std::move(guids), flags);
}
