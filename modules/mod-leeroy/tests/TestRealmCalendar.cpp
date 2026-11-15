// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmCalendar.h"
#include "TestHarness.h"
#include <array>
#include <limits>
#include <new>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>

using namespace Leeroy::Realm;
namespace
{
CalendarAuthorityFacts Authority()
{
    return {41, 9, 301, 41, 41, true, true, true, false};
}
CalendarPollBoundary Boundary() { return {5, 2, 7, 41, 9, 100, 200}; }
CalendarMemberFacts Member(std::string fence = {})
{
    CalendarMemberFacts facts;
    facts.realmOn = true;
    facts.guid = 301; facts.human = 9; facts.guild = 41;
    facts.cast = facts.randomBotAccount = true;
    facts.character = facts.body = facts.rosterMember = true;
    facts.characterGuild = facts.bodyGuild = 41;
    facts.characterAccount = facts.bodyAccount = facts.rosterAccount = 7001;
    facts.bodyFence = std::move(fence);
    return facts;
}
// Every reason the body-move admission (MutationReason) gives: none of them is a calendar fence.
constexpr std::array<std::string_view, 7> kBodyFences = {"busy:training_cleanup", "busy:training_owner",
    "busy:training_state", "busy:realm_owner", "busy:raid_owner", "busy:body_unavailable", "not_bot"};

CalendarRsvpFact Invite(std::uint64_t id = 1, std::uint8_t status = 0)
{ return {id, 1000 + id, 301, status}; }
struct AllocationCounts { unsigned created = 0, destroyed = 0, returnedIds = 0; };
struct CalendarRecord
{
    AllocationCounts& counts;
    explicit CalendarRecord(AllocationCounts& value) : counts(value) { ++counts.created; }
    ~CalendarRecord() { ++counts.destroyed; ++counts.returnedIds; }
};
std::vector<CalendarRsvpChange> Sample(CalendarRsvpTracker& tracker, CalendarPollBoundary boundary,
    std::vector<CalendarRsvpFact> const& rows, bool enabled = true, bool online = true)
{
    if (tracker.Begin(boundary, enabled, online))
        for (auto const& row : rows) tracker.Observe(row);
    return tracker.Finish();
}
}

LEEROY_TEST(RealmCalendar_OnlyCurrentCastInConfiguredGuildHasEventAuthority)
{
    CHECK(CalendarAuthorityReason(Authority()).empty());
    for (unsigned change = 0; change < 10; ++change)
    {
        auto facts = Authority();
        if (change == 0) facts.guild = 0;
        if (change == 1) facts.human = 0;
        if (change == 2) facts.creator = 0;
        if (change == 3) facts.creator = facts.human;
        if (change == 4) facts.cast = false;
        if (change == 5) facts.rosterMember = false;
        if (change == 6) facts.creatorGuild = 42;
        if (change == 7) facts.eventGuild = 42;
        if (change == 8) facts.announcement = true;
        if (change == 9) facts.guildEvent = false;
        CHECK_EQ(CalendarAuthorityReason(facts), "no_event");
    }
    auto personal = Authority(); personal.guildEvent = false; personal.eventGuild = 0;
    CHECK(CalendarAuthorityReason(personal).empty());
    personal.creatorGuild = 0; CHECK_EQ(CalendarAuthorityReason(personal), "no_event");
}

LEEROY_TEST(RealmCalendar_MemberWritesAreRefusedByIdentityOnly)
{
    auto const clean = CalendarMemberAdmission(Member());
    CHECK(clean.reason.empty()); CHECK(clean.pastFence.empty());
    auto off = Member(); off.realmOn = false;
    CHECK_EQ(CalendarMemberAdmission(off).reason, "realm_off");
    for (unsigned change = 0; change < 15; ++change)
    {
        auto facts = Member("busy:training_cleanup");
        if (change == 0) facts.guid = 0;
        if (change == 1) facts.guild = 0;
        if (change == 2) facts.guid = facts.human;
        if (change == 3) facts.cast = false;
        if (change == 4) facts.character = false;
        if (change == 5) facts.body = false;
        if (change == 6) facts.rosterMember = false;
        if (change == 7) facts.characterGuild = 42;
        if (change == 8) facts.bodyGuild = 42;
        if (change == 9) facts.characterAccount = 7002;
        if (change == 10) facts.rosterAccount = 7002;
        if (change == 11) facts.bodyAccount = 7002;
        if (change == 12) facts.characterGuild = facts.bodyGuild = 0;
        if (change == 13) { facts.characterGuild = facts.bodyGuild = 0; facts.guild = 0; }
        if (change == 14) facts.rosterAccount = facts.characterAccount = 7002;
        auto const verdict = CalendarMemberAdmission(facts);
        CHECK_EQ(verdict.reason, "no_event");
        CHECK(verdict.pastFence.empty()); // A refused write goes past nothing.
    }
    auto human = Member(); human.human = 0; // No owner configured: nobody is "the owner".
    CHECK(CalendarMemberAdmission(human).reason.empty());
    auto player = Member("not_bot"); player.randomBotAccount = false;
    CHECK_EQ(CalendarMemberAdmission(player).reason, "not_bot");
}

LEEROY_TEST(RealmCalendar_NoBodyFenceRefusesACalendarWrite)
{
    // 2026-11-10: a twelve-hour training cleanup refused every raid-night post and signup
    // (busy:training_cleanup); offline-raid posts were refused for creators inside the raid
    // (busy:training_owner). A calendar write touches no body, so each fence is only reported.
    for (auto fence : kBodyFences)
    {
        auto const verdict = CalendarMemberAdmission(Member(std::string(fence)));
        CHECK(verdict.reason.empty());
        CHECK_EQ(verdict.pastFence, fence);
    }
    auto offWithFence = Member("busy:training_cleanup"); offWithFence.realmOn = false;
    auto const off = CalendarMemberAdmission(offWithFence);
    CHECK_EQ(off.reason, "realm_off"); CHECK(off.pastFence.empty());
}

LEEROY_TEST(RealmCalendar_CreationAndSignupCapsRefuseAtBoundary)
{
    CHECK(CalendarCapacityReason(29, 99).empty());
    CHECK_EQ(CalendarCapacityReason(30, 0), "calendar_full");
    CHECK_EQ(CalendarCapacityReason(0, 100), "calendar_full");
    CHECK_EQ(CalendarCapacityReason(std::numeric_limits<std::size_t>::max(), 99), "calendar_full");
    CHECK_EQ(CalendarCapacityReason(29, std::numeric_limits<std::size_t>::max()), "calendar_full");
    CHECK(CalendarSignupReason(98, 0).empty());
    CHECK_EQ(CalendarSignupReason(99, 0), "full");
    CHECK_EQ(CalendarSignupReason(100, 0), "full");
    CHECK(CalendarSignupReason(99, 1).empty());
    CHECK(CalendarSignupReason(100, 1).empty());
    CHECK_EQ(CalendarSignupReason(98, 2), "calendar_invite_ambiguous");
    CHECK_EQ(CalendarSignupReason(100, 2), "calendar_invite_ambiguous");
}

LEEROY_TEST(RealmCalendar_StatusNamesCoverNativeRsvpsAndRejectWireOnlyUnknowns)
{
    for (auto const& status : {"signed_up", "tentative", "declined", "accepted"})
    {
        auto const native = CalendarStatus(status);
        CHECK(native.has_value());
        if (native) CHECK_EQ(CalendarStatusName(*native), std::string_view(status));
    }
    CHECK(!CalendarStatus("invited")); CHECK(!CalendarStatus("confirmed")); CHECK(!CalendarStatus(""));
    for (std::uint8_t i = 0; i < 10; ++i) CHECK(!CalendarStatusName(i).empty());
    CHECK(CalendarStatusName(10).empty()); CHECK(CalendarStatusName(255).empty());
}

LEEROY_TEST(RealmCalendar_TimestampsFitActualNativeStorageAndRemainFuture)
{
    constexpr auto max = std::numeric_limits<std::uint32_t>::max();
    CHECK(CalendarTimeReason(2000000001, 2000000000).empty());
    CHECK_EQ(CalendarTimeReason(2000000000, 2000000000), "in_past");
    CHECK_EQ(CalendarTimeReason(1999999999, 2000000000), "in_past");
    CHECK_EQ(CalendarTimeReason(0, 2000000000), "in_past");
    CHECK(CalendarTimeReason(0, 2000000000, true).empty());
    CHECK(CalendarTimeReason(max, 2000000000).empty());
    CHECK_EQ(CalendarTimeReason(std::uint64_t{max} + 1, 2000000000), "bad field: at");
    CHECK_EQ(CalendarTimeReason(std::numeric_limits<std::uint64_t>::max(), 2000000000), "bad field: at");
}

LEEROY_TEST(RealmCalendar_FinalFencePreventsNativeEntryAndReadback)
{
    unsigned writes = 0, reads = 0;
    auto result = ApplyCalendarWrite([] { return std::string("busy:realm_owner"); },
        [&] { ++writes; }, [&] { ++reads; return true; });
    CHECK(result.state == CalendarWriteState::Refused);
    CHECK_EQ(result.reason, "busy:realm_owner"); CHECK_EQ(writes, 0u); CHECK_EQ(reads, 0u);
}

LEEROY_TEST(RealmCalendar_NativeEntryNeedsExactReadbackAndStableAuthority)
{
    CalendarEventFact expected{100, 301, 41, 0, -1, 2000000000, 0, 1024, "Gather", "At the portal"};
    for (unsigned change = 0; change < 12; ++change)
    {
        auto actual = expected;
        bool authority = true, nativePointer = true;
        unsigned calls = 0;
        auto result = ApplyCalendarWrite([] { return std::string{}; }, [&]
        {
            ++calls;
            if (change == 0) ++actual.id;
            if (change == 1) ++actual.creator;
            if (change == 2) ++actual.guild;
            if (change == 3) ++actual.type;
            if (change == 4) ++actual.dungeon;
            if (change == 5) ++actual.at;
            if (change == 6) ++actual.timezone;
            if (change == 7) ++actual.flags;
            if (change == 8) actual.title = "External change";
            if (change == 9) actual.description.clear();
            if (change == 10) authority = false;
            if (change == 11) nativePointer = false;
        }, [&] { return authority && nativePointer && actual == expected; });
        CHECK_EQ(calls, 1u); CHECK(result.state == CalendarWriteState::Unknown);
        CHECK_EQ(result.reason, "calendar_write_unknown");
    }
    auto result = ApplyCalendarWrite([] { return std::string{}; }, [] {}, [&] { return expected == expected; });
    CHECK(result.state == CalendarWriteState::Complete); CHECK(result.reason.empty());
}

LEEROY_TEST(RealmCalendar_InviteIdAndAllUnchangedFieldsRemainPartOfProof)
{
    CalendarInviteFact expected{111, 100, 301, 302, 2000000000, 6, 0, "Existing note"};
    for (unsigned change = 0; change < 8; ++change)
    {
        auto actual = expected;
        auto result = ApplyCalendarWrite([] { return std::string{}; }, [&]
        {
            if (change == 0) ++actual.id;
            if (change == 1) ++actual.event;
            if (change == 2) ++actual.invitee;
            if (change == 3) ++actual.sender;
            if (change == 4) ++actual.at;
            if (change == 5) ++actual.status;
            if (change == 6) ++actual.rank;
            if (change == 7) actual.text.clear();
        }, [&] { return actual == expected; });
        CHECK(result.state == CalendarWriteState::Unknown);
    }
}

LEEROY_TEST(RealmCalendar_ExceptionsAfterNativeEntryStayUnknown)
{
    unsigned writes = 0;
    auto result = ApplyCalendarWrite([] { return std::string{}; }, [&]
    { ++writes; throw std::runtime_error("partial native write"); }, [] { return true; });
    CHECK(result.state == CalendarWriteState::Unknown); CHECK_EQ(writes, 1u);
    result = ApplyCalendarWrite([] { return std::string{}; }, [&] { ++writes; }, []() -> bool
    { throw std::runtime_error("readback unavailable"); });
    CHECK(result.state == CalendarWriteState::Unknown); CHECK_EQ(writes, 2u);
}

LEEROY_TEST(RealmCalendar_InitialInvitesAreSilentAndOnlyLaterStatusChangesEmit)
{
    CalendarRsvpTracker tracker;
    CHECK(Sample(tracker, Boundary(), {Invite(1, 1)}).empty());
    CHECK(Sample(tracker, Boundary(), {Invite(1, 1)}).empty());
    auto changes = Sample(tracker, Boundary(), {Invite(1, 2)});
    CHECK_EQ(changes.size(), std::size_t{1});
    if (!changes.empty()) { CHECK_EQ(changes.front().event, 1001u); CHECK_EQ(changes.front().status, 2u); }
    CHECK(Sample(tracker, Boundary(), {Invite(1, 2), Invite(2, 8)}).empty());
}

LEEROY_TEST(RealmCalendar_EveryLifecycleAndReloadBoundaryBaselinesSilently)
{
    for (unsigned change = 0; change < 7; ++change)
    {
        CalendarRsvpTracker tracker;
        CHECK(Sample(tracker, Boundary(), {Invite()}).empty());
        auto boundary = Boundary();
        if (change == 0) ++boundary.epoch;
        if (change == 1) ++boundary.reload;
        if (change == 2) ++boundary.humanGeneration;
        if (change == 3) ++boundary.guild;
        if (change == 4) ++boundary.human;
        if (change == 5) ++boundary.player;
        if (change == 6) ++boundary.session;
        CHECK(Sample(tracker, boundary, {Invite(1, 1)}).empty());
        CHECK_EQ(Sample(tracker, boundary, {Invite(1, 2)}).size(), std::size_t{1});
    }
}

LEEROY_TEST(RealmCalendar_OffAndOfflineNeverObserveAndRequireFreshBaseline)
{
    for (unsigned off = 0; off < 2; ++off)
    {
        CalendarRsvpTracker tracker;
        CHECK(Sample(tracker, Boundary(), {Invite()}).empty());
        CHECK(!tracker.Begin(Boundary(), off != 0, off == 0));
        tracker.Observe(Invite(1, 1));
        CHECK(tracker.Finish().empty()); CHECK_EQ(tracker.Retained(), std::size_t{0});
        CHECK(Sample(tracker, Boundary(), {Invite(1, 2)}).empty());
    }
    CalendarRsvpTracker tracker;
    auto boundary = Boundary(); boundary.session = 0;
    CHECK(!tracker.Begin(boundary, true, true));
}

LEEROY_TEST(RealmCalendar_DeletedReusedAndAmbiguousInviteIdsNeverFabricateTransitions)
{
    CalendarRsvpTracker tracker;
    Sample(tracker, Boundary(), {Invite()});
    CHECK(Sample(tracker, Boundary(), {}).empty());
    CHECK(Sample(tracker, Boundary(), {Invite(1, 1)}).empty());
    auto reused = Invite(1, 2); ++reused.event;
    CHECK(Sample(tracker, Boundary(), {reused}).empty());
    ++reused.sender; reused.status = 8;
    CHECK(Sample(tracker, Boundary(), {reused}).empty());
    auto conflict = reused; conflict.status = 1;
    CHECK(Sample(tracker, Boundary(), {reused, conflict}).empty());
    CHECK_EQ(tracker.Retained(), std::size_t{0});
    CHECK(Sample(tracker, Boundary(), {conflict}).empty());
}

LEEROY_TEST(RealmCalendar_PollCoverageAndEmissionAreBoundedWithLatestStatusBacklog)
{
    CalendarRsvpTracker tracker;
    std::vector<CalendarRsvpFact> rows;
    for (std::uint64_t id = kCalendarObservationLimit + 2 * kCalendarAdmissionLimit; id; --id)
        rows.push_back(Invite(id));
    CHECK(Sample(tracker, Boundary(), rows).empty());
    CHECK_EQ(tracker.Retained(), kCalendarObservationLimit);
    for (auto& row : rows) row.status = 1;
    auto changes = Sample(tracker, Boundary(), rows);
    CHECK_EQ(changes.size(), kCalendarEmissionLimit);
    for (auto const& change : changes) CHECK(change.event <= 1000 + kCalendarObservationLimit);
    // Unemitted changes remain bounded and coalesce to the newest actual status.
    for (auto& row : rows) if (row.invite > kCalendarEmissionLimit) row.status = 8;
    changes = Sample(tracker, Boundary(), rows);
    CHECK_EQ(changes.size(), kCalendarEmissionLimit);
    for (auto const& change : changes) CHECK_EQ(change.status, 8u);
    std::size_t emitted = 2 * kCalendarEmissionLimit;
    for (std::size_t i = 0; i < kCalendarObservationLimit / kCalendarEmissionLimit + 2; ++i)
        emitted += Sample(tracker, Boundary(), rows).size();
    CHECK_EQ(emitted, kCalendarObservationLimit + kCalendarAdmissionLimit);
    CHECK(Sample(tracker, Boundary(), rows).empty());
    CHECK_EQ(tracker.Retained(), kCalendarObservationLimit);
}

LEEROY_TEST(RealmCalendar_ReloadDropsPendingChangesAndInvalidStatuses)
{
    CalendarRsvpTracker tracker;
    std::vector<CalendarRsvpFact> rows;
    for (std::uint64_t id = 1; id <= 100; ++id) rows.push_back(Invite(id));
    Sample(tracker, Boundary(), rows);
    for (auto& row : rows) row.status = 1;
    CHECK_EQ(Sample(tracker, Boundary(), rows).size(), kCalendarEmissionLimit);
    auto boundary = Boundary(); ++boundary.reload;
    CHECK(Sample(tracker, boundary, rows).empty());
    CHECK(Sample(tracker, boundary, rows).empty());
    CHECK(Sample(tracker, boundary, {Invite(1, 255), {0, 5, 301, 1}, {2, 0, 301, 1}}).empty());
    CHECK_EQ(tracker.Retained(), std::size_t{0});
}

LEEROY_TEST(RealmCalendar_ContinuousEarlyChangesCannotStarvePendingLaterInvites)
{
    CalendarRsvpTracker tracker;
    std::vector<CalendarRsvpFact> rows;
    for (std::uint64_t id = 1; id <= 100; ++id) rows.push_back(Invite(id));
    Sample(tracker, Boundary(), rows);
    for (auto& row : rows) row.status = 1;
    CHECK_EQ(Sample(tracker, Boundary(), rows).size(), kCalendarEmissionLimit);
    for (auto& row : rows) if (row.invite <= kCalendarEmissionLimit) row.status = 8;
    auto changes = Sample(tracker, Boundary(), rows);
    CHECK_EQ(changes.size(), kCalendarEmissionLimit);
    for (auto const& change : changes) CHECK(change.event > 1000 + kCalendarEmissionLimit);
}

LEEROY_TEST(RealmCalendar_OverflowRotatesNewerInvitesIntoRealObservation)
{
    CalendarRsvpTracker tracker;
    std::vector<CalendarRsvpFact> rows;
    constexpr auto newest = kCalendarObservationLimit + 5 * kCalendarAdmissionLimit;
    for (std::uint64_t id = newest; id; --id) rows.push_back(Invite(id));
    CHECK(Sample(tracker, Boundary(), rows).empty());
    for (unsigned rotation = 0; rotation < 5; ++rotation)
    {
        CHECK(Sample(tracker, Boundary(), rows).empty());
        CHECK_EQ(tracker.Retained(), kCalendarObservationLimit);
    }
    rows.front().status = 1; // The newest ID received a turn despite all older retained invites.
    auto changes = Sample(tracker, Boundary(), rows);
    CHECK_EQ(changes.size(), std::size_t{1});
    if (!changes.empty()) { CHECK_EQ(changes.front().event, 1000 + newest); CHECK_EQ(changes.front().status, 1u); }
}

LEEROY_TEST(RealmCalendar_PacketContractIsExactToEachInstalledEnabledBoundary)
{
    std::array<std::string_view, 5> supported = {"PlayerbotsServerScript", "DungeonClearQueueFillProposalScript",
        "leeroy_realm_speech_receipt", "LeeroyRealmPvpPackets", "LeeroyRealmLfgReceiptPackets"};
    CHECK(CalendarPacketHooksSupported(CalendarPacketBoundary::Sent, supported));
    CHECK(CalendarPacketHooksSupported(CalendarPacketBoundary::CanSend, {}));
    CHECK(!CalendarPacketHooksSupported(CalendarPacketBoundary::CanSend, supported));
    CHECK(CalendarPacketHooksSupported(CalendarPacketBoundary::Sent, {}));
    for (std::size_t i = 0; i < supported.size(); ++i)
    {
        auto unknown = supported; unknown[i] = "CalendarRemovingProvider";
        CHECK(!CalendarPacketHooksSupported(CalendarPacketBoundary::Sent, unknown));
        auto invalid = supported; invalid[i] = {};
        CHECK(!CalendarPacketHooksSupported(CalendarPacketBoundary::Sent, invalid));
    }
    std::array<std::string_view, 2> duplicate = {supported[0], supported[0]};
    CHECK(!CalendarPacketHooksSupported(CalendarPacketBoundary::Sent, duplicate));
    std::array<std::string_view, kCalendarPacketProviders + 1> excess;
    excess.fill(supported[0]);
    CHECK(!CalendarPacketHooksSupported(CalendarPacketBoundary::Sent, excess));
    CHECK(!CalendarPacketHooksSupported(CalendarPacketBoundary::CanSend, excess));
}

LEEROY_TEST(RealmCalendar_EventHandoffSettlesExceptionsBeforeAndAfterInsertion)
{
    for (unsigned failure = 0; failure < 3; ++failure)
    {
        AllocationCounts counts;
        CalendarRecord* registered = nullptr;
        {
            auto local = std::make_unique<CalendarRecord>(counts);
            auto result = ApplyCalendarWrite([] { return std::string{}; }, [&]
            {
                HandoffCalendarRecord(local, [&](CalendarRecord* record)
                {
                    if (failure == 0) throw std::bad_alloc(); // _events.insert did not accept it.
                    registered = record;
                    if (failure == 1) throw std::bad_alloc(); // Native SQL submission after insertion.
                    throw std::runtime_error("non-removing packet callback threw");
                }, [&](CalendarRecord* record) noexcept { return registered == record; });
            }, [] { return true; });
            CHECK(result.state == CalendarWriteState::Unknown);
            CHECK_EQ(counts.created, 1u); CHECK_EQ(counts.destroyed, 0u);
            CHECK_EQ(bool(local), failure == 0);
            CHECK_EQ(registered != nullptr, failure != 0);
        }
        if (registered) delete registered; // The simulated manager remains the sole owner after transfer.
        CHECK_EQ(counts.destroyed, 1u); CHECK_EQ(counts.returnedIds, 1u);
    }
}

LEEROY_TEST(RealmCalendar_InviteHandoffSettlesPreNotificationVectorAndPostTransferFailures)
{
    for (unsigned failure = 0; failure < 3; ++failure)
    {
        AllocationCounts counts;
        CalendarRecord* registered = nullptr;
        unsigned notifications = 0;
        {
            auto local = std::make_unique<CalendarRecord>(counts);
            auto result = ApplyCalendarWrite([] { return std::string{}; }, [&]
            {
                HandoffCalendarRecord(local, [&](CalendarRecord* record)
                {
                    ++notifications;
                    if (failure == 0) throw std::runtime_error("packet creation/callback before invitation insert");
                    if (failure == 1) throw std::bad_alloc(); // Bucket/vector allocation did not insert the pointer.
                    registered = record;
                    throw std::runtime_error("UpdateInvite after insertion");
                }, [&](CalendarRecord* record) noexcept { return registered == record; });
            }, [] { return true; });
            CHECK(result.state == CalendarWriteState::Unknown); CHECK_EQ(notifications, 1u);
            CHECK_EQ(counts.destroyed, 0u); CHECK_EQ(bool(local), failure != 2);
            CHECK_EQ(registered != nullptr, failure == 2);
        }
        if (registered) delete registered;
        CHECK_EQ(counts.created, 1u); CHECK_EQ(counts.destroyed, 1u); CHECK_EQ(counts.returnedIds, 1u);
    }
}

LEEROY_TEST(RealmCalendar_SuccessfulHandoffLeavesExactlyOneNativeOwner)
{
    AllocationCounts counts;
    CalendarRecord* registered = nullptr;
    {
        auto local = std::make_unique<CalendarRecord>(counts);
        auto result = ApplyCalendarWrite([] { return std::string{}; }, [&]
        {
            HandoffCalendarRecord(local, [&](CalendarRecord* record) { registered = record; },
                [&](CalendarRecord* record) noexcept { return registered == record; });
        }, [&] { return registered != nullptr; });
        CHECK(result.state == CalendarWriteState::Complete); CHECK(!local);
        CHECK_EQ(counts.destroyed, 0u);
    }
    CHECK_EQ(counts.destroyed, 0u);
    delete registered;
    CHECK_EQ(counts.destroyed, 1u); CHECK_EQ(counts.returnedIds, 1u);
}

LEEROY_TEST(RealmCalendar_RemoveThenThrowProvidersRefuseBeforeRecordOrIdAllocation)
{
    for (auto boundary : {CalendarPacketBoundary::Sent, CalendarPacketBoundary::CanSend})
    {
        std::array<std::string_view, 1> providers = {"CalendarRemovingProvider"};
        AllocationCounts counts;
        CalendarRecord* registered = nullptr;
        bool called = false;
        unsigned allocatedIds = 0;
        auto removingCallback = [&]
        {
            called = true;
            delete registered; registered = nullptr;
            throw std::runtime_error("removed native record before throw");
        };
        auto result = ApplyCalendarWrite([&]
        {
            return CalendarPacketHooksSupported(boundary, providers) ? std::string{}
                : std::string("calendar_scripts_unsupported");
        }, [&]
        {
            ++allocatedIds;
            auto local = std::make_unique<CalendarRecord>(counts);
            HandoffCalendarRecord(local, [&](CalendarRecord* record)
            { registered = record; removingCallback(); },
                [&](CalendarRecord* record) noexcept { return registered == record; });
        }, [] { return true; });
        CHECK(result.state == CalendarWriteState::Refused); CHECK_EQ(result.reason, "calendar_scripts_unsupported");
        CHECK(!called); CHECK_EQ(allocatedIds, 0u); CHECK_EQ(counts.created, 0u);
        CHECK_EQ(counts.destroyed, 0u); CHECK_EQ(counts.returnedIds, 0u); CHECK(!registered);
    }
}

LEEROY_TEST(RealmCalendar_FinalHookFenceRefusesNewProviderAndCleansOnlyCallerOwnedRecord)
{
    std::array<std::string_view, 1> providers = {"PlayerbotsServerScript"};
    CHECK(CalendarPacketHooksSupported(CalendarPacketBoundary::Sent, providers)); // Initial admission.
    AllocationCounts counts;
    bool called = false;
    {
        auto local = std::make_unique<CalendarRecord>(counts);
        providers[0] = "CalendarRemovingProvider";
        auto result = ApplyCalendarWrite([&]
        {
            return CalendarPacketHooksSupported(CalendarPacketBoundary::Sent, providers) ? std::string{}
                : std::string("calendar_scripts_unsupported");
        }, [&] { called = true; }, [] { return true; });
        CHECK(result.state == CalendarWriteState::Refused); CHECK(!called); CHECK(local != nullptr);
        CHECK_EQ(counts.destroyed, 0u);
    }
    CHECK_EQ(counts.created, 1u); CHECK_EQ(counts.destroyed, 1u); CHECK_EQ(counts.returnedIds, 1u);
}

LEEROY_TEST(RealmCalendar_ThrowingConstructorReturnsItsIdWithoutAnObjectDestructor)
{
    AllocationCounts counts;
    unsigned explicitReturns = 0;
    std::uint64_t returnedId = 0;
    bool caught = false;
    try
    {
        ConstructCalendarRecord(113, [](std::uint64_t) -> std::unique_ptr<CalendarRecord>
        { throw std::bad_alloc(); }, [&](std::uint64_t id) { ++explicitReturns; returnedId = id; });
    }
    catch (std::bad_alloc const&) { caught = true; }
    CHECK(caught); CHECK_EQ(explicitReturns, 1u); CHECK_EQ(returnedId, 113u);
    CHECK_EQ(counts.created, 0u); CHECK_EQ(counts.destroyed, 0u);
    {
        auto local = ConstructCalendarRecord(114, [&](std::uint64_t)
        { return std::make_unique<CalendarRecord>(counts); }, [&](std::uint64_t) { ++explicitReturns; });
        CHECK(local != nullptr); CHECK_EQ(explicitReturns, 1u); CHECK_EQ(counts.destroyed, 0u);
    }
    CHECK_EQ(counts.created, 1u); CHECK_EQ(counts.destroyed, 1u); CHECK_EQ(counts.returnedIds, 1u);
    CHECK_EQ(explicitReturns, 1u); // A successful object's destructor, not the factory, owns its ID.
}
