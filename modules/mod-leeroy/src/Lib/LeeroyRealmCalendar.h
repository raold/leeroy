// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_CALENDAR_POLICY_H
#define MOD_LEEROY_REALM_CALENDAR_POLICY_H

#include <cstddef>
#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace Leeroy::Realm
{
inline constexpr std::size_t kCalendarCreatorLimit = 30, kCalendarGuildLimit = 100, kCalendarSignupLimit = 99;
inline constexpr std::size_t kCalendarObservationLimit = 4096, kCalendarEmissionLimit = 32, kCalendarAdmissionLimit = 32;
inline constexpr std::size_t kCalendarPacketProviders = 8;

enum class CalendarPacketBoundary { Sent, CanSend };
// Exact installed outgoing-hook method contracts for calendar notification
// opcodes only. Unknown or newly enabled methods require another source audit.
bool CalendarPacketHooksSupported(CalendarPacketBoundary, std::span<std::string_view const> providers);

// A throwing allocation/constructor has no Record destructor to return its
// already allocated native ID. Once construction succeeds, the normal Record
// destructor (or native manager) assumes that responsibility instead.
template<class Factory, class ReleaseId>
auto ConstructCalendarRecord(std::uint64_t id, Factory&& create, ReleaseId&& releaseId) -> decltype(create(id))
{
    try { return create(id); }
    catch (...) { releaseId(id); throw; }
}

// Requires the audited static-registry/no-calendar-removal callback contract
// before native entry. AddEvent inserts before notification; AddInvite notifies
// before insertion. A noexcept raw-pointer membership test settles ownership
// on either return or unwind without dereferencing or discarding an unowned
// pointer. Absence is NOT ownership proof outside that supported contract.
template<class Record, class Submit, class Registered>
void HandoffCalendarRecord(std::unique_ptr<Record>& local, Submit&& submit, Registered&& registered)
{
    static_assert(noexcept(registered(static_cast<Record*>(nullptr))), "handoff membership must not throw");
    struct Settlement
    {
        std::unique_ptr<Record>& owner;
        Registered& present;
        ~Settlement() noexcept { if (owner && present(owner.get())) owner.release(); }
    } settlement{local, registered};
    submit(local.get());
}

struct CalendarAuthorityFacts
{
    std::uint32_t guild = 0, human = 0, creator = 0, creatorGuild = 0, eventGuild = 0;
    bool cast = false, rosterMember = false, guildEvent = false, announcement = false;
};
// Personal events have native guildId zero, but still require a current cast
// creator whose exact native identity and roster belong to the configured guild.
std::string CalendarAuthorityReason(CalendarAuthorityFacts const&);

// A character writing the guild calendar (a post's creator, an event's creator, a signer).
// A calendar post, update or signup changes CalendarMgr rows and sends calendar packets; it
// never touches a body (strategies, group, master, position, DC settings). So only identity
// can refuse one: the Realm on, a cast member on a random-bot account who is not the owner,
// whose character cache row, live body (when connected) and roster entry all name the
// configured guild and one account. No body fence ever does: not a training cleanup, not a
// training, offline-raid, Realm or raid owner, not a pending bot command, not the queue fence.
// bodyFence is what the body-move admission says about this character; it never refuses a
// calendar write and is carried only so the write that goes past it can be logged.
struct CalendarMemberFacts
{
    bool realmOn = false;
    std::uint32_t guid = 0, human = 0, guild = 0;
    bool cast = false, randomBotAccount = false;
    bool character = false, body = false, rosterMember = false;
    std::uint32_t characterGuild = 0, characterAccount = 0, bodyGuild = 0, bodyAccount = 0, rosterAccount = 0;
    std::string bodyFence{};
};
struct CalendarMemberVerdict
{
    std::string reason;    // Empty: the write may go on.
    std::string pastFence; // The body fence an admitted write goes past, for the log only.
};
CalendarMemberVerdict CalendarMemberAdmission(CalendarMemberFacts const&);
std::string CalendarCapacityReason(std::size_t creatorEvents, std::size_t guildEvents);
std::string CalendarSignupReason(std::size_t invites, std::size_t matchingInvites);
std::string CalendarTimeReason(std::uint64_t at, std::uint64_t now, bool allowKeep = false);
std::optional<std::uint8_t> CalendarStatus(std::string_view);
std::string_view CalendarStatusName(std::uint8_t);

struct CalendarEventFact
{
    std::uint64_t id = 0;
    std::uint32_t creator = 0, guild = 0;
    std::uint8_t type = 0;
    std::int32_t dungeon = -1;
    std::uint64_t at = 0, timezone = 0;
    std::uint32_t flags = 0;
    std::string title, description;
    bool operator==(CalendarEventFact const&) const = default;
};
struct CalendarInviteFact
{
    std::uint64_t id = 0, event = 0;
    std::uint32_t invitee = 0, sender = 0;
    std::uint64_t at = 0;
    std::uint8_t status = 0, rank = 0;
    std::string text;
    bool operator==(CalendarInviteFact const&) const = default;
};

enum class CalendarWriteState { Refused, Complete, Unknown };
struct CalendarWriteResult { CalendarWriteState state; std::string reason; };
// No mutation after a failed final fence. Once native entry is attempted, an
// exception or failed exact readback is explicitly unknown, never a clean refusal.
template<class Ready, class Write, class Verify>
CalendarWriteResult ApplyCalendarWrite(Ready&& ready, Write&& write, Verify&& verify)
{
    if (auto why = ready(); !why.empty()) return {CalendarWriteState::Refused, std::move(why)};
    try
    {
        write();
        if (verify()) return {CalendarWriteState::Complete, {}};
    }
    catch (...) { }
    return {CalendarWriteState::Unknown, "calendar_write_unknown"};
}

struct CalendarPollBoundary
{
    std::uint64_t epoch = 0, reload = 0, humanGeneration = 0;
    std::uint32_t guild = 0, human = 0;
    std::uintptr_t player = 0, session = 0;
    bool operator==(CalendarPollBoundary const&) const = default;
};
struct CalendarRsvpFact
{
    std::uint64_t invite = 0, event = 0;
    std::uint32_t sender = 0;
    std::uint8_t status = 0;
    bool operator==(CalendarRsvpFact const&) const = default;
};
struct CalendarRsvpChange { std::uint64_t event; std::uint8_t status; };
// Refresh all retained IDs each poll; rotate up to 32 unseen IDs into the 4096
// slots, ordered circularly by invite ID. Evict only after pending publication.
// The first snapshot admits up to 4096. New/re-entering identities baseline
// silently; changes while outside this bounded observed set are not inferred.
// Input streams from GetPlayerInvites without a second unbounded pointer copy.
class CalendarRsvpTracker
{
public:
    bool Begin(CalendarPollBoundary const&, bool enabled, bool online);
    void Observe(CalendarRsvpFact const&);
    std::vector<CalendarRsvpChange> Finish();
    void Reset();
    std::size_t Retained() const { return _previous.size(); }
private:
    struct Observation { CalendarRsvpFact fact; std::optional<std::uint8_t> pending; std::uint64_t admission = 0; };
    struct CircularOrder
    {
        std::uint64_t cursor = 0;
        bool operator()(std::uint64_t first, std::uint64_t second) const
        {
            if ((first > cursor) != (second > cursor)) return first > cursor;
            return first < second;
        }
    };
    std::optional<CalendarPollBoundary> _boundary;
    bool _sampling = false;
    std::uint64_t _emissionCursor = 0;
    std::uint64_t _admissionCursor = 0, _admissionSerial = 0;
    std::map<std::uint64_t, Observation> _previous;
    std::map<std::uint64_t, CalendarRsvpFact> _sample;
    std::map<std::uint64_t, CalendarRsvpFact, CircularOrder> _candidates;
};
}
#endif
