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
    std::uint64_t until = 0;      // a come_to_me stay's end; 0 for one that lasts while the group does
    std::uint64_t group = 0;      // the group the hold lasts with, 0 for none
    std::uint64_t arriveBy = 0;   // come_to_me: the stay starts on arrival, by this time
    bool stay = false;            // we added playerbots' stay and take it back at the end
    bool follow = false;          // a follow string of ours is in force
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
// (busy:raid_owner), as his add command treats her; anyone else under the Realm's MutationReason.
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
        f.humanCombat = human->IsInCombat();
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
        hold.until = now + kStringStayMs;
        hold.arriveBy = now + kArriveMs;
        hold.group = 0;
        hold.follow = false;
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
    if (it != holds.end() && Same(it->second, bot) && it->second.follow && it->second.group == group)
    {
        bool const had = ai->HasStrategy("stay", BOT_STATE_NON_COMBAT);
        ai->ChangeStrategy("-follow,+stay", BOT_STATE_NON_COMBAT);
        if (!ai->HasStrategy("stay", BOT_STATE_NON_COMBAT))
            return "follow_failed";
        SetStay(ai, bot);
        it->second.follow = false;
        it->second.stay = it->second.stay || !had;
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
    hold.follow = true;
    hold.stay = false;   // his ask took any stay off
    hold.group = group;
    hold.until = hold.arriveBy = 0;
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
        bool const ready = on && free && human && bot->IsAlive() && !bot->IsInCombat() && !human->IsInCombat()
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
        Player* bot = FindOnline(guid);
        if (!bot)
        {
            // Between maps the hold waits for the same body; a logout or a stay that ran out drops it.
            std::uint64_t const end = hold.arriveBy ? hold.arriveBy : hold.until;
            it = end && now >= end + kArriveMs ? holds.erase(it) : std::next(it);
            continue;
        }
        if (!Same(hold, bot) || bot->GetGuildId() != hold.guild || HumanGuid() != hold.human)
        {
            it = holds.erase(it);   // another life: never write over its baseline
            continue;
        }
        Group* group = bot->GetGroup();
        std::uint64_t const groupNow = group ? group->GetGUID().GetRawValue() : 0;
        if (!hold.group && groupNow)
        {
            it = holds.erase(it);   // come_to_me's stay: joining a group resets her strategies, ours with them
            continue;
        }
        bool const due = !on || (hold.group && groupNow != hold.group) || (hold.until && now >= hold.until)
            || (hold.arriveBy && now >= hold.arriveBy);
        // Another owner holds her (a Realm lease, training, an offline raid): never touched, the hold waits.
        if (!Fence(guid, bot).empty() || bot->IsInCombat())
        {
            ++it;
            continue;
        }
        if (hold.arriveBy && !due)
        {
            if (human && bot->GetMap() == human->GetMap() && !bot->IsBeingTeleported()
                && bot->IsWithinDistInMap(human, kArrivedYards))
            {
                if (!hold.ai->HasStrategy("stay", BOT_STATE_NON_COMBAT))
                {
                    hold.ai->ChangeStrategy("+stay", BOT_STATE_NON_COMBAT);
                    hold.stay = hold.ai->HasStrategy("stay", BOT_STATE_NON_COMBAT);
                }
                SetStay(hold.ai, bot);
                hold.arriveBy = 0;
            }
            ++it;
            continue;
        }
        if (!due)
        {
            ++it;
            continue;
        }
        if (hold.stay && hold.ai->HasStrategy("stay", BOT_STATE_NON_COMBAT))
        {
            hold.ai->ChangeStrategy("-stay", BOT_STATE_NON_COMBAT);
            ResetStay(hold.ai);
            LOG_INFO("module.leeroy", "mod-leeroy: string_stay_lifted guid={}", guid);
        }
        it = holds.erase(it);
    }
}
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
    std::uint64_t const now = SteadyMs();
    bool const on = LeeroyModule::Instance().Enabled() && enabled;
    Player* human = FindOnline(HumanGuid());
    TickSeats(now, on, human);
    TickHolds(now, on, human);
}
}
