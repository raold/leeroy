// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyMemberDungeon.h"
#include "LeeroyOfflineRaidPolicy.h"
#include <algorithm>
#include <cmath>
#include <set>

namespace Leeroy::Guild
{
std::string MemberDungeonAdmission(std::uint32_t map, std::uint32_t leader,
    std::vector<DungeonCandidate> const& candidates, bool ownerOnline, bool slotBusy)
{
    if (map != MemberDungeonMap) return "unsupported_dungeon";
    if (ownerOnline) return "human_online";
    if (slotBusy) return "job_busy";
    if (candidates.size() != 5) return "wrong_composition";
    std::set<std::uint32_t> distinct;
    GuildAddCounts counts{};
    bool tankLeads = false;
    for (auto const& member : candidates)
    {
        if (!member.refusal.empty()) return member.refusal;
        if (!member.guid || !distinct.insert(member.guid).second) return "wrong_composition";
        ++counts.at(static_cast<std::size_t>(member.role));
        if (member.guid == leader) tankLeads = member.role == GuildAddRole::Tank;
    }
    return tankLeads && counts == *GuildAddComposition(5) ? "" : "wrong_composition";
}

namespace
{
using Json = nlohmann::json;
bool UInt(Json const& value, std::uint64_t max = UINT32_MAX)
{
    return value.is_number_integer() && value >= 0 && value.get<std::uint64_t>() <= max;
}
bool GuidValue(Json const& value) { return UInt(value) && value != 0; }
bool CleanupFailureTerminal(Json const& data)
{
    auto const& terminal = data.at("terminal");
    return terminal.is_object() && terminal.at("state")=="failed" &&
        terminal.at("reason").get_ref<std::string const&>().starts_with("cleanup_failed:");
}
}

std::vector<std::uint32_t> DungeonKnownRoster(Json const& data)
{
    if (!data.contains("roster") || !data.at("roster").is_array() || data.at("roster").size()!=5) return {};
    std::vector<std::uint32_t> roster;
    for (auto const& g : data.at("roster"))
    {
        if (!GuidValue(g) || std::find(roster.begin(),roster.end(),g)!=roster.end()) return {};
        roster.push_back(g.get<std::uint32_t>());
    }
    return roster;
}

bool ValidDungeonReceipt(nlohmann::json const& data)
{
    try
    {
        if (!UInt(data.at("version")) || !UInt(data.at("map")) || !UInt(data.at("difficulty")) ||
            data.at("version") != 1 || data.at("map") != MemberDungeonMap || data.at("difficulty") != 1 ||
            !data.at("stage").is_string() || !data.at("slot").is_boolean() || !data.at("settled").is_boolean() ||
            !data.at("roster").is_array() ||
            !(data.at("terminal").is_null() || data.at("terminal").is_object())) return false;
        if (!GuidValue(data.at("guild")) || !GuidValue(data.at("human")) || !GuidValue(data.at("leader")) ||
            !UInt(data.at("group")) || !UInt(data.at("instance")) || !UInt(data.at("issued_ms"),INT64_MAX)) return false;
        std::set<std::string> const stages{"accepted","grouping","teleporting","starting","running","restarting","tearing_down"};
        if (!stages.contains(data.at("stage").get<std::string>())) return false;
        for (auto key : {"recovery_ms","terminal_ms","failed_ms","failure_verified_ms"})
            if (data.contains(key) && !UInt(data.at(key),INT64_MAX)) return false;
        for (auto key : {"actor","driver","kills","wipes"})
            if (data.contains(key) && !UInt(data.at(key),key==std::string_view("kills") ? 4 : UINT32_MAX)) return false;
        if ((data.contains("recovery_authorized") && !data.at("recovery_authorized").is_boolean()) ||
            (data.contains("effect") && (!data.at("effect").is_string() || data.at("effect").get_ref<std::string const&>().size()>32))) return false;
        if (data.contains("return_scope") && !DungeonReturnAuthorized(data)) return false;
        auto const& terminal = data.at("terminal");
        if (!terminal.is_null())
        {
            if (terminal.at("kind")!="member_dungeon" || terminal.at("type")!="job" ||
                (terminal.at("state")!="done" && terminal.at("state")!="failed") || !terminal.at("id").is_string() ||
                !terminal.at("reason").is_string() || terminal.at("guids")!=data.at("roster") ||
                !UInt(terminal.at("kills"),4) || !UInt(terminal.at("wipes")) || !UInt(terminal.at("instance_id")) ||
                (terminal.at("instance_id")!=0 && terminal.at("instance_id")!=data.at("instance"))) return false;
        }
        if (data.contains("failure_verified_ms") &&
            (!CleanupFailureTerminal(data) || data.at("failure_verified_ms")==0 ||
                data.at("failure_verified_ms")<data.value("terminal_ms",std::int64_t{0}))) return false;
        auto roster = DungeonKnownRoster(data);
        std::set<std::uint32_t> ids(roster.begin(),roster.end());
        if (roster.size()!=5 || ids.contains(data.at("human").get<std::uint32_t>()) || data.at("leader")!=roster.front()) return false;
        auto const& bodies = data.at("bodies");
        if (!bodies.is_array() || (!bodies.empty() && bodies.size() != 5)) return false;
        for (auto const& body : bodies)
        {
            if (!GuidValue(body.at("guid")) || !ids.erase(body.at("guid").get<std::uint32_t>()) || !body.at("leader").is_boolean() ||
                !body.at("changed").is_boolean() || !body.at("moved").is_boolean() || !body.at("returned").is_boolean() ||
                !body.at("abandoned").is_boolean() || !UInt(body.at("map")) ||
                !UInt(body.at("dungeon"),1) || !UInt(body.at("raid"),3)) return false;
            for (auto field : {"x","y","z","o"})
                if (!body.at(field).is_number() || !std::isfinite(body.at(field).get<float>())) return false;
        }
        return true;
    }
    catch (nlohmann::json::exception const&) { return false; }
}

bool DungeonStoredEvidence(std::string const& id, std::uint64_t revision, std::uint64_t slot,
    std::uint64_t settled, Json const& data)
{
    if (!revision || slot>1 || settled>1 || !ValidDungeonReceipt(data) ||
        data.at("slot")!=(slot==1) || data.at("settled")!=(settled==1)) return false;
    auto const& terminal = data.at("terminal");
    if (terminal.is_null()) return slot && !settled;
    return terminal.at("id")==id && !(slot && settled);
}

Json DungeonTerminalUpdate(Json const& verified, Json const& firstTerminal, bool returned, std::int64_t now)
{
    auto next = verified;
    if (verified.at("terminal").is_null() && firstTerminal.is_object())
    {
        next["terminal"] = firstTerminal;
        next["terminal_ms"] = now;
    }
    // This clock can only be installed by a second write, after the first failure passed exact-ID readback.
    // Older receipts' failed_ms was prepared before verification and is not that proof.
    if (!returned && !verified.contains("failure_verified_ms") && CleanupFailureTerminal(verified) &&
        now>0 && now>=verified.value("terminal_ms",std::int64_t{0})) next["failure_verified_ms"] = now;
    next["settled"] = returned;
    if (returned) next["slot"] = false;
    return next;
}

std::optional<std::uint64_t> DungeonFailureAge(Json const& verified, std::int64_t now)
{
    if (verified.at("settled")==true || !verified.contains("failure_verified_ms") || !CleanupFailureTerminal(verified))
        return std::nullopt;
    auto at = verified.at("failure_verified_ms").get<std::int64_t>();
    return now>=at ? std::uint64_t(now-at) : 0;
}

bool DungeonWriteAllowed(std::string const& id, std::uint64_t revision, Json const& verified, Json const& next, std::int64_t now)
{
    if (!revision || revision==UINT64_MAX || !next.contains("slot") || !next.at("slot").is_boolean() ||
        !next.contains("settled") || !next.at("settled").is_boolean() ||
        !DungeonStoredEvidence(id,revision+1,next.at("slot").get<bool>(),next.at("settled").get<bool>(),next)) return false;
    if (!verified.at("terminal").is_null() && next.at("terminal")!=verified.at("terminal")) return false;
    for (auto stamp : {"terminal_ms","failure_verified_ms"})
        if (verified.contains(stamp) && (!next.contains(stamp) || next.at(stamp)!=verified.at(stamp))) return false;
    if (next.contains("failure_verified_ms") && !verified.contains("failure_verified_ms") &&
        (!CleanupFailureTerminal(verified) || next.at("failure_verified_ms")!=now)) return false;
    if ((verified.at("settled")==true && next.at("settled")==false) ||
        (verified.at("slot")==false && next.at("slot")==true)) return false;
    if (verified.at("slot")==true && next.at("slot")==false && next.at("settled")==false)
    {
        auto age = DungeonFailureAge(verified,now);
        return age && DungeonCanReleaseSlot(true,*age,false);
    }
    return true;
}

DungeonLocation DungeonLocationOf(Json const& body, std::uint32_t savedInstance, std::uint32_t map,
    std::uint32_t instance, float x, float y, float z)
{
    if (savedInstance && instance==savedInstance && map==MemberDungeonMap) return DungeonLocation::Copy;
    if (!instance && map==body.at("map") && std::hypot(x-body.at("x").get<float>(),y-body.at("y").get<float>())<=5 &&
        std::abs(z-body.at("z").get<float>())<=5) return DungeonLocation::Origin;
    return DungeonLocation::Foreign;
}

DungeonWriteRead DungeonWriteProof::Observe(std::uint64_t seenRevision, Json const* seen) const
{
    if (!seen) return DungeonWriteRead::Unavailable;
    if (seenRevision==revision+1 && *seen==after) return DungeonWriteRead::Committed;
    if (seenRevision==revision && *seen==before) return DungeonWriteRead::Previous;
    return DungeonWriteRead::Conflict;
}

bool DungeonBeforeMutation(nlohmann::json const& data)
{
    auto stage = data.value("stage",std::string{});
    return data.at("bodies").empty() && !data.value("group",0u) && !data.value("instance",0u) &&
        (stage == "accepted" || (stage == "grouping" && data.value("effect",std::string{}) == "observe"));
}

void ProjectDungeonAddBody(GuildAddBody& body, bool exactOwner)
{
    if (!exactOwner || body.fence!="busy:training_owner") return;
    body.fence.clear();
    body.grouped = body.elsewhere = body.inCombat = body.dead = body.teleporting = false;
}

bool DungeonEntrySource(bool exactOrigin, bool fighting, bool owned)
{
    return exactOrigin && !fighting && owned;
}

bool DungeonHandback(bool changed, bool proved, bool abandoned)
{
    return !changed || (proved && !abandoned);
}

bool DungeonMatchesGroup(Json const& data, std::uint32_t group, std::uint32_t leader,
    std::vector<std::uint32_t> const& members)
{
    auto roster = DungeonKnownRoster(data);
    return group && group==data.at("group") && leader==data.at("leader") && !members.empty() &&
        std::all_of(members.begin(),members.end(),[&](auto g) { return std::find(roster.begin(),roster.end(),g)!=roster.end(); });
}

bool DungeonCanReleaseSlot(bool failed, std::uint64_t sinceReport, bool held)
{
    return failed && sinceReport >= OfflineSlotReleaseMs && !held;
}

void AuthorizeDungeonReturn(nlohmann::json& data)
{
    data["recovery_authorized"] = true;
    data["return_scope"] = {{"group",data.at("group")},{"instance",data.at("instance")},
        {"leader",data.at("leader")},{"roster",data.at("roster")}};
}

bool DungeonReturnAuthorized(nlohmann::json const& data)
{
    if (!data.value("recovery_authorized",false) || !data.value("group",0u) || data.at("bodies").size()!=5) return false;
    // Older version-1 receipts set this flag only on proved recovery disband of the recorded copy.
    if (!data.contains("return_scope")) return data.value("instance",0u) != 0;
    auto const& scope = data.at("return_scope");
    return scope.at("group")==data.at("group") && scope.at("instance")==data.at("instance") &&
        scope.at("leader")==data.at("leader") && scope.at("roster")==data.at("roster");
}

bool MemberDungeonPreempts(bool admitted, std::vector<std::uint32_t> const& requested,
    std::vector<std::uint32_t> const& participants)
{
    return admitted && std::any_of(requested.begin(), requested.end(), [&](auto guid) {
        return std::find(participants.begin(), participants.end(), guid) != participants.end();
    });
}
}
