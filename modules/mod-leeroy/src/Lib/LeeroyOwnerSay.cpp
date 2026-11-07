// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyOwnerSay.h"
#include "LeeroyProtocol.h"
#include "LeeroyText.h"

#include <algorithm>
#include <array>
#include <cstdint>
#include <limits>

namespace Leeroy::Guild
{
namespace
{
using json = nlohmann::json;

// Say, yell, emote and raid warning are never sent from the console.
constexpr std::array<std::string_view, 5> kOwnerSayChannels = {"guild", "officer", "party", "raid", "whisper"};

json const* Find(json const& msg, char const* key)
{
    if (!msg.is_object())
        return nullptr;
    auto const it = msg.find(key);
    return it == msg.end() ? nullptr : &*it;
}
}

std::optional<OwnerSayCmd> ParseOwnerSay(json const& msg, std::string& error)
{
    error.clear();
    auto const fail = [&](char const* key)
    {
        error = std::string(Err::BadField) + key;
        return std::nullopt;
    };

    OwnerSayCmd cmd;
    json const* channel = Find(msg, "channel");
    if (!channel || !channel->is_string()
        || std::find(kOwnerSayChannels.begin(), kOwnerSayChannels.end(), channel->get_ref<std::string const&>())
            == kOwnerSayChannels.end())
        return fail("channel");
    cmd.channel = channel->get<std::string>();

    json const* text = Find(msg, "text");
    if (!text || !text->is_string())
        return fail("text");
    cmd.text = text->get<std::string>();

    // A whisper needs its target; every other channel has none (absent or 0).
    bool const whisper = cmd.channel == "whisper";
    if (json const* to = Find(msg, "to_guid"))
    {
        if (!to->is_number_unsigned() || to->get<std::uint64_t>() > std::numeric_limits<std::uint32_t>::max())
            return fail("to_guid");
        cmd.toGuid = to->get<std::uint32_t>();
    }
    if (whisper != (cmd.toGuid != 0))
        return fail("to_guid");
    return cmd;
}

std::pair<std::string_view, std::string> OwnerSayText(std::string_view raw)
{
    std::string text = SanitizeChatText(raw, std::numeric_limits<std::size_t>::max());
    if (text.empty())
        return {"empty_text", {}};
    if (text.size() > kMaxChatBytes)
        return {"too_long", {}};
    if (StartsWithCommandPrefix(text))
        return {"command_prefix", {}};
    return {{}, std::move(text)};
}

bool OwnerSayLimiter::Allows(std::uint64_t nowMs) const
{
    if (_sent.empty())
        return true;
    if (nowMs < _sent.back() + kOwnerSayGapMs)
        return false;
    auto const counted = std::count_if(_sent.begin(), _sent.end(),
        [nowMs](std::uint64_t t) { return nowMs < t + kOwnerSayWindowMs; });
    return static_cast<std::size_t>(counted) < kOwnerSayPerWindow;
}

void OwnerSayLimiter::Record(std::uint64_t nowMs)
{
    _sent.push_back(nowMs);
    while (!_sent.empty() && nowMs >= _sent.front() + kOwnerSayWindowMs)
        _sent.pop_front();
    while (_sent.size() > kOwnerSayPerWindow)
        _sent.pop_front();
}

OwnerSayVerdict CheckOwnerSay(OwnerSayCmd const& cmd, OwnerSayFacts const& facts, OwnerSayLimiter const& limiter,
    std::uint64_t nowMs)
{
    auto [error, text] = OwnerSayText(cmd.text);
    if (!error.empty())
        return {error, {}};

    if ((cmd.channel == "guild" || cmd.channel == "officer") && !facts.inGuild)
        return {"not_in_guild", {}};
    if (cmd.channel == "party" && !facts.inGroup)
        return {"not_in_group", {}};
    if (cmd.channel == "raid" && !facts.inRaid)
        return {"not_in_raid", {}};

    if (cmd.channel == "whisper")
    {
        if (cmd.toGuid == facts.humanGuid)
            return {"self", {}};
        if (!facts.targetCast)
            return {"not_cast", {}};
        if (!facts.targetOnline)
            return {"target_offline", {}};
    }

    if (!limiter.Allows(nowMs))
        return {"rate_limited", {}};
    return {{}, std::move(text)};
}
}
