// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "DayPlans.h"
#include "LeeroyDayPlan.h"
#include "RealmPorts.h"
#include "Guild/GuildAdd.h"
#include <cmath>
#include <random>

namespace Leeroy::Realm
{
namespace
{
using Json = nlohmann::json;
std::map<std::string, DayPermit> permits; // Indexed by opaque capability. World thread only.
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
    if (std::none_of(places.begin(), places.end(),
                     [&](auto const& p) { return p.name == place && DayPlaceMatches(activity, p.kind); }))
        return {false, "day_scope"};
    DayPermit p;
    p.origin = id;
    p.serial = LeeroyCommandSerial();
    p.guild = GuildId();
    p.human = HumanGuid();
    p.epoch = module.Epoch();
    p.activity = activity;
    p.place = place;
    p.ends = ends;
    std::size_t away = 0;
    for (auto const& [token, old] : permits)
        if (!old.Released())
            away += old.members.size();
    if (away + guids.size() > 20 || permits.size() >= ActionQueue::Capacity)
        return {false, "day_capacity"};
    auto* human = FindOnline(HumanGuid());
    if (human && human->IsInCombat())
        return {false, "busy:combat"};
    for (auto guid : guids)
    {
        if (!IsCast(guid) || Guild::GuildAddAwaiting(guid) || !DayForMember(guid).empty())
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
    if (human && human->IsInCombat())
        return "busy:combat";
    for (auto g : entry.guids)
        if (!DayOwnsMember(p->token, g) || !RealmModule::Instance().Holder(g).empty())
            return "day_scope";
    return {};
}
std::string DayMutationRefusal(Queued const& entry)
{
    auto* permit = Tagged(entry);
    if (!permit)
        return "day_unknown";
    auto& bridge = LeeroyModule::Instance().Bridge();
    if (!Enabled() || !permit->reason.empty() || permit->epoch != RealmModule::Instance().Epoch() ||
        !bridge.Connected() || bridge.ConnectionSerial() != permit->serial ||
        permit->ends <= static_cast<std::uint64_t>(NowUnixMs()))
        return "day_revoked";
    if (auto* human = FindOnline(HumanGuid()); human && human->IsInCombat())
        return "busy:combat";
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
        p->steps.emplace(entry.id, DayPermit::Step{entry.kind, entry.guids});
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
    if (permits.empty())
        return;
    auto now = static_cast<std::uint64_t>(NowUnixMs());
    auto& bridge = LeeroyModule::Instance().Bridge();
    auto* human = FindOnline(HumanGuid());
    bool const combat = human && human->IsInCombat();
    for (auto& [token, p] : permits)
        if (p.reason.empty() && (combat || !Enabled() || p.epoch != RealmModule::Instance().Epoch() ||
                                 !bridge.Connected() || p.serial != bridge.ConnectionSerial() || now >= p.ends))
            RevokeDay(token, combat ? "combat" : now >= p.ends ? "ended" : "context_changed");
    std::erase_if(permits,
                  [&](auto const& item) { return item.second.Released() && now > item.second.ends + 86400000; });
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
