// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "RealmCalendar.h"
#include "RealmPorts.h"
#include "LeeroyRealmCalendar.h"
#include "CalendarMgr.h"
#include "GameTime.h"
#include "ScriptMgr.h"
#include "ServerScript.h"
#include "WorldScript.h"
#include <array>
#include <atomic>
#include <memory>
#include <stdexcept>

namespace Leeroy::Realm
{
namespace
{
using Json = nlohmann::json;
static_assert(CALENDAR_MAX_EVENTS == kCalendarCreatorLimit && CALENDAR_MAX_GUILD_EVENTS == kCalendarGuildLimit);
static_assert(CALENDAR_STATUS_ACCEPTED == 1 && CALENDAR_STATUS_DECLINED == 2 && CALENDAR_STATUS_SIGNED_UP == 6
    && CALENDAR_STATUS_TENTATIVE == 8 && CALENDAR_STATUS_REMOVED == 9);
std::atomic<std::uint64_t> reloadRevision{0};
CalendarRsvpTracker rsvps; // World-thread only; this is observation state, never ownership.

bool SupportedPacketHooks(std::size_t hook, CalendarPacketBoundary boundary)
{
    auto const& hooks = ScriptRegistry<ServerScript>::EnabledHooks;
    if (hook >= hooks.size()) return CalendarPacketHooksSupported(boundary, {});
    if (hooks[hook].size() > kCalendarPacketProviders) return false;
    std::array<std::string_view, kCalendarPacketProviders> providers;
    std::size_t count = 0;
    for (auto const* script : hooks[hook])
    {
        if (!script) return false;
        providers[count++] = script->GetName();
    }
    return CalendarPacketHooksSupported(boundary, std::span<std::string_view const>(providers.data(), count));
}
std::string PacketHookReason()
{
    // World-thread registry is static during these audited callbacks. The
    // installed providers cannot remove calendar objects or register scripts
    // for any opcode emitted by AddEvent/AddInvite/update/status notification.
    return SupportedPacketHooks(SERVERHOOK_ON_PACKET_SENT, CalendarPacketBoundary::Sent)
        && SupportedPacketHooks(SERVERHOOK_CAN_PACKET_SEND, CalendarPacketBoundary::CanSend)
        ? "" : "calendar_scripts_unsupported";
}
bool NativeRegistered(CalendarEvent* candidate) noexcept
{
    // Pointer-only comparison, with no allocating/logging native lookup. This
    // absence proof is valid only under PacketHookReason's no-removal contract.
    for (auto* event : sCalendarMgr->GetEvents()) if (event == candidate) return true;
    return false;
}
bool NativeRegistered(CalendarInvite* candidate) noexcept
{
    for (auto const& [event, invites] : sCalendarMgr->GetInvites())
    {
        (void)event;
        for (auto* invite : invites) if (invite == candidate) return true;
    }
    return false;
}
bool NativeInviteIdExists(std::uint64_t candidate) noexcept
{
    // GetInvite logs on absence. Pre-construction ID checks must not allocate
    // or throw before a record destructor/constructor guard owns the ID.
    for (auto const& [event, invites] : sCalendarMgr->GetInvites())
    {
        (void)event;
        for (auto const* invite : invites) if (invite && invite->GetInviteId() == candidate) return true;
    }
    return false;
}
std::uint64_t NowSeconds() { return static_cast<std::uint64_t>(GameTime::GetGameTime().count()); }
CalendarEventFact Fact(CalendarEvent const& event)
{
    auto const creator = event.GetCreatorGUID().GetCounter();
    return {event.GetEventId(), event.GetCreatorGUID() == PlayerGuid(creator) ? creator : 0, event.GetGuildId(),
        static_cast<std::uint8_t>(event.GetType()), event.GetDungeonId(),
        static_cast<std::uint64_t>(event.GetEventTime()), static_cast<std::uint64_t>(event.GetTimeZoneTime()),
        event.GetFlags(), event.GetTitle(), event.GetDescription()};
}
CalendarInviteFact Fact(CalendarInvite const& invite)
{
    return {invite.GetInviteId(), invite.GetEventId(), invite.GetInviteeGUID().GetCounter(),
        invite.GetSenderGUID().GetCounter(), static_cast<std::uint64_t>(invite.GetStatusTime()),
        static_cast<std::uint8_t>(invite.GetStatus()), static_cast<std::uint8_t>(invite.GetRank()), invite.GetText()};
}
Json EventFact(CalendarEventFact const& event)
{
    return Json::array({event.id, event.creator, event.guild, event.type, event.dungeon, event.at,
        event.timezone, event.flags, event.title, event.description});
}
Json BodyFact(std::optional<BodyIdentity> const& body)
{
    if (!body) return nullptr;
    return Json::array({body->account, body->guild, body->map, body->instance, body->group,
        body->classId, body->race, body->sex, reinterpret_cast<std::uintptr_t>(body->player),
        reinterpret_cast<std::uintptr_t>(body->ai), reinterpret_cast<std::uintptr_t>(body->session)});
}
std::string MemberReason(std::uint32_t guid, std::string_view operation = {})
{
    auto& module = RealmModule::Instance();
    if (!module.On()) return "realm_off";
    auto const body = Identity(guid);
    auto const* character = sCharacterCache->GetCharacterCacheByGuid(PlayerGuid(guid));
    auto const* guild = sGuildMgr->GetGuildById(GuildId());
    auto const* member = guild ? guild->GetMember(PlayerGuid(guid)) : nullptr;
    if (!guid || guid == HumanGuid() || !IsCast(guid) || !body || body->guild != GuildId()
        || !character || character->GuildId != GuildId() || character->AccountId != body->account
        || !member || member->GetGUID() != PlayerGuid(guid) || member->GetAccountId() != body->account)
        return "no_event";
    // Initial admission has no reservation yet. An execution check uses only
    // its exact existing holder; an unrelated owner always refuses the write.
    return module.MutationReason(guid, !operation.empty() && module.Holder(guid) == operation ? operation : std::string_view{});
}
std::string EventReason(CalendarEventFact const& event, std::string_view operation = {})
{
    auto const creator = Identity(event.creator);
    CalendarAuthorityFacts facts{GuildId(), HumanGuid(), event.creator, creator ? creator->guild : 0, event.guild,
        IsCast(event.creator), IsConfiguredGuildMember(event.creator),
        CalendarEvent::IsGuildEvent(event.flags), CalendarEvent::IsGuildAnnouncement(event.flags)};
    if (auto why = CalendarAuthorityReason(facts); !why.empty()) return why;
    return MemberReason(event.creator, operation);
}
Json CreatorFence(std::uint32_t creator)
{
    auto& module = RealmModule::Instance();
    return {{"epoch", module.Epoch()}, {"guild", GuildId()}, {"human", HumanGuid()},
        {"creator_generation", module.BodyGeneration(creator)}, {"creator_body", BodyFact(Identity(creator))}};
}
bool ExactCreator(Json const& fence, std::uint32_t creator)
{
    auto& module = RealmModule::Instance();
    return module.On() && module.Epoch() == fence.at("epoch").get<std::uint64_t>()
        && GuildId() == fence.at("guild").get<std::uint32_t>() && HumanGuid() == fence.at("human").get<std::uint32_t>()
        && module.BodyGeneration(creator) == fence.at("creator_generation").get<std::uint64_t>()
        && BodyFact(Identity(creator)) == fence.at("creator_body");
}
CalendarEventType EventType(std::string const& type)
{
    if (type == "raid") return CALENDAR_TYPE_RAID;
    if (type == "dungeon") return CALENDAR_TYPE_DUNGEON;
    if (type == "pvp") return CALENDAR_TYPE_PVP;
    if (type == "meeting") return CALENDAR_TYPE_MEETING;
    return CALENDAR_TYPE_OTHER;
}
bool ExactEvent(CalendarEvent* original, CalendarEventFact const& expected)
{
    auto* current = sCalendarMgr->GetEvent(expected.id); // Native lookup scans all events.
    return current && current == original && current->GetCreatorGUID() == PlayerGuid(expected.creator)
        && Fact(*current) == expected;
}
struct InviteLookup
{
    CalendarInvite* invite = nullptr;
    std::size_t total = 0, matches = 0;
    bool valid = true;
};
InviteLookup FindInvite(std::uint64_t eventId, std::uint32_t guid)
{
    InviteLookup result;
    // Unlike GetEventInvites(), this const native collection lookup does not
    // insert an empty event bucket during read-only admission.
    auto const& all = sCalendarMgr->GetInvites();
    auto found = all.find(eventId);
    if (found == all.end()) return result;
    result.total = found->second.size();
    for (auto* invite : found->second)
    {
        if (!invite || invite->GetEventId() != eventId || !invite->GetInviteId())
        { result.valid = false; continue; }
        if (invite->GetInviteeGUID() == PlayerGuid(guid))
        { result.invite = invite; ++result.matches; }
    }
    return result;
}
bool ExactInvite(CalendarInvite* original, CalendarInviteFact const& expected)
{
    auto* current = sCalendarMgr->GetInvite(expected.id); // Native lookup scans all invite buckets.
    auto const found = FindInvite(expected.event, expected.invitee);
    return current && current == original && found.valid && found.matches == 1 && found.invite == current
        && current->GetInviteeGUID() == PlayerGuid(expected.invitee)
        && current->GetSenderGUID() == PlayerGuid(expected.sender) && Fact(*current) == expected;
}
LeeroyCommandResult Post(Json const& message)
{
    std::string why;
    auto const command = ParseCalendarPost(message, why);
    if (!command) return {false, why};
    if (!(why = PacketHookReason()).empty()) return {false, why};
    auto& module = RealmModule::Instance();
    if (!(why = MemberReason(command->creatorGuid)).empty()) return {false, why};
    if (!(why = CalendarTimeReason(command->at, NowSeconds())).empty()) return {false, why};
    if (command->inviteHuman && !sCharacterCache->GetCharacterCacheByGuid(PlayerGuid(HumanGuid())))
        return {false, "human unavailable"};
    auto const fence = CreatorFence(command->creatorGuid);
    auto* const guild = sGuildMgr->GetGuildById(GuildId());
    // Direct AddEvent enforces neither cap. Both scans are mandatory even for
    // a personal event and include this creator's existing guild events.
    if (!(why = CalendarCapacityReason(sCalendarMgr->GetEventsCreatedBy(PlayerGuid(command->creatorGuid), true).size(),
        sCalendarMgr->GetGuildEvents(GuildId()).size())).empty()) return {false, why};
    auto const eventId = sCalendarMgr->GetFreeEventId();
    if (!eventId || sCalendarMgr->GetEvent(eventId)) return {false, "calendar_id_unavailable"};
    auto event = ConstructCalendarRecord(eventId, [&](std::uint64_t id)
    {
        return std::make_unique<CalendarEvent>(id, PlayerGuid(command->creatorGuid),
            command->guildEvent ? GuildId() : 0, EventType(command->eventType), command->dungeonId,
            static_cast<time_t>(command->at), command->guildEvent ? CALENDAR_FLAG_GUILD_EVENT : 0, 0,
            command->title, command->description);
    }, [](std::uint64_t id) { sCalendarMgr->FreeEventId(id); });
    auto* const native = event.get();
    auto const expected = Fact(*native);
    std::unique_ptr<CalendarInvite> humanInvite;
    CalendarInvite* nativeInvite = nullptr;
    CalendarInviteFact expectedInvite;
    if (command->inviteHuman)
    {
        auto const inviteId = sCalendarMgr->GetFreeInviteId();
        if (!inviteId || NativeInviteIdExists(inviteId)) return {false, "calendar_id_unavailable"};
        humanInvite = ConstructCalendarRecord(inviteId, [&](std::uint64_t id)
        {
            return std::make_unique<CalendarInvite>(id, eventId, PlayerGuid(HumanGuid()),
                PlayerGuid(command->creatorGuid), 946684800, CALENDAR_STATUS_INVITED, CALENDAR_RANK_PLAYER, "");
        }, [](std::uint64_t id) { sCalendarMgr->FreeInviteId(id); });
        nativeInvite = humanInvite.get(); expectedInvite = Fact(*nativeInvite);
    }
    auto const result = ApplyCalendarWrite([&]() -> std::string
    {
        if (auto reason = PacketHookReason(); !reason.empty()) return reason;
        if (!ExactCreator(fence, command->creatorGuid) || sGuildMgr->GetGuildById(GuildId()) != guild)
            return "calendar_authority_changed";
        if (auto reason = CalendarTimeReason(command->at, NowSeconds()); !reason.empty()) return reason;
        return EventReason(expected);
    }, [&]
    {
        HandoffCalendarRecord(event, [](CalendarEvent* record)
        { sCalendarMgr->AddEvent(record, CALENDAR_SENDTYPE_ADD); },
            [](CalendarEvent* record) noexcept { return NativeRegistered(record); });
        if (nativeInvite)
        {
            if (!PacketHookReason().empty() || !ExactCreator(fence, command->creatorGuid) || !EventReason(expected).empty()
                || !ExactEvent(native, expected)) throw std::runtime_error("calendar event changed");
            HandoffCalendarRecord(humanInvite, [&](CalendarInvite* record) { sCalendarMgr->AddInvite(native, record); },
                [](CalendarInvite* record) noexcept { return NativeRegistered(record); });
        }
    }, [&]
    {
        return ExactCreator(fence, command->creatorGuid) && sGuildMgr->GetGuildById(GuildId()) == guild
            && EventReason(expected).empty() && ExactEvent(native, expected)
            && (!nativeInvite || ExactInvite(nativeInvite, expectedInvite));
    });
    bool const ok = result.state == CalendarWriteState::Complete;
    if (result.state != CalendarWriteState::Refused)
        module.PublishJob({message.at("id").get<std::string>(), "calendar_post", ok, result.reason,
            command->creatorGuid, eventId});
    return {ok, result.reason};
}
LeeroyCommandResult Update(Json const& message)
{
    std::string why;
    auto const command = ParseCalendarUpdate(message, why);
    if (!command) return {false, why};
    if (!(why = PacketHookReason()).empty()) return {false, why};
    auto* const event = sCalendarMgr->GetEvent(command->eventId);
    if (!event) return {false, "no_event"};
    auto const before = Fact(*event);
    if (!(why = EventReason(before)).empty()) return {false, why};
    if (!(why = CalendarTimeReason(command->at, NowSeconds(), true)).empty()) return {false, why};
    auto const fence = CreatorFence(before.creator);
    auto expected = before;
    if (!command->title.empty()) expected.title = command->title;
    if (!command->description.empty()) expected.description = command->description;
    if (command->at) expected.at = command->at;
    auto result = ApplyCalendarWrite([&]() -> std::string
    {
        if (auto reason = PacketHookReason(); !reason.empty()) return reason;
        if (!ExactCreator(fence, before.creator) || !ExactEvent(event, before)) return "calendar_authority_changed";
        if (auto reason = CalendarTimeReason(command->at, NowSeconds(), true); !reason.empty()) return reason;
        return EventReason(before);
    }, [&]
    {
        event->SetTitle(expected.title); event->SetDescription(expected.description);
        event->SetEventTime(static_cast<time_t>(expected.at));
        sCalendarMgr->UpdateEvent(event);
        // UpdateEvent persists only; this is the native handler's separate alert.
        if (!ExactCreator(fence, before.creator) || !EventReason(expected).empty() || !ExactEvent(event, expected))
            throw std::runtime_error("calendar update changed");
        sCalendarMgr->SendCalendarEventUpdateAlert(*event, static_cast<time_t>(before.at));
    }, [&]
    { return ExactCreator(fence, before.creator) && EventReason(expected).empty() && ExactEvent(event, expected); });
    return {result.state == CalendarWriteState::Complete, result.reason};
}
std::string SignupAdmission(Queued const& entry, std::uint32_t guid)
{
    if (auto reason = PacketHookReason(); !reason.empty()) return reason;
    auto const eventId = entry.payload.at("event_id").get<std::uint64_t>();
    auto* const event = sCalendarMgr->GetEvent(eventId);
    if (!event || EventFact(Fact(*event)) != entry.payload.at("event")) return "no_event";
    auto const fact = Fact(*event);
    if (auto why = EventReason(fact, fact.creator == guid ? entry.id : std::string_view{}); !why.empty()) return why;
    if (!ExactCreator(entry.payload.at("creator"), fact.creator)) return "calendar_authority_changed";
    if (auto why = MemberReason(guid, entry.id); !why.empty()) return why;
    if (fact.flags & CALENDAR_FLAG_INVITES_LOCKED) return "calendar_locked";
    auto const found = FindInvite(eventId, guid);
    if (!found.valid) return "calendar_invite_ambiguous";
    return CalendarSignupReason(found.total, found.matches);
}
RealmStep Signup(Queued& entry, std::uint32_t guid)
{
    if (auto why = SignupAdmission(entry, guid); !why.empty()) return {StepState::Refused, why};
    auto& module = RealmModule::Instance();
    auto* const event = sCalendarMgr->GetEvent(entry.payload.at("event_id").get<std::uint64_t>());
    auto const eventFact = Fact(*event);
    auto const found = FindInvite(eventFact.id, guid);
    auto const status = CalendarStatus(entry.payload.at("status").get<std::string>());
    if (!status) return {StepState::Refused, "bad field: status"};
    CalendarInvite* native = found.invite;
    std::unique_ptr<CalendarInvite> added;
    CalendarInviteFact before, expected;
    if (native) { before = Fact(*native); expected = before; }
    else
    {
        auto const id = sCalendarMgr->GetFreeInviteId();
        if (!id || NativeInviteIdExists(id)) return {StepState::Refused, "calendar_id_unavailable"};
        added = ConstructCalendarRecord(id, [&](std::uint64_t allocated)
        {
            return std::make_unique<CalendarInvite>(allocated, eventFact.id, PlayerGuid(guid), PlayerGuid(guid),
                static_cast<time_t>(NowSeconds()), static_cast<CalendarInviteStatus>(*status), CALENDAR_RANK_PLAYER, "");
        }, [](std::uint64_t allocated) { sCalendarMgr->FreeInviteId(allocated); });
        native = added.get(); expected = Fact(*native);
    }
    expected.status = *status; expected.at = NowSeconds();
    auto const result = ApplyCalendarWrite([&]() -> std::string
    {
        if (auto why = SignupAdmission(entry, guid); !why.empty()) return why;
        if (auto why = module.ValidateOwnedBody(guid, entry.id); !why.empty()) return why;
        if (!ExactEvent(event, eventFact) || (found.invite && !ExactInvite(found.invite, before)))
            return "calendar_invite_changed";
        return {};
    }, [&]
    {
        native->SetStatus(static_cast<CalendarInviteStatus>(expected.status));
        native->SetStatusTime(static_cast<time_t>(expected.at));
        if (added)
            HandoffCalendarRecord(added, [&](CalendarInvite* record) { sCalendarMgr->AddInvite(event, record); },
                [](CalendarInvite* record) noexcept { return NativeRegistered(record); });
        else sCalendarMgr->UpdateInvite(native);
        if (!module.ValidateOwnedBody(guid, entry.id).empty() || !SignupAdmission(entry, guid).empty()
            || !ExactEvent(event, eventFact) || !ExactInvite(native, expected))
            throw std::runtime_error("calendar signup changed");
        sCalendarMgr->SendCalendarEventStatus(*event, *native);
    }, [&]
    {
        return module.ValidateOwnedBody(guid, entry.id).empty() && SignupAdmission(entry, guid).empty()
            && ExactEvent(event, eventFact) && ExactInvite(native, expected);
    });
    if (result.state == CalendarWriteState::Unknown) return {StepState::Unknown, result.reason};
    return {result.state == CalendarWriteState::Complete ? StepState::Complete : StepState::Refused, result.reason};
}
void Poll()
{
    auto& module = RealmModule::Instance();
    auto* human = FindOnline(HumanGuid());
    CalendarPollBoundary boundary{module.Epoch(), reloadRevision.load(), module.BodyGeneration(HumanGuid()),
        GuildId(), HumanGuid(), reinterpret_cast<std::uintptr_t>(human),
        reinterpret_cast<std::uintptr_t>(human ? human->GetSession() : nullptr)};
    if (!rsvps.Begin(boundary, module.On(), human != nullptr)) return;
    // This core getter scans every invite and returns all matching pointers.
    // Native temporary cost is O(all invites + human invites); retained adapter
    // observations are limited to 4096, with up to 32 fair rotating admissions
    // and publications per 60-second poll (the initial baseline admits up to 4096).
    auto const invites = sCalendarMgr->GetPlayerInvites(PlayerGuid(boundary.human));
    for (auto const* invite : invites)
        if (invite && invite->GetInviteeGUID() == PlayerGuid(boundary.human))
            rsvps.Observe({invite->GetInviteId(), invite->GetEventId(), invite->GetSenderGUID().GetCounter(),
                static_cast<std::uint8_t>(invite->GetStatus())});
    auto const changes = rsvps.Finish();
    if (!module.On() || module.Epoch() != boundary.epoch || reloadRevision.load() != boundary.reload
        || GuildId() != boundary.guild || HumanGuid() != boundary.human
        || module.BodyGeneration(boundary.human) != boundary.humanGeneration || FindOnline(boundary.human) != human)
    { rsvps.Reset(); return; }
    for (auto const& change : changes) Publish(MakeCalendarRsvp(change.event, CalendarStatusName(change.status)));
}
class CalendarWorldScript final : public WorldScript
{
public:
    CalendarWorldScript() : WorldScript("LeeroyRealmCalendarWorldScript", {WORLDHOOK_ON_AFTER_CONFIG_LOAD}) { }
    void OnAfterConfigLoad(bool /*reload*/) override { ++reloadRevision; }
};
}
void RegisterRealmCalendarHandlers()
{
    auto& module = RealmModule::Instance();
    module.RegisterExecutor("calendar_signup", Signup, SignupAdmission);
    module.RegisterPoll("realm_calendar", 60000, Poll);
    RegisterCommand("calendar_post", Post);
    RegisterCommand("calendar_update", Update);
    RegisterCommand("calendar_signup", [](Json const& message) -> LeeroyCommandResult
    {
        std::string why;
        auto const command = ParseCalendarSignup(message, why);
        if (!command) return {false, why};
        if (!(why = PacketHookReason()).empty()) return {false, why};
        auto* const event = sCalendarMgr->GetEvent(command->eventId);
        if (!event) return {false, "no_event"};
        auto const fact = Fact(*event);
        if (!(why = EventReason(fact)).empty()) return {false, why};
        Queued entry{message.at("id").get<std::string>(), "calendar_signup", {command->guid}, 0,
            {{"event_id", command->eventId}, {"status", command->status}, {"event", EventFact(fact)},
                {"creator", CreatorFence(fact.creator)}}};
        return RealmModule::Instance().Accept(std::move(entry), why) ? LeeroyCommandResult{true, ""}
            : LeeroyCommandResult{false, why};
    });
}
void AddRealmCalendarScripts() { new CalendarWorldScript(); }
}
