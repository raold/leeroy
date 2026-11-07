// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "GuildAdd.h"
#include "GuildPorts.h"
#include "OfflineRaidJob.h"
#include "RaidAssembly.h"
#include "Realm/RealmModule.h"
#include "Realm/RealmPorts.h"
#include "LeeroyGuildAddPolicy.h"

#include "AiFactory.h"
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

#include <cmath>
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

// The talent tree with most points, and the average item level in whole levels, as the pick weighs them.
std::int8_t TabOf(Player* player)
{
    return static_cast<std::int8_t>(AiFactory::GetPlayerSpecTab(player));
}

std::uint32_t GearOf(Player* player)
{
    return static_cast<std::uint32_t>(std::lround(player->GetAverageItemLevel()));
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

// The instance copy he stands in, for the size fences: none outside an instance.
GuildAddPlace PlaceOf(Map* map)
{
    InstanceMap* instance = map ? map->ToInstanceMap() : nullptr;
    return instance ? GuildAddPlace{instance->GetMaxPlayers(), instance->IsRaid()} : GuildAddPlace{};
}

// hisGroup: a member of the group he leads, whom only the add command's other fences can keep out.
GuildAddBody BodyOf(uint32 guid, Player* bot, Player* human, uint32 guildId, bool hisGroup = false)
{
    GuildAddBody body;
    if (!bot)
        return body;
    body.online = true;
    body.cast = IsCast(guid) && bot->GetGuildId() == guildId;
    if (!hisGroup)
    {
        // Realm leases first, then training, offline raids, pending bot jobs and a body that is not a usable bot.
        body.fence = Realm::RealmModule::Instance().MutationReason(guid);
        body.grouped = bot->GetGroup() || bot->GetOriginalGroup() || bot->GetGroupInvite();
    }
    else
    {
        // The same fences without the one that holds his own group's members away from the Realm.
        body.fence = Realm::RealmOwns(guid) ? "busy:realm_owner" : Realm::NativeOwnershipReason(guid);
        if (body.fence.empty() && (!GET_PLAYERBOT_AI(bot) || IsSelfBot(bot) || !Realm::IsOnRandomBotAccount(guid)))
            body.fence = "not_bot";
        body.grouped = bot->GetGroup() != human->GetGroup();
    }
    body.otherTeam = bot->GetTeamId(true) != human->GetTeamId(true);
    body.dead = !bot->IsAlive();
    body.inCombat = bot->IsInCombat();
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

// He took the group he built onto an instance map: what a called raid's formation grants (dungeon-clear's entry,
// which only writes what a member lacks) and its summon does (his copy only), for the guild members of that group
// who are not in his copy yet, under the add command's fences.
void BringIn(Player* human, Group* group)
{
    if (!human->IsAlive() || GroupHasRealmOwner(group))
        return;
    Map* map = human->GetMap();
    uint32 const guildId = LeeroyModule::Instance().GuildId();
    std::vector<Player*> arrivals;
    for (auto const& slot : group->GetMemberSlots())
    {
        uint32 const guid = slot.guid.GetCounter();
        Player* bot = guid == HumanGuid() ? nullptr : FindOnline(guid);
        if (!bot || bot->GetMap() == map || !GuildAddSkipReason(BodyOf(guid, bot, human, guildId, true)).empty())
            continue;
        GrantRaidEntry(bot, map->GetId(), map->GetDifficulty());
        arrivals.push_back(bot);
    }
    for (std::size_t i = 0; i < arrivals.size(); ++i)
        Bring(arrivals[i], human, i, arrivals.size());
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
    std::string why = HumanRefusal(human);
    // A size his copy cannot hold: refused before his group changes, since the core would turn the rest away.
    if (why.empty())
        why = GuildAddSizeRefusal(size, PlaceOf(human->GetMap()));
    if (!why.empty())
    {
        LOG_INFO("module.leeroy", "mod-leeroy: add{} from the guild refused: {}", size, why);
        return {why};
    }

    Group* group = human->GetGroup();
    std::vector<GuildAddMember> connected;
    uint32 members = 1;
    if (group)
    {
        members = group->GetMembersCount();
        for (auto const& slot : group->GetMemberSlots())
            if (Player* member = ObjectAccessor::FindConnectedPlayer(slot.guid))
                connected.push_back({RoleOf(member), member->getClass(), TabOf(member)});
    }
    else
        connected.push_back({RoleOf(human), human->getClass(), TabOf(human)});

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
        candidates.push_back({guid, RoleOf(bot), bot->getClass(), TabOf(bot), GearOf(bot)});
    }

    auto const plan = PlanGuildAdd(size, members, connected, std::move(candidates));
    GuildAddCounts added{};
    GuildAddBuffs covered = plan.covered;
    std::uint32_t notBrought = 0;
    std::vector<std::pair<std::uint32_t, std::string>> joined;
    if (!plan.full && !plan.picks.empty())
    {
        bool const created = !group;
        Map* map = human->GetMap();
        std::vector<Player*> arrivals;
        for (auto const& pick : plan.picks)
        {
            // Every fence again at the mutation boundary: each join runs group hooks.
            Player* bot = FindOnline(pick.guid);
            if (!bot || !GuildAddSkipReason(BodyOf(pick.guid, bot, human, guildId)).empty())
                continue;
            // His group only once someone can join it, and a raid only once a party is full.
            if (!group && !(group = LeaderGroup(human)))
                break;
            if (GroupHasRealmOwner(group))
                break;
            if (!group->isRaidGroup() && group->GetMembersCount() >= MAXGROUPSIZE)
                group->ConvertToRaid();
            if (group->IsFull() || !group->AddMember(bot))
                continue;
            RaidHabits(bot, human);
            if (map->IsDungeon())
                GrantRaidEntry(bot, map->GetId(), map->GetDifficulty());
            ++added[static_cast<std::size_t>(pick.role)];
            covered |= pick.buffs;
            joined.emplace_back(pick.guid, RoleName(pick.role));
            arrivals.push_back(bot);
        }
        for (std::size_t i = 0; i < arrivals.size(); ++i)
            if (!Bring(arrivals[i], human, i, arrivals.size()))
                ++notBrought;
        // Nobody joined the group made for them: never leave him in a group of one.
        if (created && group && arrivals.empty())
        {
            group->Disband();
            group = nullptr;
        }
    }

    if (!joined.empty())
    {
        builtGroup = group->GetGUID();
        builtSize = size;
        PublishGuildGroup("built", human, group, joined);
    }
    LOG_INFO("module.leeroy", "mod-leeroy: add{} from the guild: {} tanks, {} healers, {} damage joined, {} not brought",
        size, added[0], added[1], added[2], notBrought);
    auto messages = GuildAddMessages(plan, added, notBrought, unavailable);
    if (auto missing = GuildAddMissingLine(covered); !missing.empty())
        messages.push_back(std::move(missing));
    return messages;
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
        {
            // A group bigger than this copy is not brought in: the core would send all but the first few home.
            if (auto why = GuildAddEntryRefusal(group->GetMembersCount(), PlaceOf(map)); !why.empty())
                ChatHandler(player->GetSession()).PSendSysMessage("{}", why);
            else
                BringIn(player, group);
            PublishGuildGroup("entered", player, group, {});
        }
    }
};
}

void AddGuildAddScripts()
{
    new GuildAddCommandScript();
    new GuildAddPlayerScript();
}
}
