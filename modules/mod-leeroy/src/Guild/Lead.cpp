// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "Lead.h"
#include "GuildPorts.h"
#include "LeeroyInviteSpots.h"
#include "LeeroyLead.h"
#include "Realm/RealmModule.h"
#include "Realm/RealmPorts.h"

#include "AiObjectContext.h"
#include "Config.h"
#include "Group.h"
#include "LFGMgr.h"
#include "Log.h"
#include "Map.h"
#include "MapMgr.h"
#include "MotionMaster.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "PlayerbotAIConfig.h"
#include "Playerbots.h"
#include "PositionValue.h"
#include "Timer.h"
#include "TravelMgr.h"
#include "WorldSession.h"

#include <chrono>
#include <optional>
#include <string>
#include <utility>
#include <vector>

// His "you lead" in the open world (lead_do): she takes his group's lead, the others of his group follow her, and he
// follows her himself. Logs carry guids, the activity and codes only.

namespace Leeroy::Guild
{
namespace
{
static_assert(static_cast<int>(LeadRpg::Idle) == RPG_IDLE && static_cast<int>(LeadRpg::GoGrind) == RPG_GO_GRIND
    && static_cast<int>(LeadRpg::GoCamp) == RPG_GO_CAMP && static_cast<int>(LeadRpg::WanderRandom) == RPG_WANDER_RANDOM
    && static_cast<int>(LeadRpg::WanderNpc) == RPG_WANDER_NPC && static_cast<int>(LeadRpg::DoQuest) == RPG_DO_QUEST
    && static_cast<int>(LeadRpg::TravelFlight) == RPG_TRAVEL_FLIGHT && static_cast<int>(LeadRpg::Rest) == RPG_REST
    && static_cast<int>(LeadRpg::OutdoorPvp) == RPG_OUTDOOR_PVP && static_cast<int>(LeadRpg::DoGather) == RPG_DO_GATHER,
    "LeadRpg mirrors playerbots' NewRpgStatus");

bool enabled = false;   // Leeroy.Lead.Enable, world thread
std::uint32_t untilTick = 0;

std::uint64_t SteadyMs()
{
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count());
}

LeeroyCommandResult Fail(std::string reason) { return {false, std::move(reason)}; }

// One life of a body: only that same life is ever written back (a relog or an AI replacement is never touched).
struct Body
{
    Player* player = nullptr;
    PlayerbotAI* ai = nullptr;
    WorldSession* session = nullptr;
};

Body BodyOf(Player* player) { return {player, GET_PLAYERBOT_AI(player), player->GetSession()}; }

bool Same(Body const& body, Player* player)
{
    return player && body.player == player && body.ai == GET_PLAYERBOT_AI(player) && body.session == player->GetSession();
}

// Connected, also between maps (a far teleport takes a body out of the world for a moment).
Player* Connected(std::uint32_t guid)
{
    Player* player = ObjectAccessor::FindConnectedPlayer(ObjectGuid::Create<HighGuid::Player>(guid));
    return player && player->GetSession() ? player : nullptr;
}

bool InWorld(Player* player) { return player && player->IsInWorld() && !player->IsDuringRemoveFromWorld(); }

struct Follower
{
    std::uint32_t guid = 0;
    Body body;
};

// The lead in force (one at a time: his group has one lead), with what it changed.
struct Lead
{
    std::string id;                  // the start command's id: the ended event carries it
    std::uint32_t guid = 0;          // her
    Body body;
    std::uint32_t guild = 0, human = 0;
    LeadState state;
    int spot = -1;                   // travel: the kInviteSpots index she walks to
    bool removedFollow = false;      // she was following him: given back at the end
    std::vector<std::string> added;  // the strategies we added (non-combat), taken back at the end
    std::vector<Follower> followers; // the others of his group whose master we made her
    std::uint32_t lastMap = 0;
    float lastX = 0.0f, lastY = 0.0f, lastZ = 0.0f;
    bool hasLast = false;
};
std::optional<Lead> active;   // world thread

// The activity's playerbots strategies (non-combat); the first is the one a reset would take away.
std::vector<std::string> Strategies(LeadActivity activity)
{
    switch (activity)
    {
        case LeadActivity::Quest: return {"new rpg", "grind"};
        case LeadActivity::Grind: return {"grind", "new rpg"};
        case LeadActivity::Travel: return {"new rpg"};
    }
    return {"new rpg"};
}

WorldPosition SpotPosition(InviteSpot const& spot) { return WorldPosition(spot.map, spot.x, spot.y, spot.z, spot.o); }

InviteSpot const* SpotOf(Lead const& lead)
{
    return lead.spot >= 0 && lead.spot < static_cast<int>(kInviteSpots.size())
        ? &kInviteSpots[static_cast<std::size_t>(lead.spot)] : nullptr;
}

// The ownership fences of a member of his group: every one but the one that holds his group for him (busy:raid_owner),
// as his strings and his add command treat her; anyone else under the Realm's MutationReason.
std::string Fence(std::uint32_t guid, Player* bot, Player* human)
{
    Group* group = bot->GetGroup();
    bool const his = human && group && group->IsMember(human->GetGUID());
    if (!his)
        return Realm::RealmModule::Instance().MutationReason(guid);
    if (Realm::RealmOwns(guid))
        return "busy:realm_owner";
    return Realm::NativeOwnershipReason(guid);
}

bool Travelling(Player* player)
{
    return player->IsBeingTeleported() || player->IsInFlight() || player->GetTransport() || player->GetVehicle();
}

bool Battleground(Player* player)
{
    Map* map = player->GetMap();
    return player->InBattleground() || player->InArena() || (map && map->IsBattlegroundOrArena());
}

bool Dungeon(Player* player) { return InWorld(player) && player->GetMap() && player->GetMap()->IsDungeon(); }

// Anyone of the group on `at`'s map, `at` included, in combat.
bool GroupFight(Player* at)
{
    if (at->IsInCombat())
        return true;
    if (Group* group = at->GetGroup())
        for (GroupReference* ref = group->GetFirstMember(); ref; ref = ref->next())
            if (Player* member = ref->GetSource(); member && member->GetMap() == at->GetMap() && member->IsInCombat())
                return true;
    return false;
}

void Remember(Lead& lead, Player* bot)
{
    lead.lastMap = bot->GetMapId();
    lead.lastX = bot->GetPositionX();
    lead.lastY = bot->GetPositionY();
    lead.lastZ = bot->GetPositionZ();
    lead.hasLast = true;
}

LeadFacts ReadFacts(LeadDoCmd const& cmd, Player* human, Player* bot)
{
    LeadFacts f;
    LeeroyModule& module = LeeroyModule::Instance();
    f.enabled = module.Enabled() && enabled;
    if (human)
    {
        f.humanOnline = true;
        f.humanAlive = human->IsAlive();
        f.humanCombat = GroupFight(human);
        f.humanTravelling = Travelling(human);
        f.humanInstance = !human->GetMap() || human->GetMap()->IsDungeon();
        f.humanBattleground = Battleground(human);
    }
    std::uint32_t const guid = cmd.guid;
    f.cast = guid != HumanGuid() && IsCast(guid) && Realm::IsPinned(guid) && Realm::IsOnRandomBotAccount(guid)
        && (!bot || (bot->GetGuildId() == module.GuildId() && GET_PLAYERBOT_AI(bot) && !IsSelfBot(bot)));
    if (!bot || !human || !f.cast)
        return f;
    PlayerbotAI* ai = GET_PLAYERBOT_AI(bot);
    f.online = true;
    f.fence = Fence(guid, bot, human);
    f.dead = !bot->IsAlive();
    f.combat = bot->IsInCombat();
    f.travelling = Travelling(bot);
    f.instance = !bot->GetMap() || bot->GetMap()->IsDungeon() || bot->HasPendingBind();
    f.battleground = Battleground(bot);
    lfg::LfgState const lfg = sLFGMgr->GetState(bot->GetGUID());
    f.queued = bot->InBattlegroundQueue() || (lfg != lfg::LFG_STATE_NONE && lfg != lfg::LFG_STATE_RAIDBROWSER);
    Group* his = human->GetGroup();
    f.hisGroup = his && his->IsMember(bot->GetGUID());
    f.leader = his && his->IsLeader(human->GetGUID());
    f.groupSpecial = his && (his->isLFGGroup() || his->isBGGroup() || his->isBFGroup());
    f.sameMap = bot->GetMap() == human->GetMap();
    f.distance = f.sameMap ? bot->GetDistance(human) : 0.0f;
    for (std::string const& name : Strategies(cmd.activity))
        f.supported = f.supported && ai->GetAiObjectContext()->GetStrategy(name) != nullptr;
    if (cmd.activity == LeadActivity::Travel)
        if (InviteSpot const* spot = FindInviteSpot(cmd.spot))
        {
            f.spotValid = MapMgr::IsValidMapCoord(spot->map, spot->x, spot->y, spot->z, spot->o)
                && sMapMgr->GetZoneId(PHASEMASK_NORMAL, spot->map, spot->x, spot->y, spot->z) == spot->zone;
            f.spotSameMap = bot->GetMapId() == spot->map;
            f.spotDistance = f.spotSameMap ? bot->GetDistance(spot->x, spot->y, spot->z) : 0.0f;
        }
    return f;
}

// Her activity's strategies off (the wait), or on again; nothing else of hers is touched.
void SetActivity(Lead& lead, bool on)
{
    PlayerbotAI* ai = lead.body.ai;
    for (std::string const& name : Strategies(lead.state.activity))
        if (ai->HasStrategy(name, BOT_STATE_NON_COMBAT) != on)
            ai->ChangeStrategy((on ? "+" : "-") + name, BOT_STATE_NON_COMBAT);
}

// Where new rpg takes her: questing picks afresh, grinding wanders and grinds nearby, travel walks to the spot.
void Aim(Lead& lead)
{
    NewRpgInfo& info = lead.body.ai->rpgInfo;
    switch (lead.state.activity)
    {
        case LeadActivity::Quest: info.ChangeToIdle(); break;
        case LeadActivity::Grind: info.ChangeToWanderRandom(); break;
        case LeadActivity::Travel:
            if (InviteSpot const* spot = SpotOf(lead))
                info.ChangeToGoCamp(SpotPosition(*spot));
            break;
    }
}

// The lead's end: the others follow him again (while she is still their master), then she is given back what the lead
// took (her master, his follow) and loses what it added. Published as the "lead" job, ended, with its why.
void EndLead(std::string_view why, bool publish = true)
{
    if (!active)
        return;
    Lead lead = std::move(*active);
    active.reset();
    Player* bot = Connected(lead.guid);
    bool const same = Same(lead.body, bot);
    Player* human = Connected(HumanGuid());
    std::vector<std::uint32_t> back;
    for (Follower const& follower : lead.followers)
    {
        Player* member = Connected(follower.guid);
        if (!Same(follower.body, member) || follower.body.ai->GetMaster() != lead.body.player)
            continue;   // someone else has that member by now: never written over
        Group* group = member->GetGroup();
        follower.body.ai->SetMaster(human && group && group->IsMember(human->GetGUID()) ? human : nullptr);
        back.push_back(follower.guid);
    }
    if (same)
    {
        PlayerbotAI* ai = lead.body.ai;
        for (std::string const& name : lead.added)
            if (ai->HasStrategy(name, BOT_STATE_NON_COMBAT))
                ai->ChangeStrategy("-" + name, BOT_STATE_NON_COMBAT);
        ai->rpgInfo.ChangeToIdle();
        if (ai->GetMaster() == bot)
        {
            Group* group = bot->GetGroup();
            ai->SetMaster(human && group && group->IsMember(human->GetGUID()) ? human : nullptr);
        }
        if (lead.removedFollow && !ai->HasStrategy("follow", BOT_STATE_NON_COMBAT))
            ai->ChangeStrategy("+follow", BOT_STATE_NON_COMBAT);
        if (InWorld(bot) && !bot->IsInCombat() && !bot->IsBeingTeleported())
            bot->StopMoving();
    }
    LOG_INFO("module.leeroy", "mod-leeroy: lead_ended guid={} why={} followers={}", lead.guid, why, back.size());
    if (!publish)
        return;
    JobEvent job;
    job.id = lead.id;
    job.kind = std::string(kLeadJobKind);
    job.state = "ended";
    job.reason = std::string(why);
    job.guid = lead.guid;
    job.guids = std::move(back);
    PublishJob(job);
}

std::string Start(LeadDoCmd const& cmd, std::string const& id, Player* human, Player* bot,
    std::vector<std::uint32_t>& followers)
{
    PlayerbotAI* ai = GET_PLAYERBOT_AI(bot);
    Lead lead;
    lead.id = id;
    lead.guid = cmd.guid;
    lead.body = BodyOf(bot);
    lead.guild = bot->GetGuildId();
    lead.human = HumanGuid();
    std::uint64_t const now = SteadyMs();
    lead.state.activity = cmd.activity;
    lead.state.started = now;
    lead.state.until = now + kLeadMs;
    lead.spot = cmd.activity == LeadActivity::Travel ? InviteSpotIndex(cmd.spot) : -1;
    // Her own master: playerbots keeps a self-mastered bot as the others' master (as an offline raid's driver), and
    // she follows nobody.
    ai->SetMaster(bot);
    if (ai->HasStrategy("follow", BOT_STATE_NON_COMBAT))
    {
        ai->ChangeStrategy("-follow", BOT_STATE_NON_COMBAT);
        lead.removedFollow = !ai->HasStrategy("follow", BOT_STATE_NON_COMBAT);
    }
    if (ai->HasStrategy("stay", BOT_STATE_NON_COMBAT))
    {
        // An older "stay here" of his: leading means moving, and it is not given back.
        ai->ChangeStrategy("-stay", BOT_STATE_NON_COMBAT);
        ai->GetAiObjectContext()->GetValue<PositionMap&>("position")->Get()["stay"].Reset();
    }
    for (std::string const& name : Strategies(cmd.activity))
    {
        if (ai->HasStrategy(name, BOT_STATE_NON_COMBAT))
            continue;
        ai->ChangeStrategy("+" + name, BOT_STATE_NON_COMBAT);
        if (ai->HasStrategy(name, BOT_STATE_NON_COMBAT))
            lead.added.push_back(name);
    }
    active = std::move(lead);
    Lead& held = *active;
    if (!ai->HasStrategy(Strategies(cmd.activity).front(), BOT_STATE_NON_COMBAT))
    {
        EndLead("failed", false);   // everything it changed given back; nothing started, so nothing ends
        return "lead_failed";
    }
    Aim(held);
    Remember(held, bot);
    // The others of his group on her map, his own cast members following him, now follow her.
    Group* group = human->GetGroup();
    for (GroupReference* ref = group ? group->GetFirstMember() : nullptr; ref; ref = ref->next())
    {
        Player* member = ref->GetSource();
        if (!member || member == bot || member == human)
            continue;
        std::uint32_t const guid = member->GetGUID().GetCounter();
        PlayerbotAI* memberAi = GET_PLAYERBOT_AI(member);
        if (!memberAi || IsSelfBot(member) || !IsCast(guid) || !Realm::IsPinned(guid) || !Realm::IsOnRandomBotAccount(guid)
            || member->GetGuildId() != held.guild || member->GetMap() != bot->GetMap() || memberAi->GetMaster() != human
            || !Fence(guid, member, human).empty())
            continue;
        memberAi->SetMaster(bot);
        held.followers.push_back({guid, BodyOf(member)});
        followers.push_back(guid);
    }
    LOG_INFO("module.leeroy", "mod-leeroy: lead_started guid={} activity={} followers={}", cmd.guid,
        LeadActivityName(cmd.activity), followers.size());
    return {};
}

LeeroyCommandResult HandleLeadDo(nlohmann::json const& message)
{
    std::string error;
    auto const cmd = ParseLeadDo(message, error);
    if (!cmd)
        return Fail(std::move(error));
    if (cmd->action == LeadAction::End)
    {
        // His end works whatever the switch says: it only gives back.
        if (!active || (cmd->guid && cmd->guid != active->guid))
            return Fail("no_lead");
        EndLead("his");
        return {true, {}};
    }
    std::string const id = message.at("id").get<std::string>();
    if (active)
        EndLead("replaced");   // one lead at a time: his newer ask
    Player* human = FindOnline(HumanGuid());
    Player* bot = cmd->guid == HumanGuid() ? nullptr : FindOnline(cmd->guid);
    std::string why = LeadRefusal(cmd->activity, ReadFacts(*cmd, human, bot));
    std::vector<std::uint32_t> followers;
    if (why.empty())
        why = Start(*cmd, id, human, bot, followers);
    if (!why.empty())
    {
        LOG_INFO("module.leeroy", "mod-leeroy: lead_refused guid={} activity={} code={}", cmd->guid,
            LeadActivityName(cmd->activity), why);
        return Fail(std::move(why));
    }
    JobEvent job;
    job.id = id;
    job.kind = std::string(kLeadDoType);
    job.state = "done";
    job.reason = "leading";
    job.guid = cmd->guid;
    job.guids = std::move(followers);
    PublishJob(job);
    return {true, {}};
}

// Followers whose body, group or master changed are no longer the lead's: let go, untouched.
void Prune(Lead& lead, Player* bot)
{
    for (auto it = lead.followers.begin(); it != lead.followers.end();)
    {
        Player* member = Connected(it->guid);
        if (Same(it->body, member) && member->GetGroup() && member->GetGroup() == bot->GetGroup()
            && it->body.ai->GetMaster() == bot)
        {
            ++it;
            continue;
        }
        LOG_INFO("module.leeroy", "mod-leeroy: lead_follower_released guid={}", it->guid);
        it = lead.followers.erase(it);
    }
}

// new rpg kept to the activity, and never its stuck teleport (no way there ends a travel instead: no_path).
void Steer(Lead& lead)
{
    NewRpgInfo& info = lead.body.ai->rpgInfo;
    int const status = static_cast<int>(info.GetStatus());
    if (auto const to = LeadSteer(lead.state.activity, status))
    {
        if (*to == LeadRpg::GoCamp)
        {
            if (InviteSpot const* spot = SpotOf(lead))
                info.ChangeToGoCamp(SpotPosition(*spot));
        }
        else
            info.ChangeToWanderRandom();
        LOG_INFO("module.leeroy", "mod-leeroy: lead_steered guid={} from={} to={}", lead.guid, status,
            static_cast<int>(*to));
    }
    info.stuckTs = getMSTime();
    info.stuckAttempts = 0;
}

void TickActive()
{
    Lead& lead = *active;
    std::uint64_t const now = SteadyMs();
    Player* human = Connected(HumanGuid());
    Player* bot = Connected(lead.guid);
    LeadTickFacts f;
    f.now = now;
    f.on = LeeroyModule::Instance().Enabled() && enabled;
    f.leaderHere = Same(lead.body, bot) && bot->GetGuildId() == lead.guild && HumanGuid() == lead.human;
    f.humanOnline = human != nullptr;
    if (f.leaderHere)
    {
        PlayerbotAI* ai = lead.body.ai;
        bool const here = InWorld(bot);
        f.dead = !bot->IsAlive();
        f.instance = Dungeon(bot) || Dungeon(human);
        f.battleground = (here && Battleground(bot)) || (InWorld(human) && Battleground(human));
        Group* group = bot->GetGroup();
        f.grouped = human && group && group == human->GetGroup() && group->IsLeader(human->GetGUID())
            && !group->isLFGGroup() && !group->isBGGroup() && !group->isBFGroup();
        f.selfMastered = ai->GetMaster() == bot;
        f.activityOn = ai->HasStrategy(Strategies(lead.state.activity).front(), BOT_STATE_NON_COMBAT);
        f.fenced = !Fence(lead.guid, bot, human).empty();
        bool jumped = !here || (lead.hasLast && bot->GetMapId() != lead.lastMap);
        if (here && lead.hasLast && bot->GetMapId() == lead.lastMap)
            jumped = jumped || bot->GetExactDist(lead.lastX, lead.lastY, lead.lastZ) > kLeadJumpYards;
        f.teleported = bot->IsBeingTeleported() || jumped;
        f.fighting = here && GroupFight(bot);
        f.sameMap = here && InWorld(human) && !human->IsBeingTeleported() && human->GetMap() == bot->GetMap();
        f.distance = f.sameMap ? bot->GetDistance(human) : 0.0f;
        if (InviteSpot const* spot = SpotOf(lead); spot && here && bot->GetMapId() == spot->map)
            f.spotDistance = bot->GetDistance(spot->x, spot->y, spot->z);
    }
    NoteLead(lead.state, f);
    LeadDecision const decision = DecideLead(lead.state, f);
    if (decision.step == LeadStep::End)
    {
        EndLead(decision.why);
        return;
    }
    Prune(lead, bot);
    if (decision.step == LeadStep::Pause)
    {
        // The wait: she stands where she is (her activity off) until he is back; the others stand with her.
        SetActivity(lead, false);
        bot->StopMoving();
        bot->GetMotionMaster()->Clear();
        LOG_INFO("module.leeroy", "mod-leeroy: lead_paused guid={}", lead.guid);
    }
    else if (decision.step == LeadStep::Resume)
    {
        SetActivity(lead, true);
        if (lead.state.activity == LeadActivity::Travel)
            Aim(lead);
        LOG_INFO("module.leeroy", "mod-leeroy: lead_resumed guid={}", lead.guid);
    }
    ApplyLead(lead.state, decision.step, now);
    if (!lead.state.paused)
        Steer(lead);
    Remember(lead, bot);
}
}

void ReloadLead() { enabled = sConfigMgr->GetOption<bool>("Leeroy.Lead.Enable", false); }

void RegisterLeadHandler() { RegisterCommand(std::string(kLeadDoType), HandleLeadDo); }

void TickLead(std::uint32_t diffMs)
{
    if (!active)
        return;
    if (diffMs < untilTick)
    {
        untilTick -= diffMs;
        return;
    }
    untilTick = 1000;
    TickActive();
}

void ForgetLead(std::uint32_t guid)
{
    if (!active)
        return;
    if (guid == active->guid)
    {
        EndLead("gone");   // the others follow him again before her body goes
        return;
    }
    auto& followers = active->followers;
    for (auto it = followers.begin(); it != followers.end(); ++it)
        if (it->guid == guid)
        {
            followers.erase(it);
            return;
        }
}
}
