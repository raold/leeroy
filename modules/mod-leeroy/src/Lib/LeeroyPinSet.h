// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_PIN_SET_H
#define MOD_LEEROY_PIN_SET_H
#include <cstdint>
#include <map>
#include <shared_mutex>
#include <string>
#include <string_view>
#include <unordered_set>

namespace Leeroy
{
inline constexpr std::uint8_t kPinNone = 0, kPinMute = 1, kPinWatch = 2;
inline constexpr std::string_view kPinOwnerTraining = "training", kPinOwnerBenchmark = "benchmark";

// Reads also run on chat/map threads. This owns contributions, never Player pointers or the upstream pin set.
class PinSet
{
public:
    void Set(std::string_view owner, std::unordered_set<std::uint32_t> guids, std::uint8_t flags = kPinNone);
    std::unordered_set<std::uint32_t> Union(std::unordered_set<std::uint32_t> const& cast) const;
    std::unordered_set<std::uint32_t> Owned(std::string_view owner) const;
    bool Muted(std::uint32_t guid) const;
    bool Watched(std::uint32_t guid) const;
private:
    struct Entry { std::unordered_set<std::uint32_t> guids; std::uint8_t flags; };
    bool Flag(std::uint32_t guid, std::uint8_t flag) const;
    mutable std::shared_mutex _mutex;
    std::map<std::string, Entry, std::less<>> _owners;
};
PinSet& Pins();
struct PinPlan
{
    std::unordered_set<std::uint32_t> guids;
    bool keep = false, online = false;
};
// Owner contributions are explicit cleanup obligations even when disabled.
// Upstream flags apply to the whole set, so do not extend owner retention to
// ordinary cast members when their keep flag has been turned off.
PinPlan PlanPins(PinSet const& owners, std::unordered_set<std::uint32_t> const& cast,
    bool enabled, bool keepCast, bool bringOnline);
inline bool SpeakerSuppressed(bool moduleEnabled, bool excludeCast, bool isCast, bool ownerMuted)
{
    return ownerMuted || (moduleEnabled && excludeCast && isCast);
}
}

// Set only. The world-thread caller subsequently invokes LeeroyApplyPins, the sole upstream composer.
void LeeroySetExtraPins(std::string_view owner, std::unordered_set<std::uint32_t> guids,
    std::uint8_t flags = Leeroy::kPinNone);
#endif
