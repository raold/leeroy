// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_OWNER_SAY_H
#define MOD_LEEROY_OWNER_SAY_H

#include <cstddef>
#include <cstdint>
#include <deque>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <nlohmann/json.hpp>

namespace Leeroy::Guild
{
// The guild console speaking into WoW as the owner: one line queued on his own session, as if he had typed it. Not
// human_say, which push-to-talk uses unchanged; this one has its own guard and its own rate limit.
inline constexpr std::string_view kOwnerSayType = "owner_say";
// Under the core's chat-flood mute: at most one line per gap, and kOwnerSayPerWindow lines per window.
inline constexpr std::uint64_t kOwnerSayGapMs = 1500, kOwnerSayWindowMs = 30000;
inline constexpr std::size_t kOwnerSayPerWindow = 8;

// channel is guild, officer, party, raid or whisper; toGuid is non-zero for a whisper only.
struct OwnerSayCmd
{
    std::string channel, text;
    std::uint32_t toGuid = 0;
};

// Fields only: the bridge has already checked v, id and type. error is "bad field: <key>" on refusal.
std::optional<OwnerSayCmd> ParseOwnerSay(nlohmann::json const&, std::string& error);

// {"", text to send} or {refusal code, ""}: SanitizeChatText without a cap, then empty_text, too_long (> 255 bytes),
// command_prefix. A line is refused, never cut.
std::pair<std::string_view, std::string> OwnerSayText(std::string_view raw);

// What the world thread saw of him and the whisper target at the moment of the command.
struct OwnerSayFacts
{
    std::uint32_t humanGuid = 0;
    bool inGuild = false, inGroup = false, inRaid = false, targetCast = false, targetOnline = false;
};

// Allows: at least kOwnerSayGapMs since the last recorded line, and fewer than kOwnerSayPerWindow recorded lines
// with nowMs - t < kOwnerSayWindowMs. Record keeps only what can still count.
class OwnerSayLimiter
{
public:
    bool Allows(std::uint64_t nowMs) const;
    void Record(std::uint64_t nowMs);

private:
    std::deque<std::uint64_t> _sent;
};

struct OwnerSayVerdict
{
    std::string_view error;
    std::string text;
};

// Text first, then the channel's facts, then the whisper target, the limiter last; Check never records. error is
// empty and text the line to send when it passes, else error is the refusal code and text empty.
OwnerSayVerdict CheckOwnerSay(OwnerSayCmd const&, OwnerSayFacts const&, OwnerSayLimiter const&, std::uint64_t nowMs);
}

#endif
