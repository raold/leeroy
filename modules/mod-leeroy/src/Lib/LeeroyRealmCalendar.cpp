// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmCalendar.h"
#include <array>
#include <iterator>
#include <limits>

namespace Leeroy::Realm
{
bool CalendarPacketHooksSupported(CalendarPacketBoundary boundary, std::span<std::string_view const> providers)
{
    if (providers.size() > kCalendarPacketProviders) return false;
    // No installed constructor enables CanPacketSend, including constructors
    // whose classes inherit the default method. Empty is the supported set.
    if (boundary == CalendarPacketBoundary::CanSend) return providers.empty();
    if (boundary != CalendarPacketBoundary::Sent) return false;
    constexpr std::array<std::string_view, 5> supported = {"PlayerbotsServerScript",
        "DungeonClearQueueFillProposalScript", "leeroy_realm_speech_receipt",
        "LeeroyRealmPvpPackets", "LeeroyRealmLfgReceiptPackets"};
    for (std::size_t i = 0; i < providers.size(); ++i)
    {
        bool known = false;
        for (auto name : supported) if (providers[i] == name) { known = true; break; }
        if (!known) return false;
        for (std::size_t earlier = 0; earlier < i; ++earlier)
            if (providers[earlier] == providers[i]) return false;
    }
    return true;
}
std::string CalendarAuthorityReason(CalendarAuthorityFacts const& f)
{
    return !f.guild || !f.human || !f.creator || f.creator == f.human || !f.cast || !f.rosterMember
        || f.creatorGuild != f.guild || f.announcement || f.eventGuild != (f.guildEvent ? f.guild : 0)
        ? "no_event" : "";
}
CalendarMemberVerdict CalendarMemberAdmission(CalendarMemberFacts const& f)
{
    if (!f.realmOn) return {"realm_off", {}};
    if (!f.guid || !f.guild || f.guid == f.human || !f.cast || !f.character || !f.body || !f.rosterMember
        || f.characterGuild != f.guild || f.bodyGuild != f.guild
        || f.characterAccount != f.bodyAccount || f.rosterAccount != f.bodyAccount)
        return {"no_event", {}};
    if (!f.randomBotAccount) return {"not_bot", {}};
    return {{}, f.bodyFence};
}
std::string CalendarCapacityReason(std::size_t creatorEvents, std::size_t guildEvents)
{
    return creatorEvents >= kCalendarCreatorLimit || guildEvents >= kCalendarGuildLimit ? "calendar_full" : "";
}
std::string CalendarSignupReason(std::size_t invites, std::size_t matchingInvites)
{
    if (matchingInvites > 1) return "calendar_invite_ambiguous";
    return !matchingInvites && invites >= kCalendarSignupLimit ? "full" : "";
}
std::string CalendarTimeReason(std::uint64_t at, std::uint64_t now, bool allowKeep)
{
    if (!at && allowKeep) return {};
    // CalendarMgr writes uint32 eventtime to both SQL and the native client.
    if (at > std::numeric_limits<std::uint32_t>::max()) return "bad field: at";
    return at <= now ? "in_past" : "";
}
std::optional<std::uint8_t> CalendarStatus(std::string_view status)
{
    if (status == "signed_up") return 6;
    if (status == "tentative") return 8;
    if (status == "declined") return 2;
    if (status == "accepted") return 1;
    return std::nullopt;
}
std::string_view CalendarStatusName(std::uint8_t status)
{
    constexpr std::array<std::string_view, 10> names = {"invited", "accepted", "declined", "confirmed", "out",
        "standby", "signed_up", "not_signed_up", "tentative", "removed"};
    return status < names.size() ? names[status] : std::string_view{};
}
void CalendarRsvpTracker::Reset()
{
    _boundary.reset(); _sampling = false; _emissionCursor = _admissionCursor = _admissionSerial = 0;
    _previous.clear(); _sample.clear(); _candidates.clear();
}
bool CalendarRsvpTracker::Begin(CalendarPollBoundary const& boundary, bool enabled, bool online)
{
    _sample.clear(); _sampling = false;
    if (!enabled || !online || !boundary.guild || !boundary.human || !boundary.player || !boundary.session)
    { Reset(); return false; }
    if (!_boundary || *_boundary != boundary) Reset();
    _candidates = decltype(_candidates)(CircularOrder{_admissionCursor});
    _boundary = boundary; _sampling = true;
    return true;
}
void CalendarRsvpTracker::Observe(CalendarRsvpFact const& fact)
{
    if (!_sampling || !fact.invite || !fact.event || CalendarStatusName(fact.status).empty()) return;
    if (_previous.contains(fact.invite))
    {
        auto [it, inserted] = _sample.emplace(fact.invite, fact);
        if (!inserted && it->second != fact) it->second.event = 0;
        return;
    }
    auto const cap = _previous.empty() ? kCalendarObservationLimit : kCalendarAdmissionLimit;
    if (_candidates.size() == cap && _candidates.key_comp()(_candidates.rbegin()->first, fact.invite)) return;
    auto [it, inserted] = _candidates.emplace(fact.invite, fact);
    if (!inserted && it->second != fact) it->second.event = 0;
    if (_candidates.size() > cap) _candidates.erase(std::prev(_candidates.end()));
}
std::vector<CalendarRsvpChange> CalendarRsvpTracker::Finish()
{
    std::vector<CalendarRsvpChange> changes;
    if (!_sampling) return changes;
    for (auto it = _previous.begin(); it != _previous.end();)
        if (!_sample.contains(it->first)) it = _previous.erase(it);
        else ++it;
    for (auto const& [id, fact] : _sample)
    {
        if (!fact.event) { _previous.erase(id); continue; }
        Observation observation{fact, {}, 0};
        auto old = _previous.find(id);
        if (old != _previous.end() && old->second.fact.event == fact.event && old->second.fact.sender == fact.sender)
        {
            observation.admission = old->second.admission;
            observation.pending = old->second.pending;
            if (old->second.fact.status != fact.status) observation.pending = fact.status;
        }
        else observation.admission = ++_admissionSerial;
        _previous.insert_or_assign(id, std::move(observation));
    }
    // Rotate publication so continuously changing early IDs cannot starve the
    // rest of the bounded observed set. Each poll visits at most 4096 records.
    auto it = _previous.upper_bound(_emissionCursor);
    for (std::size_t visited = 0; visited < _previous.size() && changes.size() < kCalendarEmissionLimit; ++visited)
    {
        if (it == _previous.end()) it = _previous.begin();
        auto& observation = it->second;
        if (observation.pending)
        {
            changes.push_back({observation.fact.event, *observation.pending});
            observation.pending.reset(); _emissionCursor = it->first;
        }
        ++it;
    }
    for (auto const& [id, fact] : _candidates)
    {
        if (!fact.event) continue;
        if (_previous.size() == kCalendarObservationLimit)
        {
            auto oldest = _previous.end();
            for (auto candidate = _previous.begin(); candidate != _previous.end(); ++candidate)
                if (!candidate->second.pending && (oldest == _previous.end()
                    || candidate->second.admission < oldest->second.admission)) oldest = candidate;
            if (oldest == _previous.end()) break;
            _previous.erase(oldest);
        }
        _previous.emplace(id, Observation{fact, {}, ++_admissionSerial});
        _admissionCursor = id;
    }
    _sample.clear(); _candidates.clear(); _sampling = false;
    return changes;
}
}
