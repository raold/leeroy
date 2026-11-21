// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "DayPlans.h"
#include "Guild/MemberDungeonJob.h"
#include "Guild/MemberDungeonReceipts.h"
#include "LeeroyDayPlan.h"
#include "RealmPorts.h"
#include "Guild/GuildAdd.h"
#include "Guild/Strings.h"
#include "Group.h"
#include <cmath>
#include <random>

namespace Leeroy::Realm
{
namespace
{
using Json = nlohmann::json;
std::map<std::string, DayPermit> permits; // Indexed by opaque capability. World thread only.
struct PreparedOwner
{
    Json original;
    std::uint64_t serial, epoch, humanGeneration;
    std::uint32_t guild, human;
    std::int64_t expires;
    std::optional<BodyIdentity> owner;
    std::set<std::uint32_t> group;
    std::set<std::string> tokens;
    bool consumed;
    Json driverBinding; // Non-null tickets permit only one dedicated-driver preparation, never body ownership.
    std::string heroic;
    std::vector<std::uint32_t> heroicMembers;
    std::map<std::uint32_t, std::pair<std::optional<BodyIdentity>, std::uint64_t>> bodies;
};
std::map<std::string, PreparedOwner> prepared;
std::set<std::uint32_t> OwnerGroup(std::optional<BodyIdentity> const& owner)
{
    std::set<std::uint32_t> members;
    if (owner && owner->player && owner->player->GetGroup())
        for (auto const& slot : owner->player->GetGroup()->GetMemberSlots())
            members.insert(slot.guid.GetCounter());
    return members;
}
bool OwnerWaiting(std::uint32_t guid)
{
    return std::any_of(prepared.begin(), prepared.end(), [&](auto const& it) {
        return it.second.original.value("type",std::string{}) != "start_member_dungeon" &&
            it.second.driverBinding.is_null() && !it.second.consumed && NowUnixMs() < it.second.expires && it.second.bodies.contains(guid);
    });
}

bool Enabled()
{
    return Config().dayPlans && RealmModule::Instance().On();
}
bool CommandCurrent()
{
    auto const& bridge = LeeroyModule::Instance().Bridge();
    return LeeroyCommandSerial() && bridge.Connected() && bridge.ConnectionSerial() == LeeroyCommandSerial();
}
std::string Nonce()
{
    std::random_device entropy;
    std::string token;
    for (unsigned word = 0; word < 4; ++word)
    {
        auto value = static_cast<std::uint32_t>(entropy());
        for (unsigned n = 0; n < 8; ++n)
            token += "0123456789abcdef"[(value >> (4 * n)) & 15];
    }
    return token;
}
DayPermit* ByOrigin(std::string const& id)
{
    for (auto& [token, p] : permits)
        if (p.origin == id)
            return &p;
    return nullptr;
}
DayPermit* Tagged(Queued const& entry)
{
    if (!entry.payload.is_object())
        return nullptr;
    auto value = entry.payload.find("day_permit");
    if (value == entry.payload.end() || !value->is_string())
        return nullptr;
    auto found = permits.find(value->get_ref<std::string const&>());
    return found == permits.end() ? nullptr : &found->second;
}
void Report(DayPermit const& p)
{
    Publish(DayStateMessage(p, LeeroyWorldStartedMs()));
}
bool SameMembers(DayPermit const& p, std::vector<std::uint32_t> const& guids)
{
    return guids.size() == p.members.size() && std::is_sorted(guids.begin(), guids.end()) &&
           std::adjacent_find(guids.begin(), guids.end()) == guids.end() &&
           std::all_of(guids.begin(), guids.end(), [&](auto g) { return p.Covers(g); });
}
LeeroyCommandResult Begin(Json const& message)
{
    if (!Enabled() || !CommandCurrent())
        return {false, "day_off"};
    auto const id = message.at("id").get<std::string>();
    if (!message.contains("members") || !message["members"].is_array() || message["members"].empty() ||
        message["members"].size() > 2 || !message.contains("ends_ms") || !message["ends_ms"].is_number_unsigned())
        return {false, "day_scope"};
    std::vector<std::uint32_t> guids;
    for (auto const& g : message["members"])
    {
        if (!g.is_number_unsigned() || !g.get<std::uint64_t>() || g.get<std::uint64_t>() > UINT32_MAX)
            return {false, "day_scope"};
        guids.push_back(g.get<std::uint32_t>());
    }
    if (!std::is_sorted(guids.begin(), guids.end()) || std::adjacent_find(guids.begin(), guids.end()) != guids.end())
        return {false, "day_scope"};
    auto const activity = message.value("activity", std::string{}), place = message.value("place", std::string{});
    auto const ends = message["ends_ms"].get<std::uint64_t>(), now = static_cast<std::uint64_t>(NowUnixMs());
    if (ends <= now || ends - now > 4 * 3600000 || (activity == "duel_practice" && guids.size() != 2))
        return {false, "day_scope"};
    if (auto existing = ByOrigin(id))
    {
        if (existing->serial != LeeroyCommandSerial() || !SameMembers(*existing, guids) || existing->ends != ends ||
            existing->activity != activity || existing->place != place)
            return {false, "day_duplicate"};
        Report(*existing);
        return {true, {}};
    }
    auto& module = RealmModule::Instance();
    auto const& places = module.Places();
    auto destination = std::find_if(places.begin(), places.end(),
                                    [&](auto const& p) { return p.name == place && DayPlaceMatches(activity, p.kind); });
    if (destination == places.end())
        return {false, "day_scope"};
    DayPermit p;
    p.origin = id;
    p.serial = LeeroyCommandSerial();
    p.guild = GuildId();
    p.human = HumanGuid();
    p.epoch = module.Epoch();
    p.activity = activity;
    p.place = place;
    p.destination = *destination;
    p.ends = ends;
    std::size_t away = 0;
    for (auto const& [token, old] : permits)
        if (!old.Released())
            away += old.members.size();
    if (away + guids.size() > 20 || permits.size() >= ActionQueue::Capacity)
        return {false, "day_capacity"};
    auto* human = FindOnline(HumanGuid());
    if (Guild::HisFight(human))
        return {false, "busy:combat"};
    for (auto guid : guids)
    {
        if (!IsCast(guid) || OwnerWaiting(guid) || Guild::GuildAddAwaiting(guid) || !DayForMember(guid).empty())
            return {false, "day_scope"};
        if (auto why = AdmissionReason(guid, Purpose::Move); !why.empty())
            return {false, why};
        auto identity = Identity(guid);
        if (!identity || identity->group)
            return {false, "day_scope"};
        p.members.emplace(guid, DayPermit::Member{*identity, module.BodyGeneration(guid)});
    }
    p.token = Nonce();
    auto const token = p.token;
    auto [it, inserted] = permits.emplace(token, std::move(p));
    if (!inserted)
        return {false, "day_capacity"};
    Report(it->second);
    return {true, {}};
}
} // namespace

DayOwnerCheck::DayOwnerCheck(Json const& message, std::vector<std::uint32_t> members, std::string reason)
{
    auto const wireType = message.value("type", std::string{});
    inspectingDungeon = wireType == "member_dungeon_inspect";
    bool const dungeonDriver = wireType == "member_dungeon_prepare_driver";
    if ((inspectingDungeon || dungeonDriver) && !message.contains("day_check"))
    { error = "day_owner_scope"; return; }
    if ((!message.contains("day_check") && (message.contains("day_driver_ticket") || message.contains("dungeon_driver_ticket"))) ||
        (message.contains("day_check") && message.contains("day_prepared")))
    { error = "day_owner_scope"; return; }
    if (!message.contains("day_check") && !message.contains("day_prepared"))
        return;
    if (!CommandCurrent()) { error = "day_owner_session"; return; }
    original = message;
    original.erase("ts");
    targets = std::move(members);
    why = std::move(reason);
    auto currentRequest = [&](PreparedOwner const& saved) {
        auto& module = RealmModule::Instance();
        auto owner = Identity(HumanGuid(), true);
        if (!DayRequestMatches(saved.original, original, saved.serial, LeeroyCommandSerial(), saved.expires,
                               NowUnixMs(), saved.consumed) || saved.epoch != module.Epoch() ||
            saved.guild != GuildId() || saved.human != HumanGuid() ||
            saved.humanGeneration != module.BodyGeneration(HumanGuid()) ||
            saved.owner != owner || OwnerGroup(owner) != saved.group)
            return false;
        for (auto const& [guid, body] : saved.bodies)
        {
            auto current = Identity(guid, true);
            if (module.BodyGeneration(guid) != body.second || current.has_value() != body.first.has_value() ||
                (current && !SameLife(*current, *body.first)))
                return false;
        }
        return true;
    };
    if (message.contains("day_prepared"))
    {
        auto found = prepared.find(message.value("day_prepared", std::string{}));
        original.erase("day_prepared");
        if (found == prepared.end()) { error = "day_owner_expired"; return; }
        auto& saved = found->second;
        if (!saved.driverBinding.is_null() || !currentRequest(saved))
        { error = "day_owner_changed"; return; }
        if (!saved.tokens.empty() && DayGuildAddProgress(saved.tokens, saved.expires) != DayHandoff::Ready)
        { error = "day_cleanup_unresolved"; return; }
        if (!saved.heroic.empty() && !Guild::DungeonReturned(saved.heroic,saved.heroicMembers))
        { error = "dungeon_cleanup_unresolved"; return; }
        saved.consumed = true; // Keep the check ID fenced through its original deadline.
        return;
    }
    checking = true;
    auto wire = message.at("type").get<std::string>();
    constexpr std::string_view prefix = "day_check_";
    preparingDriver = wire == "day_prepare_offline_driver" || dungeonDriver;
    if (dungeonDriver != message.contains("dungeon_driver_ticket") ||
        (wire == "day_prepare_offline_driver") != message.contains("day_driver_ticket") ||
        (!inspectingDungeon && !preparingDriver && !wire.starts_with(prefix)))
    { error = "day_owner_scope"; return; }
    original["type"] = dungeonDriver || inspectingDungeon ? "start_member_dungeon" :
        preparingDriver ? "start_offline_raid" : wire.substr(prefix.size());
    original.erase("dungeon_driver_ticket");
    original.erase("dungeon_inspect");
    original.erase("day_driver_ticket");
    auto const& check = message.at("day_check");
    if (!check.is_object() || !check.contains("request_id") || !check["request_id"].is_string() ||
        !check.contains("origins") || !check["origins"].is_array() ||
        (check["origins"].empty() && original["type"] != "start_member_dungeon" && !check.contains("heroic_start")) ||
        check["origins"].size() > 40 || (!preparingDriver && prepared.size() >= 40))
    { error = "day_owner_scope"; return; }
    expires = check.value("expires_ms", std::int64_t{});
    auto now = NowUnixMs();
    if (expires <= now || expires > now + 35000) { error = "day_owner_expired"; return; }
    expires = std::min<std::int64_t>(expires, now + 30000);
    checkId = message.at("id").get<std::string>();
    auto requestId = check.at("request_id").get<std::string>();
    if (requestId.empty() || requestId.size() > 128 || requestId == checkId || prepared.contains(checkId))
    { error = "day_owner_scope"; return; }
    original.erase("day_check");
    original["id"] = requestId;
    std::set<std::string> origins;
    for (auto const& origin : check.at("origins"))
    {
        auto p = ByOrigin(origin.get<std::string>());
        if (!p || p->guild != GuildId() || p->human != HumanGuid() ||
            std::none_of(targets.begin(), targets.end(), [&](auto g) { return p->Covers(g); }) ||
            !origins.insert(p->origin).second)
        { error = "day_owner_scope"; return; }
        tokens.insert(p->token);
    }
    for (auto guid : targets)
    {
        auto current = DayForMember(guid);
        if (OwnerWaiting(guid) || (!current.empty() && !tokens.contains(current)))
        { error = "day_owner_scope"; return; }
    }
    if (check.contains("heroic_start"))
    {
        if (!check["heroic_start"].is_string() || (why != "raid" && why != "string" && why != "summon") ||
            check.value("owner_guid",0u) != HumanGuid() || !FindOnline(HumanGuid()) || Guild::HisFight(FindOnline(HumanGuid())))
        { error = "dungeon_owner_scope"; return; }
        heroic = check["heroic_start"].get<std::string>();
        for (auto guid : targets)
            if (!Guild::MemberDungeonFor(guid).empty())
            {
                if (!Guild::CanProjectDungeonBody(heroic,guid)) { error = "dungeon_owner_scope"; return; }
                heroicMembers.push_back(guid);
            }
        if (heroic.empty() || heroicMembers.empty()) { error = "dungeon_owner_scope"; return; }
    }
    binding = check;
    if (preparingDriver)
    {
        auto found = prepared.find(message.value(dungeonDriver ? "dungeon_driver_ticket" : "day_driver_ticket", std::string{}));
        if (found == prepared.end() || !DayDriverTicketMatches(found->second.driverBinding, binding,
                                                             found->second.tokens, tokens) ||
            !currentRequest(found->second))
        { error = "day_owner_changed"; return; }
        expires = found->second.expires;
        found->second.consumed = true;
    }
}
bool DayOwnerCheck::ProjectsDungeon(std::uint32_t guid) const
{
    return checking && !heroic.empty() && Guild::CanProjectDungeonBody(heroic,guid);
}
std::string DayOwnerCheck::Token(std::uint32_t guid) const
{
    if (checking)
        for (auto const& token : tokens)
            if (DayOwnsMember(token, guid) && DayPreviewFence(token, guid).empty())
                return token;
    return {};
}
bool DayOwnerCheck::DriverPreparationCurrent() const
{
    return preparingDriver && error.empty() && CommandCurrent() && NowUnixMs() < expires;
}
LeeroyCommandResult DayOwnerCheck::Accept(bool coldDriver) const
{
    if (!checking || preparingDriver || !error.empty() || !CommandCurrent() || NowUnixMs() >= expires)
        return {false, "day_owner_expired"};
    if (coldDriver && std::any_of(prepared.begin(), prepared.end(), [&](auto const& item) {
            return !item.second.driverBinding.is_null() && item.second.original.at("id") == original.at("id");
        }))
        return {false, "day_owner_scope"};
    auto& module = RealmModule::Instance();
    auto owner = Identity(HumanGuid(), true);
    PreparedOwner request{original, LeeroyCommandSerial(), module.Epoch(), module.BodyGeneration(HumanGuid()), GuildId(), HumanGuid(),
                          expires, owner, OwnerGroup(owner), tokens, false, coldDriver ? binding : Json(nullptr), heroic, heroicMembers, {}};
    for (auto guid : targets)
        request.bodies.emplace(guid, std::pair{Identity(guid, true), module.BodyGeneration(guid)});
    auto refusal = StoreDayOwnerRequest(prepared, checkId, std::move(request), coldDriver,
                                        [&](auto const& token) { RevokeDay(token, why); });
    if (refusal.empty() && !coldDriver)
    {
        if (why != "heroic") CancelPreparedDungeon(targets);
        if (!heroic.empty()) return Guild::StopMemberDungeon(heroic,true);
    }
    return {refusal.empty(), refusal};
}

std::string DayForMember(std::uint32_t guid)
{
    for (auto const& [token, p] : permits)
        if (!p.Released() && p.Covers(guid))
            return token;
    return {};
}
bool DayOwnsMember(std::string const& token, std::uint32_t guid)
{
    auto it = permits.find(token);
    if (it == permits.end())
        return false;
    auto& module = RealmModule::Instance();
    return it->second.Owns(guid, module.Holder(guid), Identity(guid, true), module.BodyGeneration(guid),
                           module.Epoch());
}
std::string DayPreviewFence(std::string const& token, std::uint32_t guid)
{
    auto& realm = RealmModule::Instance();
    auto operation = realm.Holder(guid);
    if (!DayOwnsMember(token, guid))
        return realm.MutationReason(guid);
    if (auto continuation = realm.FindPvpOwner(operation);
        continuation && DayFishingCleaning(*continuation, realm.GuildEpoch()))
        return realm.ValidateContinuation(continuation->token, guid, ContinuationMode::Cleanup);
    return realm.MutationReason(guid, operation);
}

Player* DayPartner(std::string const& token, std::uint32_t guid)
{
    if (!DayOwnsMember(token, guid))
        return nullptr;
    auto const& permit = permits.at(token);
    if (permit.activity != "duel_practice" || permit.members.size() != 2)
        return nullptr;
    for (auto const& [partner, member] : permit.members)
        if (partner != guid && DayOwnsMember(token, partner))
            return FindOnline(partner);
    return nullptr;
}

std::string CheckDayStep(Queued const& entry)
{
    if (!entry.payload.is_object() || !entry.payload.contains("day_permit"))
        return {};
    auto* p = Tagged(entry);
    if (!Enabled() || !CommandCurrent())
        return "day_off";
    if (!p || !p->reason.empty() || p->ends <= static_cast<std::uint64_t>(NowUnixMs()) ||
        p->serial != LeeroyCommandSerial() || p->guild != GuildId() || p->human != HumanGuid() ||
        p->epoch != RealmModule::Instance().Epoch())
        return "day_revoked";
    if (p->steps.contains(entry.id))
        return "day_duplicate";
    if (!p->Allows(entry.kind, entry.guids))
        return "day_scope";
    if (entry.kind == "realm_move" && (entry.payload.at("place") != p->place || entry.payload.value("social", false)))
        return "day_scope";
    if (entry.kind == "rpg_status" && (entry.payload.value("quest_id", 0u) ||
                                       entry.payload.at("status").get<std::string>() != DayStatusFor(p->activity)))
        return "day_scope";
    auto* human = FindOnline(HumanGuid());
    if (Guild::HisFight(human))
        return "busy:combat";
    for (auto g : entry.guids)
        if (!DayOwnsMember(p->token, g) || !RealmModule::Instance().Holder(g).empty())
            return "day_scope";
    return {};
}
std::string DayMutationRefusal(Queued const& entry)
{
    if (!entry.payload.contains("day_permit")) return {};
    auto* permit = Tagged(entry);
    if (!permit)
        return "day_unknown";
    auto& bridge = LeeroyModule::Instance().Bridge();
    if (auto why = permit->MutationRefusal(Enabled(), RealmModule::Instance().Epoch(), bridge.Connected(),
            bridge.ConnectionSerial(), NowUnixMs(), Guild::HisFight(FindOnline(HumanGuid()))); !why.empty())
        return why;
    auto const& places = RealmModule::Instance().Places();
    if (std::none_of(places.begin(), places.end(), [&](auto const& p) { return permit->LocationMatches(p); }))
        return "day_place_changed";
    Player* first = nullptr;
    for (auto const& [guid, member] : permit->members)
    {
        auto* bot = FindOnline(guid);
        if (!DayOwnsMember(permit->token, guid) || !bot || bot->GetGroup() || bot->GetOriginalGroup() ||
            bot->GetGroupInvite())
            return "busy:day_member";
        if (bot->IsInCombat() || bot->IsBeingTeleported() || bot->GetMap()->Instanceable())
            return "busy:day_member";
        if (entry.kind == "realm_move")
            continue;
        if (bot->GetMapId() != permit->destination.map ||
            bot->GetDistance(permit->destination.x, permit->destination.y, permit->destination.z) > 30.f ||
            (first && !first->IsWithinDistInMap(bot, 30.f)))
            return "day_place_changed";
        first = bot;
        auto move = std::find_if(permit->steps.begin(), permit->steps.end(), [&](auto const& s) {
            return s.second.kind == "realm_move" && s.second.succeeded && s.second.members.front() == guid;
        });
        if (move == permit->steps.end() || !move->second.destination)
            return "day_scope";
        auto const& pos = *move->second.destination;
        if (bot->GetMapId() != move->second.map || bot->GetDistance(pos[0], pos[1], pos[2]) > 30.f)
            return "day_place_changed";
    }
    return {};
}

void DayAdmitted(Queued const& entry)
{
    if (auto p = Tagged(entry))
        p->steps.emplace(entry.id, DayPermit::Step{entry.kind, entry.guids, false, false, std::nullopt, 0});
}
void DayMoveIssued(Queued const& entry, std::uint32_t map, float x, float y, float z)
{
    if (auto permit = Tagged(entry))
    {
        auto& step = permit->steps.at(entry.id);
        step.map = map;
        step.destination = std::array{x, y, z};
    }
}

bool DayMoving(std::string const& token, Player* bot)
{
    if (!bot || !bot->IsBeingTeleported())
        return false;
    auto guid = bot->GetGUID().GetCounter();
    if (!DayOwnsMember(token, guid))
        return false;
    auto const& steps = permits.at(token).steps;
    auto step = steps.find(RealmModule::Instance().Holder(guid));
    if (step == steps.end() || !step->second.destination)
        return false;
    auto const& dest = bot->GetTeleportDest();
    return step->second.InTransitTo(dest.GetMapId(), dest.GetPositionX(), dest.GetPositionY(), dest.GetPositionZ());
}

void DayFinished(Queued const& entry, bool released, bool success)
{
    auto p = Tagged(entry);
    if (!p)
        return;
    auto step = p->steps.find(entry.id);
    if (step == p->steps.end())
        return;
    step->second.released = released;
    step->second.succeeded = released && success;
    if (!released)
    {
        p->unresolved = true;
        if (p->reason.empty())
            p->reason = "action_unproved";
    }
    Report(*p);
}
bool DayRevoking(Queued const& entry)
{
    auto p = Tagged(entry);
    if (p && p->reason.empty() && Guild::HisFight(FindOnline(HumanGuid())))
        RevokeDay(p->token, "combat");
    return p && !p->reason.empty();
}
void RevokeDay(std::string const& token, std::string const& reason)
{
    auto it = permits.find(token);
    if (it == permits.end())
        return;
    auto& p = it->second;
    if (p.reason.empty())
        p.reason = reason;
    Report(p);
}
DayHandoff DayGuildAddProgress(std::set<std::string> const& tokens, std::int64_t deadline)
{
    std::vector<DayPermit const*> selected;
    for (auto const& token : tokens)
    {
        auto it = permits.find(token);
        selected.push_back(it == permits.end() ? nullptr : &it->second);
    }
    return GuildAddDayProgress(selected, NowUnixMs(), deadline);
}
void TickDayPlans()
{
    std::erase_if(prepared, [](auto const& it) { return NowUnixMs() >= it.second.expires; });
    if (permits.empty())
        return;
    auto now = static_cast<std::uint64_t>(NowUnixMs());
    auto& bridge = LeeroyModule::Instance().Bridge();
    auto* human = FindOnline(HumanGuid());
    bool const combat = Guild::HisFight(human);
    for (auto& [token, p] : permits)
        if (p.reason.empty() && (combat || !Enabled() || p.epoch != RealmModule::Instance().Epoch() ||
                                 !bridge.Connected() || p.serial != bridge.ConnectionSerial() || now >= p.ends))
            RevokeDay(token, combat ? "combat" : now >= p.ends ? "ended" : "context_changed");
    std::erase_if(permits,
                  [&](auto const& item) { return item.second.Released() && now > item.second.ends + 86400000; });
}
void InvalidateDayOwnerChecks() { prepared.clear(); }
bool HasPreparedOwner(std::uint32_t guid) { return OwnerWaiting(guid); }
void CancelPreparedDungeon(std::vector<std::uint32_t> const& picks)
{
    std::erase_if(prepared, [&](auto const& item){
        auto const& p = item.second;
        return !p.consumed && p.original.value("type",std::string{}) == "start_member_dungeon" &&
            std::any_of(picks.begin(),picks.end(),[&](auto g){return p.bodies.contains(g);});
    });
}

void RegisterDayOwnerCheck(std::string const& originalType, LeeroyCommandHandler handler)
{
    RegisterCommand("day_check_" + originalType, [handler](Json const& msg) -> LeeroyCommandResult {
        if (!msg.contains("day_check") || msg.contains("day_prepared")) return {false, "day_owner_scope"};
        return handler(msg);
    });
}

void RegisterDayPlans()
{
    RegisterCommand("day_begin", Begin);
    RegisterCommand("day_status", [](Json const& m) -> LeeroyCommandResult {
        auto p = ByOrigin(m.value("origin_id", std::string{}));
        if (!CommandCurrent() || !p || p->guild != GuildId() || p->human != HumanGuid())
            return {false, "day_unknown"};
        Report(*p);
        return {true, {}};
    });
    RegisterCommand("day_revoke", [](Json const& m) -> LeeroyCommandResult {
        auto p = ByOrigin(m.value("origin_id", std::string{}));
        if (!CommandCurrent() || !p || p->guild != GuildId() || p->human != HumanGuid())
            return {false, "day_unknown"};
        if (m.value("permit", std::string{}) != p->token || m.value("serial", std::uint64_t{}) != p->serial ||
            m.value("started_ms", std::int64_t{}) != LeeroyWorldStartedMs())
            return {false, "day_unknown"};
        auto why = m.value("reason", std::string{});
        static std::set<std::string> const reasons = {
            "his_group",     "raid",       "offline_raid",    "string",          "heroic",         "held",
            "ended",         "combat",     "context_changed", "action_unproved", "action_refused", "admission_refused",
            "holiday_ended", "changed_tie"};
        if (!reasons.contains(why))
            return {false, "day_scope"};
        RevokeDay(p->token, why);
        return {true, {}};
    });
}
} // namespace Leeroy::Realm
