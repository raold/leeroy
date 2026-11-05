// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "GuildAdd.h"
#include "GuildPorts.h"
#include "OfflineRaidJob.h"
#include "RaidAssembly.h"
#include "Realm/RealmModule.h"
#include "LeeroyGuildAddPolicy.h"

#include "Chat.h"
#include "ChatCommand.h"
#include "Group.h"
#include "InstanceSaveMgr.h"
#include "LFGMgr.h"
#include "Log.h"
#include "Map.h"
#include "MapMgr.h"
#include "PlayerbotAI.h"
#include "Playerbots.h"
#include "ScriptMgr.h"

#include <map>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace Leeroy::Guild
{
namespace
{
// World thread. The group his last add command filled, and the size he asked for: when he takes that group into an
// instance, the mind is told, so a raid built this way can count like a called one.
ObjectGuid builtGroup;
uint32 builtSize = 0;

GuildAddRole RoleOf(Player* player)
{
    if (PlayerbotAI::IsTank(player, true))
        return GuildAddRole::Tank;
    if (PlayerbotAI::IsHeal(player, true))
        return GuildAddRole::Healer;
    return GuildAddRole::Damage;
}

char const* RoleName(GuildAddRole role)
{
    switch (role)
    {
        case GuildAddRole::Tank: return "tank";
        case GuildAddRole::Healer: return "healer";
        default: return "damage";
    }
}

// Why he cannot take guild members into his group right now; empty when he can.
std::string HumanRefusal(Player* human)
{
    Map* map = human->GetMap();
    if (!map || human->IsBeingTeleported() || human->IsInFlight())
        return "You cannot add guild members while travelling";
    if (human->InBattleground() || human->InArena() || map->IsBattlegroundOrArena())
        return "You cannot add guild members in a battleground or arena";
    if (human->GetTransport() || human->GetVehicle())
        return "You cannot add guild members while on a transport or vehicle";
    if (!human->IsAlive())
        return "You cannot add guild members while dead";
    if (human->IsInCombat())
        return "You cannot add guild members in combat";
    if (Group* group = human->GetGroup())
    {
        if (group->isLFGGroup() || group->isBGGroup() || group->isBFGroup())
            return "You cannot add guild members to a Dungeon Finder or battleground group";
        if (!group->IsLeader(human->GetGUID()))
            return "Only your group's leader can add guild members";
        if (GroupHasRealmOwner(group))
            return "The Realm is using someone in your group: try again in a moment";
        for (auto const& slot : group->GetMemberSlots())
            if (OfflineRaidOwns(slot.guid.GetCounter()))
                return "An offline raid owns someone in your group";
    }
    return {};
}

GuildAddBody BodyOf(uint32 guid, Player* bot, Player* human, uint32 guildId)
{
    GuildAddBody body;
    if (!bot)
        return body;
    body.online = true;
    body.cast = IsCast(guid) && bot->GetGuildId() == guildId;
    // Realm leases first, then training, offline raids, pending bot jobs and a body that is not a usable bot.
    body.fence = Realm::RealmModule::Instance().MutationReason(guid);
    body.otherTeam = bot->GetTeamId(true) != human->GetTeamId(true);
    body.dead = !bot->IsAlive();
    body.inCombat = bot->IsInCombat();
    body.grouped = bot->GetGroup() || bot->GetOriginalGroup() || bot->GetGroupInvite();
    Map* map = bot->GetMap();
    body.inBattleground = bot->InBattleground() || bot->InArena() || (map && map->IsBattlegroundOrArena());
    // A raid browser listing is no commitment: joining a group drops it.
    lfg::LfgState const lfg = sLFGMgr->GetState(bot->GetGUID());
    body.queued = bot->InBattlegroundQueue() || (lfg != lfg::LFG_STATE_NONE && lfg != lfg::LFG_STATE_RAIDBROWSER);
    body.teleporting = bot->IsBeingTeleported();
    body.inFlight = bot->IsInFlight();
    body.elsewhere = !map || bot->HasPendingBind() || (map->Instanceable() && map != human->GetMap());
    Map* his = human->GetMap();
    if (his->IsDungeon())
        if (InstancePlayerBind const* bind = sInstanceSaveMgr->PlayerGetBoundInstance(bot->GetGUID(), his->GetId(),
                his->GetDifficulty()))
            body.locked = bind->perm && bind->save && bind->save->GetInstanceId() != his->GetInstanceId();
    return body;
}

// The raid night's summon, with him as the destination: beside him, in his copy of an instance only.
bool Bring(Player* bot, Player* human, std::size_t index, std::size_t count)
{
    Map* map = human->GetMap();
    if (bot->GetMap() == map && bot->IsWithinDistInMap(human, 40.0f))
        return true;
    if (map->Instanceable()
        && (sInstanceSaveMgr->PlayerGetDestinationInstanceId(bot, map->GetId(), map->GetDifficulty())
                != map->GetInstanceId()
            || sMapMgr->PlayerCannotEnter(map->GetId(), bot, false) != Map::CAN_ENTER))
        return false;
    constexpr float pi = 3.14159265358979323846f;
    float const angle = pi * (0.5f + static_cast<float>(index) / static_cast<float>(count ? count : 1));
    float x, y, z;
    human->GetClosePoint(x, y, z, bot->GetObjectSize(), 3.0f, angle);
    return bot->TeleportTo(map->GetId(), x, y, z, human->GetOrientation());
}

void PublishGuildGroup(std::string reason, Player* human, Group* group,
    std::vector<std::pair<std::uint32_t, std::string>> added)
{
    GuildGroupEvent event;
    event.reason = std::move(reason);
    event.size = builtSize;
    event.leaderGuid = human->GetGUID().GetCounter();
    event.raid = group->isRaidGroup();
    event.map = human->GetMapId();
    event.instanceId = human->GetInstanceId();
    event.difficulty = static_cast<std::uint8_t>(human->GetMap()->GetDifficulty());
    event.added = std::move(added);
    for (auto const& slot : group->GetMemberSlots())
        if (uint32 const guid = slot.guid.GetCounter(); guid != event.leaderGuid && IsCast(guid))
            event.members.push_back(guid);
    LeeroyModule::Instance().Publish(EncodeEvent(NowUnixMs(), MakeGuildGroupEvent(event)), false);
}

std::vector<std::string> Fill(Player* human, uint32 size)
{
    if (auto why = HumanRefusal(human); !why.empty())
        return {why};

    Group* group = human->GetGroup();
    std::vector<GuildAddMember> connected;
    uint32 members = 1;
    if (group)
    {
        members = group->GetMembersCount();
        for (auto const& slot : group->GetMemberSlots())
            if (Player* member = ObjectAccessor::FindConnectedPlayer(slot.guid))
                connected.push_back({RoleOf(member), member->getClass()});
    }
    else
        connected.push_back({RoleOf(human), human->getClass()});

    uint32 const guildId = LeeroyModule::Instance().GuildId();
    std::vector<GuildAddCandidate> candidates;
    std::map<std::string, std::uint32_t> unavailable;
    for (uint32 guid : CastMembers())
    {
        if (guid == HumanGuid())
            continue;
        Player* bot = FindOnline(guid);
        if (auto reason = GuildAddSkipReason(BodyOf(guid, bot, human, guildId)); !reason.empty())
        {
            ++unavailable[reason];
            continue;
        }
        candidates.push_back({guid, RoleOf(bot), bot->getClass()});
    }

    auto const plan = PlanGuildAdd(size, members, connected, std::move(candidates));
    GuildAddCounts added{};
    std::uint32_t notBrought = 0;
    std::vector<std::pair<std::uint32_t, std::string>> joined;
    if (!plan.full && !plan.picks.empty())
    {
        group = LeaderGroup(human);
        if (!group)
            return {"Your group could not be created"};
        if (!group->isRaidGroup() && members + plan.picks.size() > static_cast<std::size_t>(MAXGROUPSIZE))
            group->ConvertToRaid();
        Map* map = human->GetMap();
        std::vector<Player*> arrivals;
        for (auto const& pick : plan.picks)
        {
            // Every fence again at the mutation boundary: each join runs group hooks.
            Player* bot = FindOnline(pick.guid);
            if (!bot || GroupHasRealmOwner(group) || group->IsFull()
                || !GuildAddSkipReason(BodyOf(pick.guid, bot, human, guildId)).empty() || !group->AddMember(bot))
                continue;
            RaidHabits(bot, human);
            if (map->IsDungeon())
                GrantRaidEntry(bot, map->GetId(), map->GetDifficulty());
            ++added[static_cast<std::size_t>(pick.role)];
            joined.emplace_back(pick.guid, RoleName(pick.role));
            arrivals.push_back(bot);
        }
        for (std::size_t i = 0; i < arrivals.size(); ++i)
            if (!Bring(arrivals[i], human, i, arrivals.size()))
                ++notBrought;
    }

    if (!joined.empty())
    {
        builtGroup = group->GetGUID();
        builtSize = size;
        PublishGuildGroup("built", human, group, joined);
    }
    LOG_INFO("module.leeroy", "mod-leeroy: add{} from the guild: {} tanks, {} healers, {} damage joined, {} not brought",
        size, added[0], added[1], added[2], notBrought);
    return GuildAddMessages(plan, added, notBrought, unavailable);
}

class GuildAddCommandScript : public AllCommandScript
{
public:
    GuildAddCommandScript() : AllCommandScript("LeeroyGuildAddCommandScript", {ALLCOMMANDHOOK_ON_TRY_EXECUTE_COMMAND}) { }

    bool OnTryExecuteCommand(ChatHandler& handler, std::string_view command) override
    {
        Player* invoker = handler.IsConsole() ? nullptr : handler.GetPlayer();
        LeeroyModule& module = LeeroyModule::Instance();
        GuildAddGate gate;
        gate.console = handler.IsConsole();
        gate.enabled = module.Enabled();
        gate.invoker = invoker ? invoker->GetGUID().GetCounter() : 0;
        gate.human = module.HumanGuid();
        gate.guild = module.GuildId();
        if (!gate.console && gate.invoker && gate.invoker == gate.human)
            gate.cast = module.Cast().Guids().size();
        if (!GuildAddIntercepts(gate))
            return true;
        // The core's own resolution of what he typed: its abbreviations, case rules and permissions.
        uint32 const size = ParseGuildAddCommand(Acore::ChatCommands::GetAutoCompletionsFor(handler, command));
        if (!size)
            return true;
        for (auto const& message : Fill(invoker, size))
            handler.PSendSysMessage("{}", message);
        return false; // Never playerbots' addclass bots for him.
    }
};

class GuildAddPlayerScript : public PlayerScript
{
public:
    GuildAddPlayerScript() : PlayerScript("LeeroyGuildAddPlayerScript", {PLAYERHOOK_ON_MAP_CHANGED}) { }

    void OnPlayerMapChanged(Player* player) override
    {
        if (!player || builtGroup.IsEmpty() || !GuildEnabled() || player->GetGUID().GetCounter() != HumanGuid())
            return;
        Group* group = player->GetGroup();
        Map* map = player->GetMap();
        if (group && group->GetGUID() == builtGroup && group->IsLeader(player->GetGUID()) && map && map->IsDungeon())
            PublishGuildGroup("entered", player, group, {});
    }
};
}

void AddGuildAddScripts()
{
    new GuildAddCommandScript();
    new GuildAddPlayerScript();
}
}
