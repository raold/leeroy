// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "Strings.h"
#include "GuildAdd.h"
#include "GuildPorts.h"
#include "RaidAssembly.h"
#include "LeeroyEmotes.h"
#include "LeeroyEvents.h"
#include "LeeroySocialSit.h"
#include "LeeroyStrings.h"
#include "Realm/RealmModule.h"
#include "Realm/RealmPorts.h"

#include "AiObjectContext.h"
#include "Config.h"
#include "DBCStores.h"
#include "EmoteAction.h"
#include "Group.h"
#include "LFGMgr.h"
#include "Log.h"
#include "Map.h"
#include "Opcodes.h"
#include "PlayerbotAI.h"
#include "Playerbots.h"
#include "PositionValue.h"
#include "WorldPacket.h"
#include "WorldSession.h"

#include <chrono>
#include <iterator>
#include <map>
#include <optional>
#include <string>
#include <utility>

// The owner's strings in game (string_do): carried out at once on the world thread, or refused with the policy's
// code. Logs carry the member's guid, the kind and codes only.

namespace Leeroy::Guild
{
namespace
{
constexpr std::uint32_t kSpellDuel = 7266;
// come_to_me's stay starts once she has arrived beside him; a teleport that never lands drops it.
constexpr std::uint64_t kArriveMs = 30000;
constexpr float kArrivedYards = 50.0f;

bool enabled = false;   // Leeroy.Strings.Enable, world thread
std::uint32_t untilTick = 0;
int waived = 0;          // StringWaiver depth: his own asks pass come_to_me's fence (world thread)

std::uint64_t SteadyMs()
{
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count());
}

// What a string of ours changed in one body, until it ends. Only that same life is ever written back: a relog or an
// AI replacement drops the hold untouched.
struct Hold
{
    Player* player = nullptr;
    PlayerbotAI* ai = nullptr;
    WorldSession* session = nullptr;
    std::uint32_t guild = 0, human = 0;
    StringHoldState state;   // what it added and how long it lasts (LeeroyStrings.h)
};
std::map<std::uint32_t, Hold> holds;   // world thread
Realm::SocialSeats seats;                  // #244's lease, keyed by the string's command id

bool Same(Hold const& hold, Player* bot)
{
    return bot && hold.player == bot && hold.ai == GET_PLAYERBOT_AI(bot) && hold.session == bot->GetSession();
}

Hold& Own(std::uint32_t guid, Player* bot)
{
    Hold& hold = holds[guid];
    if (!Same(hold, bot) || hold.guild != bot->GetGuildId() || hold.human != HumanGuid())
    {
        hold = Hold();
        hold.player = bot;
        hold.ai = GET_PLAYERBOT_AI(bot);
        hold.session = bot->GetSession();
        hold.guild = bot->GetGuildId();
        hold.human = HumanGuid();
    }
    return hold;
}

bool InHisGroup(Player* bot)
{
    Group* group = bot->GetGroup();
    return group && group->IsMember(ObjectGuid::Create<HighGuid::Player>(HumanGuid()));
}

// The ownership fences. A member of his own group is under every fence but the one that holds his group for him
// (busy:raid_owner), as his add command treats her; anyone else under the Realm's MutationReason. Called inside a
// StringWaiver: the strings' own come_to_me fence never holds his next string back.
std::string Fence(std::uint32_t guid, Player* bot)
{
    if (!InHisGroup(bot))
        return Realm::RealmModule::Instance().MutationReason(guid);
    if (Realm::RealmOwns(guid))
        return "busy:realm_owner";
    return Realm::NativeOwnershipReason(guid);
}

std::uint32_t AreaFlags(std::uint32_t area)
{
    AreaTableEntry const* entry = sAreaTableStore.LookupEntry(area);
    return entry ? entry->flags : 0;
}

bool Travelling(Player* player)
{
    return player->IsBeingTeleported() || player->IsInFlight() || player->GetTransport() || player->GetVehicle();
}

// His fight: him, or anyone of his group on his map, in combat (RaidLead's own test of his group's fight).
bool HisFight(Player* human)
{
    if (human->IsInCombat())
        return true;
    if (Group* group = human->GetGroup())
        for (GroupReference* ref = group->GetFirstMember(); ref; ref = ref->next())
            if (Player* member = ref->GetSource(); member && member->GetMap() == human->GetMap() && member->IsInCombat())
                return true;
    return false;
}

bool Battleground(Player* player)
{
    Map* map = player->GetMap();
    return player->InBattleground() || player->InArena() || (map && map->IsBattlegroundOrArena());
}

void SetStay(PlayerbotAI* ai, Player* bot)
{
    PositionMap& positions = ai->GetAiObjectContext()->GetValue<PositionMap&>("position")->Get();
    positions["stay"].Set(bot->GetPositionX(), bot->GetPositionY(), bot->GetPositionZ(), bot->GetMapId());
}

void ResetStay(PlayerbotAI* ai)
{
    PositionMap& positions = ai->GetAiObjectContext()->GetValue<PositionMap&>("position")->Get();
    positions["stay"].Reset();
}

// A string that moves her ends her string seat first: she stands for what he asked next.
void EndSeat(Player* bot)
{
    std::uint32_t const guid = bot->GetGUID().GetCounter();
    auto const seat = seats.Entries().find(guid);
    if (seat == seats.Entries().end())
        return;
    seats.End(guid, std::string(seat->second.token));
    if (bot->getStandState() == UNIT_STAND_STATE_SIT)
        bot->SetStandState(UNIT_STAND_STATE_STAND);
}

StringFacts ReadFacts(StringDoCmd const& cmd, Player* human, Player* bot)
{
    StringFacts f;
    LeeroyModule& module = LeeroyModule::Instance();
    f.enabled = module.Enabled() && enabled;
    f.emote = cmd.emote;
    if (human)
    {
        f.humanOnline = true;
        f.humanAlive = human->IsAlive();
        f.humanCombat = HisFight(human);
        f.humanTravelling = Travelling(human);
        f.humanInstance = !human->GetMap() || human->GetMap()->IsDungeon();
        f.humanBattleground = Battleground(human);
        f.humanDuel = human->duel != nullptr;
        f.humanResting = human->HasPlayerFlag(PLAYER_FLAGS_RESTING);
        f.humanArea = AreaFlags(human->GetAreaId());
        f.humanZone = AreaFlags(human->GetZoneId());
    }
    std::uint32_t const guid = cmd.guid;
    f.cast = guid != HumanGuid() && IsCast(guid) && Realm::IsPinned(guid) && Realm::IsOnRandomBotAccount(guid)
        && (!bot || (bot->GetGuildId() == module.GuildId() && GET_PLAYERBOT_AI(bot) && !IsSelfBot(bot)));
    if (!bot || !human || !f.cast)
        return f;
    f.online = true;
    f.fence = Fence(guid, bot);
    f.dead = !bot->IsAlive();
    f.combat = bot->IsInCombat();
    f.travelling = Travelling(bot);
    f.duel = bot->duel != nullptr;
    f.instance = !bot->GetMap() || bot->GetMap()->IsDungeon() || bot->HasPendingBind();
    f.battleground = Battleground(bot);
    lfg::LfgState const lfg = sLFGMgr->GetState(bot->GetGUID());
    f.queued = bot->InBattlegroundQueue() || (lfg != lfg::LFG_STATE_NONE && lfg != lfg::LFG_STATE_RAIDBROWSER);
    f.area = AreaFlags(bot->GetAreaId());
    f.sameMap = bot->GetMap() == human->GetMap();
    f.sameZone = f.sameMap && bot->GetZoneId() == human->GetZoneId();
    f.distance = f.sameMap ? bot->GetDistance(human) : 0.0f;
    Group* his = human->GetGroup();
    f.hisGroup = his && his->IsMember(bot->GetGUID());
    f.otherGroup = !f.hisGroup && (bot->GetGroup() || bot->GetOriginalGroup() || bot->GetGroupInvite());
    f.leader = !his || his->IsLeader(human->GetGUID());
    f.groupSpecial = his && (his->isLFGGroup() || his->isBGGroup() || his->isBFGroup());
    f.groupRaid = his && his->isRaidGroup();
    f.groupMembers = his ? his->GetMembersCount() : 0;
    if (cmd.kind == StringKind::JoinGroup)
        f.joinRefusal = GuildAddJoinRefusal(human, bot);
    return f;
}

// Beside him; a member outside his group then stays there for kStringStayMs once she has arrived, so her own routine
// does not walk her off at once. In his group, following him keeps her with him.
std::string ComeToMe(Player* bot, Player* human, std::string& outcome)
{
    bool const grouped = InHisGroup(bot);
    if (!GuildAddBring(bot, human))
        return "teleport_failed";
    if (!grouped)
    {
        Hold& hold = Own(bot->GetGUID().GetCounter(), bot);
        std::uint64_t const now = SteadyMs();
        hold.state.until = now + kStringStayMs;
        hold.state.arriveBy = now + kArriveMs;
        hold.state.group = 0;
        hold.state.follow = hold.state.unfollowed = false;
    }
    outcome = "came";
    return {};
}

// As his add command fills a party: his group (made now when he has none), the raid night's habits, beside him.
std::string JoinGroup(Player* bot, Player* human, std::string& outcome)
{
    Group* group = human->GetGroup();
    bool const created = !group;
    if (!group && !(group = LeaderGroup(human)))
        return "group_failed";
    if (group->isRaidGroup() || group->IsFull() || group->GetMembersCount() >= kStringPartySize
        || !group->AddMember(bot))
    {
        if (created && group->GetMembersCount() < 2)
            group->Disband();   // never leave him in a group of one
        return "group_full";
    }
    holds.erase(bot->GetGUID().GetCounter());   // RaidHabits resets her strategies, our stay with them
    RaidHabits(bot, human);
    outcome = GuildAddBring(bot, human) ? "joined" : "joined_far";
    return {};
}

// Him as her master with playerbots' follow; the same string again ends it, and she waits where she is, as playerbots'
// own "stay" has her. Both last while that group does.
std::string Follow(Player* bot, Player* human, std::string& outcome)
{
    PlayerbotAI* ai = GET_PLAYERBOT_AI(bot);
    std::uint32_t const guid = bot->GetGUID().GetCounter();
    std::uint64_t const group = bot->GetGroup()->GetGUID().GetRawValue();
    auto const it = holds.find(guid);
    if (it != holds.end() && Same(it->second, bot) && it->second.state.follow && it->second.state.group == group)
    {
        StringHoldState& state = it->second.state;
        bool const had = ai->HasStrategy("stay", BOT_STATE_NON_COMBAT);
        bool const following = ai->HasStrategy("follow", BOT_STATE_NON_COMBAT);
        ai->ChangeStrategy("-follow,+stay", BOT_STATE_NON_COMBAT);
        if (!ai->HasStrategy("stay", BOT_STATE_NON_COMBAT))
            return "follow_failed";
        SetStay(ai, bot);
        state.follow = false;
        state.stay = state.stay || !had;
        state.unfollowed = state.unfollowed || following;
        outcome = "stopped";
        return {};
    }
    if (!ai->GetAiObjectContext()->GetStrategy("follow"))
        return "unsupported";
    ai->SetMaster(human);
    ai->ChangeStrategy("+follow,-stay", BOT_STATE_NON_COMBAT);
    ResetStay(ai);
    if (!ai->HasStrategy("follow", BOT_STATE_NON_COMBAT) || ai->GetMaster() != human)
        return "follow_failed";
    Hold& hold = Own(guid, bot);
    hold.state = StringHoldState();
    hold.state.follow = true;   // his ask took any stay off
    hold.state.group = group;
    outcome = "following";
    return {};
}

// The core's own duel spell, cast at him: the request is his to accept.
std::string DuelMe(Player* bot, Player* human, std::string& outcome)
{
    bot->SetFacingToObject(human);
    bot->CastSpell(human, kSpellDuel, false);
    bool const challenged = bot->duel && human->duel && bot->duel->Opponent == human && bot->duel->Initiator == bot
        && human->duel->Opponent == bot;
    if (!challenged)
        return "duel_failed";
    outcome = "challenged";
    return {};
}

// As the core's emote command, aimed at him; inside bridge speech, so its event carries via_bridge.
std::string Emote(Player* bot, Player* human, std::string const& name, std::string& outcome)
{
    std::optional<uint32> const emote = LeeroyTextEmoteId(name);
    if (!emote || !sEmotesTextStore.LookupEntry(*emote))
        return "bad_emote";
    if (!bot->CanSpeak())
        return "muted";
    bot->SetFacingToObject(human);
    {
        LeeroyBridgeSpeech const speech;
        WorldPacket packet(CMSG_TEXT_EMOTE, 16);
        packet << uint32(*emote)
               << EmoteActionBase::GetNumberOfEmoteVariants(static_cast<TextEmotes>(*emote), bot->getRace(),
                      bot->getGender())
               << human->GetGUID();
        bot->GetSession()->HandleTextEmoteOpcode(packet);
    }
    // HandleTextEmoteOpcode applies the flood throttle before emitting.
    if (!bot->CanSpeak())
        return "muted";
    outcome = "emoted";
    return {};
}

// #244's social seat: she sits facing him and keeps sitting while he stays near, for kStringSitSeconds at most.
std::string Sit(Player* bot, Player* human, std::string const& id, std::string& outcome)
{
    std::uint32_t const guid = bot->GetGUID().GetCounter();
    if (auto const seat = seats.Entries().find(guid); seat != seats.Entries().end())
        seats.End(guid, std::string(seat->second.token));
    if (bot->IsMounted() && bot->HasUnitMovementFlag(MOVEMENTFLAG_FLYING))
        return "sit_failed";   // never off a flying mount in the air
    if (!seats.Begin(guid, id, SteadyMs(), kStringSitSeconds, LeeroyModule::Instance().GuildId(), HumanGuid()))
        return "sit_failed";
    if (bot->IsMounted())
    {
        WorldPacket empty;   // as playerbots dismounts: through her session
        bot->GetSession()->HandleCancelMountAuraOpcode(empty);
    }
    bot->SetFacingToObject(human);
    bot->StopMoving();
    bot->SetStandState(UNIT_STAND_STATE_SIT);
    outcome = "sat";
    return {};
}

std::string Carry(StringDoCmd const& cmd, std::string const& id, Player* human, Player* bot, std::string& outcome)
{
    if (cmd.kind != StringKind::Emote && cmd.kind != StringKind::Sit)
        EndSeat(bot);
    switch (cmd.kind)
    {
        case StringKind::ComeToMe: return ComeToMe(bot, human, outcome);
        case StringKind::JoinGroup: return JoinGroup(bot, human, outcome);
        case StringKind::Follow: return Follow(bot, human, outcome);
        case StringKind::DuelMe: return DuelMe(bot, human, outcome);
        case StringKind::Emote: return Emote(bot, human, cmd.emote, outcome);
        case StringKind::Sit: return Sit(bot, human, id, outcome);
    }
    return "bad_kind";
}

LeeroyCommandResult HandleStringDo(nlohmann::json const& message)
{
    std::string error;
    auto const cmd = ParseStringDo(message, error);
    if (!cmd)
        return {false, std::move(error)};
    if (cmd->targetGuid && cmd->targetGuid != HumanGuid())
        return {false, "bad field: target_guid"};
    std::string const id = message.at("id").get<std::string>();
    StringWaiver const waiver;   // his own ask passes his earlier come_to_me's fence
    Player* human = FindOnline(HumanGuid());
    Player* bot = cmd->guid == HumanGuid() ? nullptr : FindOnline(cmd->guid);
    std::string why = StringRefusal(cmd->kind, ReadFacts(*cmd, human, bot));
    std::string outcome;
    if (why.empty())
        why = Carry(*cmd, id, human, bot, outcome);
    if (!why.empty())
    {
        LOG_INFO("module.leeroy", "mod-leeroy: string_refused guid={} kind={} code={}", cmd->guid,
            StringKindName(cmd->kind), why);
        return {false, std::move(why)};
    }
    // What she did, for the mind: the ack says only that it was carried out.
    JobEvent job;
    job.id = id;
    job.kind = std::string(kStringDoType);
    job.state = "done";
    job.reason = outcome;
    job.guid = cmd->guid;
    job.guids = {cmd->guid};
    PublishJob(job);
    LOG_INFO("module.leeroy", "mod-leeroy: string_done guid={} kind={} outcome={}", cmd->guid,
        StringKindName(cmd->kind), outcome);
    return {true, {}};
}

void TickSeats(std::uint64_t now, bool on, Player* human)
{
    auto const snapshot = seats.Entries();
    for (auto const& [guid, seat] : snapshot)
    {
        Player* bot = FindOnline(guid);
        bool const free = bot && Fence(guid, bot).empty();
        bool const ready = on && free && human && bot->IsAlive() && !bot->IsInCombat() && !HisFight(human)
            && !bot->IsBeingTeleported() && bot->GetMap() == human->GetMap()
            && bot->GetDistance(human) <= kStringSitYards;
        if (!seats.Current(seat, now, ready, LeeroyModule::Instance().GuildId(), HumanGuid()))
        {
            seats.End(guid, seat.token);
            if (free && bot->getStandState() == UNIT_STAND_STATE_SIT)
                bot->SetStandState(UNIT_STAND_STATE_STAND);
            continue;
        }
        // Idle playerbot updates can stand her up; the owned seat keeps the pose while every check permits it.
        bot->StopMoving();
        if (bot->getStandState() != UNIT_STAND_STATE_SIT)
            bot->SetStandState(UNIT_STAND_STATE_SIT);
    }
}

void TickHolds(std::uint64_t now, bool on, Player* human)
{
    for (auto it = holds.begin(); it != holds.end();)
    {
        std::uint32_t const guid = it->first;
        Hold& hold = it->second;
        StringHoldState& state = hold.state;
        Player* bot = FindOnline(guid);
        if (!bot)
        {
            // Between maps the hold waits for the same body; a logout or a stay that ran out drops it.
            std::uint64_t const end = state.arriveBy ? state.arriveBy : state.until;
            it = end && now >= end + kArriveMs ? holds.erase(it) : std::next(it);
            continue;
        }
        if (!Same(hold, bot) || bot->GetGuildId() != hold.guild || HumanGuid() != hold.human)
        {
            it = holds.erase(it);   // another life: never write over its baseline
            continue;
        }
        StringHoldFacts facts;
        facts.now = now;
        facts.on = on;
        facts.humanOnline = human != nullptr;
        Group* group = bot->GetGroup();
        facts.group = group ? group->GetGUID().GetRawValue() : 0;
        facts.raid = group && group->isRaidGroup();
        StringHoldStep const step = DecideStringHold(state, facts);
        if (step == StringHoldStep::Drop)
        {
            it = holds.erase(it);   // she joined a group: its join reset her strategies, ours with them
            continue;
        }
        // Another owner holds her: never touched, the hold waits. Its own come_to_me fence kept the
        // Realm, training and offline raids out, so that is only an owner that never asks (a raid night's group).
        if (!Fence(guid, bot).empty() || bot->IsInCombat())
        {
            ++it;
            continue;
        }
        if (step == StringHoldStep::Keep)
        {
            if (state.arriveBy && human && bot->GetMap() == human->GetMap() && !bot->IsBeingTeleported()
                && bot->IsWithinDistInMap(human, kArrivedYards))
            {
                if (!hold.ai->HasStrategy("stay", BOT_STATE_NON_COMBAT))
                {
                    hold.ai->ChangeStrategy("+stay", BOT_STATE_NON_COMBAT);
                    state.stay = hold.ai->HasStrategy("stay", BOT_STATE_NON_COMBAT);
                }
                SetStay(hold.ai, bot);
                state.arriveBy = 0;
            }
            ++it;
            continue;
        }
        if (state.stay && hold.ai->HasStrategy("stay", BOT_STATE_NON_COMBAT))
        {
            hold.ai->ChangeStrategy("-stay", BOT_STATE_NON_COMBAT);
            ResetStay(hold.ai);
            LOG_INFO("module.leeroy", "mod-leeroy: string_stay_lifted guid={}", guid);
        }
        // A stop inside a party that has since become a raid: she follows him again, as his raid's members do.
        if (step == StringHoldStep::LiftFollow && !hold.ai->HasStrategy("follow", BOT_STATE_NON_COMBAT))
            hold.ai->ChangeStrategy("+follow", BOT_STATE_NON_COMBAT);
        it = holds.erase(it);
    }
}
}

StringWaiver::StringWaiver() { ++waived; }
StringWaiver::~StringWaiver() { --waived; }

bool StringStayHeld(std::uint32_t guid)
{
    if (waived)
        return false;
    auto const it = holds.find(guid);
    return it != holds.end() && StringHoldFences(it->second.state);
}

void ReloadStrings() { enabled = sConfigMgr->GetOption<bool>("Leeroy.Strings.Enable", false); }

void RegisterStringsHandler() { RegisterCommand(std::string(kStringDoType), HandleStringDo); }

void ForgetStrings(std::uint32_t guid)
{
    holds.erase(guid);
    if (auto const seat = seats.Entries().find(guid); seat != seats.Entries().end())
        seats.End(guid, std::string(seat->second.token));
}

void TickStrings(std::uint32_t diffMs)
{
    if (holds.empty() && seats.Entries().empty())
        return;
    if (diffMs < untilTick)
    {
        untilTick -= diffMs;
        return;
    }
    untilTick = 1000;
    StringWaiver const waiver;   // the strings' own holds are theirs to lift
    std::uint64_t const now = SteadyMs();
    bool const on = LeeroyModule::Instance().Enabled() && enabled;
    Player* human = FindOnline(HumanGuid());
    TickSeats(now, on, human);
    TickHolds(now, on, human);
}
}
