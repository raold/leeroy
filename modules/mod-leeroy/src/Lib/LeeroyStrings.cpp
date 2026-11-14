// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyStrings.h"
#include "LeeroyProtocol.h"

#include <algorithm>
#include <limits>
#include <utility>

namespace Leeroy::Guild
{
namespace
{
using json = nlohmann::json;

constexpr std::array<std::pair<std::string_view, StringKind>, 6> kKinds = {{
    {"come_to_me", StringKind::ComeToMe}, {"join_group", StringKind::JoinGroup}, {"follow", StringKind::Follow},
    {"duel_me", StringKind::DuelMe}, {"emote", StringKind::Emote}, {"sit", StringKind::Sit}}};

json const* Find(json const& msg, char const* key)
{
    if (!msg.is_object())
        return nullptr;
    auto const it = msg.find(key);
    return it == msg.end() ? nullptr : &*it;
}

bool Guid(json const* value, bool zero)
{
    return value && value->is_number_unsigned() && value->get<std::uint64_t>() <= std::numeric_limits<std::uint32_t>::max()
        && (zero || value->get<std::uint64_t>() != 0);
}

// Where the core refuses a duel: an area without the duel flag, or a sanctuary.
bool NoDuel(std::uint32_t area) { return !(area & kAreaAllowDuels) || (area & kAreaSanctuary); }

std::string KindRefusal(StringKind kind, StringFacts const& f)
{
    switch (kind)
    {
        case StringKind::ComeToMe:
            if (f.otherGroup) return "grouped";
            if (!f.humanResting && !((f.humanArea | f.humanZone) & kStringGatherAreas)) return "bad_area";
            return {};
        case StringKind::JoinGroup:
            if (f.hisGroup) return "in_group";
            if (f.groupSpecial) return "lfg_group";
            if (!f.leader) return "not_leader";
            if (f.groupRaid) return "in_raid";
            if (f.groupMembers >= kStringPartySize) return "group_full";
            return f.joinRefusal;
        case StringKind::Follow:
            if (!f.hisGroup) return "not_in_group";
            if (f.groupRaid) return "busy:raid_owner";
            return {};
        case StringKind::DuelMe:
            if (f.hisGroup) return "in_group";
            if (f.duel || f.humanDuel) return "busy:duel";
            if (!f.sameMap || !f.sameZone || f.distance > kStringDuelYards) return "too_far";
            if (NoDuel(f.area) || NoDuel(f.humanArea)) return "bad_area";
            return {};
        case StringKind::Emote:
            if (!StringEmoteAllowed(f.emote)) return "bad_emote";
            if (!f.sameMap || f.distance > kStringEmoteYards) return "too_far";
            return {};
        case StringKind::Sit:
            if (!f.sameMap || f.distance > kStringSitYards) return "too_far";
            return {};
    }
    return "bad_kind";
}
}

std::optional<StringKind> ParseStringKind(std::string_view name)
{
    for (auto const& [text, kind] : kKinds)
        if (text == name)
            return kind;
    return std::nullopt;
}

std::string_view StringKindName(StringKind kind)
{
    for (auto const& [text, value] : kKinds)
        if (value == kind)
            return text;
    return {};
}

bool StringEmoteAllowed(std::string_view name)
{
    return std::find(kStringEmotes.begin(), kStringEmotes.end(), name) != kStringEmotes.end();
}

std::optional<StringDoCmd> ParseStringDo(json const& msg, std::string& error)
{
    error.clear();
    auto const fail = [&](char const* key)
    {
        error = std::string(Err::BadField) + key;
        return std::nullopt;
    };
    StringDoCmd cmd;
    json const* guid = Find(msg, "guid");
    if (!Guid(guid, false))
        return fail("guid");
    cmd.guid = guid->get<std::uint32_t>();
    json const* kind = Find(msg, "kind");
    if (!kind || !kind->is_string())
        return fail("kind");
    auto const parsed = ParseStringKind(kind->get_ref<std::string const&>());
    if (!parsed)
        return fail("kind");
    cmd.kind = *parsed;
    if (json const* target = Find(msg, "target_guid"))
    {
        if (!Guid(target, true))
            return fail("target_guid");
        cmd.targetGuid = target->get<std::uint32_t>();
    }
    // The emote is named for the emote kind only, and only from the strings' own list.
    json const* emote = Find(msg, "emote");
    if (cmd.kind == StringKind::Emote)
    {
        if (!emote || !emote->is_string() || !StringEmoteAllowed(emote->get_ref<std::string const&>()))
            return fail("emote");
        cmd.emote = emote->get<std::string>();
    }
    else if (emote && !(emote->is_string() && emote->get_ref<std::string const&>().empty()))
        return fail("emote");
    return cmd;
}

std::string StringRefusal(StringKind kind, StringFacts const& f)
{
    if (!f.enabled) return "off";
    if (!f.humanOnline) return "human_offline";
    if (!f.cast) return "not_cast";
    if (!f.online) return "not_online";
    if (!f.fence.empty()) return f.fence;
    if (f.dead) return "dead";
    if (f.combat || f.humanCombat) return "combat";
    if (f.instance || f.humanInstance) return "in_instance";
    if (f.battleground || f.humanBattleground) return "in_bg";
    if (f.queued) return "queued";
    if (f.travelling || f.humanTravelling) return "travelling";
    if (!f.humanAlive) return "human_dead";
    return KindRefusal(kind, f);
}
}
