// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyLead.h"
#include "LeeroyInviteSpots.h"
#include "LeeroyProtocol.h"

#include <array>
#include <limits>
#include <utility>

namespace Leeroy::Guild
{
namespace
{
using json = nlohmann::json;

constexpr std::array<std::pair<std::string_view, LeadActivity>, 3> kActivities = {{
    {"quest", LeadActivity::Quest}, {"grind", LeadActivity::Grind}, {"travel", LeadActivity::Travel}}};

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

bool EmptyText(json const* value) { return value->is_string() && value->get_ref<std::string const&>().empty(); }
}

std::optional<LeadActivity> ParseLeadActivity(std::string_view name)
{
    for (auto const& [text, activity] : kActivities)
        if (text == name)
            return activity;
    return std::nullopt;
}

std::string_view LeadActivityName(LeadActivity activity)
{
    for (auto const& [text, value] : kActivities)
        if (value == activity)
            return text;
    return {};
}

std::optional<LeadDoCmd> ParseLeadDo(json const& msg, std::string& error)
{
    error.clear();
    auto const fail = [&](char const* key)
    {
        error = std::string(Err::BadField) + key;
        return std::nullopt;
    };
    LeadDoCmd cmd;
    json const* action = Find(msg, "action");
    if (!action || !action->is_string())
        return fail("action");
    std::string const& name = action->get_ref<std::string const&>();
    if (name == "start")
        cmd.action = LeadAction::Start;
    else if (name == "end")
        cmd.action = LeadAction::End;
    else
        return fail("action");
    json const* guid = Find(msg, "guid");
    json const* activity = Find(msg, "activity");
    json const* spot = Find(msg, "spot");
    if (cmd.action == LeadAction::End)
    {
        // An end names at most whom it ends (0 or absent: whoever leads), never what or where.
        if (guid && !Guid(guid, true))
            return fail("guid");
        cmd.guid = guid ? guid->get<std::uint32_t>() : 0;
        if (activity && !EmptyText(activity))
            return fail("activity");
        if (spot && !EmptyText(spot))
            return fail("spot");
        return cmd;
    }
    if (!Guid(guid, false))
        return fail("guid");
    cmd.guid = guid->get<std::uint32_t>();
    if (!activity || !activity->is_string())
        return fail("activity");
    auto const parsed = ParseLeadActivity(activity->get_ref<std::string const&>());
    if (!parsed)
        return fail("activity");
    cmd.activity = *parsed;
    // The spot is named for travel only, from the meeting spots' own list.
    if (cmd.activity == LeadActivity::Travel)
    {
        if (!spot || !spot->is_string() || !FindInviteSpot(spot->get_ref<std::string const&>()))
            return fail("spot");
        cmd.spot = spot->get<std::string>();
    }
    else if (spot && !EmptyText(spot))
        return fail("spot");
    return cmd;
}

std::string LeadRefusal(LeadActivity activity, LeadFacts const& f)
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
    if (!f.hisGroup) return "not_in_group";
    if (f.groupSpecial) return "lfg_group";
    if (!f.leader) return "not_leader";
    if (!f.sameMap || f.distance > kLeadStartYards) return "too_far";
    if (!f.supported) return "unsupported";
    if (activity == LeadActivity::Travel)
    {
        if (!f.spotValid) return "bad_spot";
        if (!f.spotSameMap) return "other_map";
        if (f.spotDistance <= kLeadArriveYards) return "here";
    }
    return {};
}

LeadDecision DecideLead(LeadState const& s, LeadTickFacts const& f)
{
    auto const end = [](std::string_view why) { return LeadDecision{LeadStep::End, why}; };
    if (!f.leaderHere) return end("gone");
    if (!f.on) return end("off");
    if (!f.humanOnline) return end("logout");
    if (f.dead) return end("dead");
    if (f.instance) return end("instance");
    if (f.battleground) return end("pvp");
    if (!f.grouped) return end("group");
    if (!f.selfMastered) return end("taken");
    if (!s.paused && !f.activityOn) return end("reset");
    if (f.fenced) return end("fenced");
    if (f.teleported) return end("teleported");
    if (f.now >= s.until) return end("timeout");
    bool const travel = s.activity == LeadActivity::Travel;
    if (travel && f.spotDistance >= 0.0f && f.spotDistance <= kLeadArriveYards) return end("arrived");
    if (travel && !s.paused && !f.fighting && s.progressAt && f.now >= s.progressAt + kLeadStuckMs)
        return end("no_path");
    if (s.paused && s.behindSince && f.now >= s.behindSince + kLeadBehindMs) return end("left");
    if (f.fighting)
        return {};
    bool const behind = !f.sameMap || f.distance > kLeadWaitYards;
    if (!s.paused && behind)
        return {LeadStep::Pause, {}};
    if (s.paused && f.sameMap && f.distance <= kLeadResumeYards)
        return {LeadStep::Resume, {}};
    return {};
}

void NoteLead(LeadState& s, LeadTickFacts const& f)
{
    if (s.activity != LeadActivity::Travel)
        return;
    if (s.paused || f.fighting || f.spotDistance < 0.0f)
    {
        s.progressAt = f.now;   // a wait, a fight or another map is not her failing to find the way
        return;
    }
    if (!s.progressAt || f.spotDistance + kLeadProgressYards < s.best)
    {
        s.best = f.spotDistance;
        s.progressAt = f.now;
    }
}

void ApplyLead(LeadState& s, LeadStep step, std::uint64_t now)
{
    if (step == LeadStep::Pause)
    {
        s.paused = true;
        s.behindSince = now;
    }
    else if (step == LeadStep::Resume)
    {
        s.paused = false;
        s.behindSince = 0;
        s.progressAt = now;
    }
}

std::optional<LeadRpg> LeadSteer(LeadActivity activity, int status)
{
    auto const is = [status](LeadRpg rpg) { return status == static_cast<int>(rpg); };
    switch (activity)
    {
        case LeadActivity::Quest:
            if (is(LeadRpg::TravelFlight) || is(LeadRpg::OutdoorPvp))
                return LeadRpg::WanderRandom;
            return std::nullopt;
        case LeadActivity::Grind:
            if (is(LeadRpg::Idle) || is(LeadRpg::GoGrind) || is(LeadRpg::WanderRandom) || is(LeadRpg::Rest))
                return std::nullopt;
            return LeadRpg::WanderRandom;
        case LeadActivity::Travel:
            return is(LeadRpg::GoCamp) ? std::nullopt : std::optional<LeadRpg>(LeadRpg::GoCamp);
    }
    return std::nullopt;
}
}
