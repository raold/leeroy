// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "DummyTrialJob.h"
#include "TrainingCommands.h"
#include "TrainingTap.h"
#include "GuildPorts.h"
#include "Guild/OfflineRaidJob.h"
#include "Guild/Strings.h"
#include "LeeroyDummyLifecycle.h"
#include "LeeroyTrainingProtocol.h"
#include "Realm/RealmOwnership.h"

#if !defined(LEEROY_HAS_DUNGEON_CLEAR)
#error "LEEROY_HAS_DUNGEON_CLEAR comes from mod-leeroy.cmake; re-run cmake"
#endif
#if LEEROY_HAS_DUNGEON_CLEAR
#include "AiObjectContext.h"
#include "AttackAction.h"
#include "Creature.h"
#include "Group.h"
#include "GroupMgr.h"
#include "InstanceSaveMgr.h"
#include "Log.h"
#include "Map.h"
#include "MapMgr.h"
#include "ModelIgnoreFlags.h"
#include "PlayerbotAI.h"
#include "Playerbots.h"
#include "WorldSession.h"
#include "TestRun/DcTestDriver.h"
#include "TestRun/DcTestRunManager.h"
#include <algorithm>
#include <chrono>
#include <cmath>
#include <list>
#include <memory>
#include <optional>
#endif

namespace Leeroy::Training
{
#if LEEROY_HAS_DUNGEON_CLEAR
namespace
{
using Clock = std::chrono::steady_clock;
uint64 Now()
{
    return std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now().time_since_epoch()).count();
}
ObjectGuid Guid(uint32 low) { return ObjectGuid::Create<HighGuid::Player>(low); }
template<class T> std::uintptr_t Identity(T* object) { return reinterpret_cast<std::uintptr_t>(object); }
bool HumanConnected(uint32 low)
{
    auto* human = low ? ObjectAccessor::FindConnectedPlayer(Guid(low)) : nullptr;
    return human && human->GetSession(); // Includes a connected human who is currently worldporting.
}

struct Member
{
    uint32 guid = 0;
    std::string co, dropped, cleanupError;
    WorldLocation origin;
    std::uintptr_t player = 0, ai = 0, session = 0;
    ObjectGuid group;
    std::uintptr_t groupIdentity = 0;
    // A throwing Create may already have linked its leader. Retain that exact object until published/cleaned.
    std::unique_ptr<Group> creatingGroup;
    bool prepared = false, moved = false, arrived = false, attacking = false, frozen = false,
        released = true, resetDone = true, returned = false, returnIssued = false, blocked = false,
        groupReleased = true, groupReleaseStarted = false, leaderFlag = false, leaderRestored = true,
        finalResetDone = true, abandoned = false, setUp = false, letGo = false, landing = false;
    uint32 reasserted = 0, nearTeleports = 0;
    uint64 started = 0, ms = 0, damage = 0, hits = 0, retryAt = 0, setupAt = 0;
    std::optional<uint64> outsideSince;
};
struct Job
{
    DummyTrialCmd command;
    Guild::JobEvent event;
    DummyLifecycle life;
    std::vector<Member> members;
    uint32 guild = 0, human = 0;
    uint64 epoch = 0;
    ObjectGuid driver, dummy;
    std::uintptr_t driverIdentity = 0;
    int slot = -1;
    bool returning = false, terminalPublished = false, heldNoted = false, bounded = false;
    uint64 failedAt = 0, releaseCheckAt = 0;
    std::vector<uint32> held; // After the release bound: the members the job keeps (DummyFateAfterBound).

    Job(DummyTrialCmd requested, uint64 accepted) : command(std::move(requested)), life(accepted, command.seconds) { }
};
std::unique_ptr<Job> active; // World-thread owner. No retained Player/AI/Creature pointers.

class DummyAttack : public AttackAction
{
public:
    explicit DummyAttack(PlayerbotAI* ai) : AttackAction(ai, "training dummy attack") { }
    bool Begin(Unit* target) { return Attack(target); }
};

bool SameBody(Member const& member, Player* bot)
{
    return bot && Identity(bot) == member.player && Identity(bot->GetSession()) == member.session
        && Identity(GET_PLAYERBOT_AI(bot)) == member.ai;
}

// A blocked member is never the same session to the trial's own steps; only letting it go asks past that.
bool SameSession(Member const& member, Player* bot) { return !member.blocked && SameBody(member, bot); }

bool AtOrigin(Member const& member, Player* bot)
{
    return bot && bot->IsInWorld() && !bot->IsBeingTeleported() && !bot->GetInstanceId()
        && bot->GetMapId() == member.origin.GetMapId()
        && bot->GetDistance2d(member.origin.GetPositionX(), member.origin.GetPositionY()) <= 5.0f
        && std::abs(bot->GetPositionZ() - member.origin.GetPositionZ()) <= 5.0f;
}

// In the world on the trial's map, outside any instance, anywhere on it (DummyReturnState::onTrialMap).
bool OnTrialMap(Job const& job, Player* bot)
{
    return bot && bot->IsInWorld() && !bot->IsBeingTeleported() && !bot->GetInstanceId()
        && bot->GetMapId() == kDummyPlaces[job.command.place].map;
}

bool AtTrial(Job const& job, Player* bot)
{
    auto const& place = kDummyPlaces[job.command.place];
    return OnTrialMap(job, bot) && bot->GetDistance2d(place.x, place.y) <= 45.0f
        && std::abs(bot->GetPositionZ() - place.z) <= 15.0f;
}

// The trial's dummy: the live one nearest the place, whichever ring the member that looks for it stands on.
Creature* TrialDummy(Job const& job, Player* bot)
{
    auto const& place = kDummyPlaces[job.command.place];
    std::list<Creature*> found;
    bot->GetCreatureListWithEntryInGrid(found, kDummyEntry, static_cast<float>(kDummyRangedRingYd) + 15.0f);
    Creature* best = nullptr;
    float bestYd = 15.0f;
    for (auto* creature : found)
    {
        float const yd = creature->GetDistance(place.x, place.y, place.z);
        if (creature->IsAlive() && yd <= bestYd)
        {
            best = creature;
            bestYd = yd;
        }
    }
    return best;
}

// Where a member stands for the fight, facing the dummy (DummyRingRadius). A ranged spot needs ground within 5 yd of
// the dummy's height and a clear line to it; without them the member takes its melee-ring spot, as before.
Position RingSpot(DummyPlace const& place, std::size_t count, std::size_t index, bool ranged)
{
    constexpr double pi = 3.14159265358979323846;
    double const angle = 2.0 * pi * static_cast<double>(index) / static_cast<double>(count);
    float const facing = static_cast<float>(std::fmod(angle + pi, 2.0 * pi));
    auto at = [&](double radius)
    {
        return Position(place.x + static_cast<float>(radius * std::cos(angle)),
            place.y + static_cast<float>(radius * std::sin(angle)), place.z, facing);
    };
    Position const melee = at(DummyRingRadius(count, false));
    if (!ranged) return melee;
    Position spot = at(DummyRingRadius(count, true));
    auto* map = sMapMgr->FindBaseNonInstanceMap(place.map);
    if (!map) return melee;
    float const ground = map->GetHeight(PHASEMASK_NORMAL, spot.GetPositionX(), spot.GetPositionY(), place.z + 5.0f);
    if (!std::isfinite(ground) || ground <= INVALID_HEIGHT || std::abs(ground - place.z) > 5.0f) return melee;
    spot.m_positionZ = ground;
    if (!map->isInLineOfSight(place.x, place.y, place.z + 2.0f, spot.GetPositionX(), spot.GetPositionY(),
            ground + 2.0f, PHASEMASK_NORMAL, LINEOFSIGHT_ALL_CHECKS, VMAP::ModelIgnoreFlags::Nothing))
        return melee;
    return spot;
}

Group* OwnedGroup(Member const& member)
{
    auto* group = member.creatingGroup ? member.creatingGroup.get()
        : member.group ? sGroupMgr->GetGroupByGUID(member.group.GetCounter()) : nullptr;
    return group && Identity(group) == member.groupIdentity ? group : nullptr;
}

bool HasNonPermanentBind(uint32 guid)
{
    for (uint8 difficulty = 0; difficulty < MAX_DIFFICULTY; ++difficulty)
        for (auto const& bound : sInstanceSaveMgr->PlayerGetBoundInstances(Guid(guid), Difficulty(difficulty)))
            if (!bound.second.perm) return true;
    return false;
}

bool GroupScope(Member const& member, Player* bot)
{
    auto* group = OwnedGroup(member);
    bool exact = group && (!bot->GetGroup() || bot->GetGroup() == group);
    bool sole = group && group->GetLeaderGUID() == bot->GetGUID()
        && group->GetMemberSlots().size() == 1 && group->GetMemberSlots().front().guid == bot->GetGUID();
    return DummyGroupProven(member.groupReleased, bot->GetGroup() != nullptr, exact,
        group ? group->GetMembersCount() : 0, sole, group ? group->GetInviteeCount() : 0,
        group && !group->isRaidGroup() && !group->isBGGroup() && !group->isBFGroup() && !group->isLFGGroup(),
        !member.groupReleased && HasNonPermanentBind(member.guid));
}

DummyOwnerState OwnerState(Job const& job, Member const& member, Player* bot, bool blockedToo = false)
{
    auto master = Identity(GET_PLAYERBOT_AI(bot)->GetMaster());
    return {blockedToo ? SameBody(member, bot) : SameSession(member, bot),
        member.guid == job.human || member.guid == Guild::HumanGuid(),
        Guild::IsCast(member.guid), IsSelfBot(bot), bot->GetGuildId() == job.guild,
        bot->GetGroupInvite() != nullptr || bot->GetOriginalGroup() != nullptr,
        DcTestRunManager::Instance().IsReserved(bot->GetGUID()) || Guild::OfflineRaidOwns(member.guid),
        GroupScope(member, bot), member.released ? master == 0 : master == job.driverIdentity,
        !bot->GetSession() || bot->GetSession()->IsLoggingOut()};
}

bool Owned(Job const& job, Member const& member, Player* bot)
{
    return bot && GET_PLAYERBOT_AI(bot) && DummyOwnerProven(OwnerState(job, member, bot));
}

// Why Owned is false, for the log: "body" when there is no online bot with an AI to ask.
std::string MissingOwnership(Job const& job, Member const& member, Player* bot)
{
    return bot && GET_PLAYERBOT_AI(bot) ? DummyOwnerMissing(OwnerState(job, member, bot)) : std::string("body");
}

Player* Driver(Job const& job);

// Our solo party's creation makes the bot's stock "reset botAI" drop the driver as master and reset
// its strategies (DummyMasterReassertable). Put the driver back and strip follow again, as the
// offline raid does. True only when the driver was put back.
bool Reassert(Job const& job, Member& member, Player* bot)
{
    auto* ai = bot ? GET_PLAYERBOT_AI(bot) : nullptr;
    if (!ai || ai->GetMaster() || member.released || member.groupReleased) return false; // The common, cheap case.
    auto* driver = Driver(job);
    if (!driver || !DummyMasterReassertable(OwnerState(job, member, bot), member.released, member.groupReleased, true))
        return false;
    ai->SetMaster(driver);
    ai->ChangeStrategy("-follow", BOT_STATE_COMBAT);
    ai->ChangeStrategy("-follow", BOT_STATE_NON_COMBAT);
    ++member.reasserted;
    if (member.reasserted == 1 || member.reasserted % 50 == 0)
        LOG_INFO("module.leeroy", "mod-leeroy: dummy trial {} master_reasserted guid={} count={}", job.event.id,
            member.guid, member.reasserted);
    return Identity(ai->GetMaster()) == job.driverIdentity;
}

void PublishCreatedGroup(Member& member)
{
    if (!member.creatingGroup) return;
    auto* group = member.creatingGroup.get();
    member.group = group->GetGUID();
    if (!member.group) return; // Create failed before assigning an ID; cleanup retains the construction receipt.
    auto* registered = sGroupMgr->GetGroupByGUID(member.group.GetCounter());
    if (registered && registered != group) return; // Keep the receipt; never replace another registered group.
    if (!registered) sGroupMgr->AddGroup(group);
    member.creatingGroup.release(); // Core owns the group only after the registration succeeded.
}

bool Usable(Job const& job, Player* bot, uint32 guid)
{
    auto* ai = bot ? GET_PLAYERBOT_AI(bot) : nullptr;
    DummyBotState state{Guild::IsCast(guid) && guid != job.human,
        bot && bot->IsInWorld(), bot && bot->IsAlive(), bot && bot->IsInCombat(),
        bot && (bot->GetGroup() || bot->GetGroupInvite()), bot && bot->GetMap()->Instanceable(),
        bot && (bot->InBattleground() || bot->InBattlegroundQueue()), Guild::OfflineRaidOwns(guid),
        HasNonPermanentBind(guid)};
    return DummyBotUsable(state) && bot->GetGuildId() == job.guild && ai && !IsSelfBot(bot) && !ai->GetMaster()
        && bot->GetSession() && !bot->GetSession()->IsLoggingOut()
        && !bot->IsBeingTeleported() && !bot->IsInFlight() && !bot->GetTransport() && !bot->GetVehicle()
        && !bot->GetTradeData() && !bot->IsUsingLfg() && !DcTestRunManager::Instance().IsReserved(bot->GetGUID());
}

Player* Driver(Job const& job)
{
    auto* driver = DcTestDriver::Get();
    return driver && driver->GetGUID() == job.driver && Identity(driver) == job.driverIdentity
        && driver->GetGUID().GetCounter() != Guild::HumanGuid() && !Guild::IsCast(driver->GetGUID().GetCounter())
        ? driver : nullptr;
}

void Progress(Job& job, std::string stage)
{
    auto event = job.event;
    event.state = "progress";
    event.stage = std::move(stage);
    Guild::PublishJob(event);
}

void Snapshot(Job const& job, Member& member, uint64 now)
{
    if (member.frozen) return;
    if (member.attacking) member.ms = now >= member.started ? now - member.started : 0;
    if (auto* table = TrainingTap::Instance().Dummy(job.slot))
        if (auto* row = table->Find(member.guid))
        {
            member.damage = row->damage;
            member.hits = 0;
            for (auto const& hit : row->done) member.hits = SaturatingAdd(member.hits, hit.hits);
        }
}

void Drop(Job const& job, Member& member, std::string reason, uint64 now)
{
    Snapshot(job, member, now);
    member.frozen = true;
    member.attacking = false;
    if (member.dropped.empty()) member.dropped = std::move(reason);
}

// The blocked-member line's detail: `missing=` for an ownership change, where the body is for a move.
void Block(Job const& job, Member& member, std::string reason, std::string const& detail = {})
{
    if (!member.blocked)
        LOG_WARN("module.leeroy", "mod-leeroy: dummy trial {} blocked member {}: {}{}{}; its return stops and "
            "training cleanup is required", job.event.id, member.guid, reason, detail.empty() ? "" : " ", detail);
    member.blocked = true;
    member.cleanupError = std::move(reason);
    RequireTrainingCleanup();
}

std::string Missing(Job const& job, Member const& member, Player* bot)
{
    return "missing=" + MissingOwnership(job, member, bot);
}

// Where a member that left the trial spot is, in ids and yards. On 2026-11-23 the block of 1507 (location_changed)
// could not say whether it was teleporting, on another map or instance, or only out of range.
std::string Where(Job const& job, Member const& member, Player* bot)
{
    if (!bot) return "where=unknown";
    auto const& place = kDummyPlaces[job.command.place];
    return Acore::StringFormat("map={} zone={} instance={} teleporting={} trial_yd={:.0f} trial_dz={:.0f} "
        "origin_map={}", bot->GetMapId(), bot->GetZoneId(), bot->GetInstanceId(), bot->IsBeingTeleported() ? 1 : 0,
        bot->GetDistance2d(place.x, place.y), bot->GetPositionZ() - place.z, member.origin.GetMapId());
}

// A near teleport in the fight (DecideDummyFightPlace: Wait), counted once per teleport: first and every 50th logged.
void NoteNearTeleport(Job const& job, Member& member)
{
    if (member.landing) return;
    member.landing = true;
    ++member.nearTeleports;
    if (member.nearTeleports == 1 || member.nearTeleports % 50 == 0)
        LOG_INFO("module.leeroy", "mod-leeroy: dummy trial {} near_teleport_wait guid={} count={}", job.event.id,
            member.guid, member.nearTeleports);
}

void NoteGroupGone(Member& member, Player* bot)
{
    if (member.groupReleaseStarted && !member.creatingGroup && !OwnedGroup(member)
        && bot && !bot->GetGroup() && !sGroupMgr->GetGroupByGUID(member.group.GetCounter()))
        member.groupReleased = true; // Only our recorded release intent can consume a disappeared group.
}

// The ownership a release step needs: the trial's own proof, or for letting go of a blocked member
// DummyLetGoOwned (the same body; the driver, or no master after our party's stock reset).
bool Ours(Job const& job, Member const& member, Player* bot, bool letGo)
{
    if (!letGo) return Owned(job, member, bot);
    auto* ai = bot ? GET_PLAYERBOT_AI(bot) : nullptr;
    return ai && DummyLetGoOwned(OwnerState(job, member, bot, true), member.released, member.groupReleased,
        !ai->GetMaster());
}

// One release step of a return or of letting go. Empty when it was taken, else why the body is no longer ours.
// Never dereference a Group after RemoveMember/Disband: both can delete it.
std::string ReleaseStep(Job const& job, Member& member, Player* bot, DummyReturnStep step, bool letGo)
{
    switch (step)
    {
        case DummyReturnStep::ReleaseMaster:
            bot->AttackStop();
            bot->CombatStopWithPets(true);
            GET_PLAYERBOT_AI(bot)->SetMaster(nullptr); // Admission required a masterless bot.
            member.released = true;
            return {};
        case DummyReturnStep::ResetStrategies:
            bot->AttackStop();
            bot->CombatStopWithPets(true);
            GET_PLAYERBOT_AI(bot)->Reset(false); // Clears trial targets/engine; ownership excludes logging out.
            GET_PLAYERBOT_AI(bot)->ResetStrategies();
            JournalReset(member.guid);
            member.resetDone = true; // A throwing reset remains false and is retried while ownership still holds.
            return {};
        case DummyReturnStep::ReleaseGroup:
        {
            PublishCreatedGroup(member);
            auto* group = OwnedGroup(member);
            if (!group || !Ours(job, member, bot, letGo)) return "group_changed";
            member.groupReleaseStarted = true; // Record before a possibly throwing/deleting core operation.
            if (member.creatingGroup)
            {
                // No ID means Create stopped before core membership/registration. Only discard an empty object.
                if (group->GetGUID() || group->GetMembersCount() || group->GetInviteeCount() || bot->GetGroup())
                    return "group_create_incomplete";
                member.creatingGroup.reset();
                member.groupReleased = true;
            }
            else if (group->GetMembersCount()) group->RemoveMember(bot->GetGUID());
            else group->Disband();
            return {}; // Re-resolve the player and GroupMgr identity on the next bounded iteration.
        }
        case DummyReturnStep::RestoreLeaderFlag:
            if (!member.leaderFlag) bot->RemovePlayerFlag(PLAYER_FLAGS_GROUP_LEADER);
            member.leaderRestored = true; // Group::Disband does not undo Create's leader flag in this core.
            return {};
        case DummyReturnStep::FinalStockReset:
            GET_PLAYERBOT_AI(bot)->Reset(false);
            GET_PLAYERBOT_AI(bot)->ResetStrategies(); // Factory defaults depend on the restored groupless state.
            JournalReset(member.guid);
            member.finalResetDone = true; // A failure retries only this step while released ownership still holds.
            return {};
        default:
            return {};
    }
}

void ReturnMember(Job& job, Member& member, uint64 now)
{
    if (member.returned || member.blocked) return;
    // Reset while the driver and solo group still prove control, then release both and issue one return.
    for (unsigned action = 0; action < 6; ++action)
    {
        auto* connected = ObjectAccessor::FindConnectedPlayer(Guid(member.guid));
        auto* bot = Guild::FindOnline(member.guid);
        NoteGroupGone(member, bot);
        Reassert(job, member, bot);
        DummyReturnState state{member.prepared, connected != nullptr, SameSession(member, connected), bot != nullptr,
            connected && connected->IsBeingTeleported(), AtOrigin(member, bot),
            member.released, member.resetDone, Owned(job, member, bot), OnTrialMap(job, bot),
            member.groupReleased, member.leaderRestored, member.finalResetDone,
            connected && connected->GetSession() && connected->GetSession()->IsLoggingOut()};
        auto const step = DecideDummyReturn(state);
        switch (step)
        {
            case DummyReturnStep::Complete:
                member.returned = true;
                member.cleanupError.clear();
                return;
            case DummyReturnStep::Wait:
                member.cleanupError = state.loggingOut ? "logging_out"
                    : state.teleporting ? "teleport_pending" : "offline";
                return;
            case DummyReturnStep::Block:
            {
                std::string reason = !state.sameSession ? "session_changed"
                    : !member.groupReleased && HasNonPermanentBind(member.guid) ? "instance_binding_changed"
                    : !state.owned ? "ownership_changed" : "location_changed";
                std::string const detail = reason == "ownership_changed" ? Missing(job, member, bot)
                    : reason == "location_changed" ? Where(job, member, bot) : "";
                Block(job, member, std::move(reason), detail);
                return;
            }
            case DummyReturnStep::ReleaseMaster:
            case DummyReturnStep::ResetStrategies:
            case DummyReturnStep::ReleaseGroup:
            case DummyReturnStep::RestoreLeaderFlag:
            case DummyReturnStep::FinalStockReset:
                if (auto reason = ReleaseStep(job, member, bot, step, false); !reason.empty())
                {
                    Block(job, member, std::move(reason));
                    return;
                }
                break;
            case DummyReturnStep::Teleport:
                if (now < member.retryAt) return;
                member.retryAt = now + kDummyRetryMs;
                member.returnIssued = bot->TeleportTo(member.origin);
                member.cleanupError = member.returnIssued ? "return_pending" : "return_refused";
                return;
        }
    }
}

// Past the release bound: let go of a blocked member that our solo party or the driver still holds, where it
// stands (DecideDummyLetGo). True once nothing of ours is left on it; it then stays unreturned and blocked.
bool LetGo(Job const& job, Member& member)
{
    for (unsigned action = 0; action < 6; ++action)
    {
        auto* connected = ObjectAccessor::FindConnectedPlayer(Guid(member.guid));
        auto* bot = Guild::FindOnline(member.guid);
        NoteGroupGone(member, bot);
        DummyReturnState state{member.prepared, connected != nullptr, SameBody(member, connected), bot != nullptr,
            connected && connected->IsBeingTeleported(), AtOrigin(member, bot),
            member.released, member.resetDone, Ours(job, member, bot, true), OnTrialMap(job, bot),
            member.groupReleased, member.leaderRestored, member.finalResetDone,
            connected && connected->GetSession() && connected->GetSession()->IsLoggingOut()};
        auto const step = DecideDummyLetGo(state);
        if (step == DummyReturnStep::Complete) return true;
        if (step == DummyReturnStep::Wait || step == DummyReturnStep::Block || step == DummyReturnStep::Teleport)
            return false;
        if (!ReleaseStep(job, member, bot, step, true).empty()) return false; // No longer provably ours: left alone.
    }
    return false;
}

void Prepare(Job& job, uint64 now)
{
    Progress(job, "moving");
    auto* driver = Driver(job);
    if (!driver) { job.life.Stop("driver_offline", now); return; }
    auto const& place = kDummyPlaces[job.command.place];
    std::size_t count = job.members.size();
    for (std::size_t index = 0; index < count; ++index)
    {
        auto& member = job.members[index];
        auto* bot = Guild::FindOnline(member.guid);
        if (!Usable(job, bot, member.guid))
        {
            member.dropped = HasNonPermanentBind(member.guid) ? "instance_bound" : "offline";
            member.returned = true;
            member.frozen = true;
            continue;
        }
        member.origin = WorldLocation(bot->GetMapId(), bot->GetPosition());
        member.player = Identity(bot);
        member.ai = Identity(GET_PLAYERBOT_AI(bot));
        member.session = Identity(bot->GetSession());
        member.prepared = true;
        // The body moves untouched: no party, no master, no reset (Setup does that at the dummy). A one-member
        // party formed here would be disbanded by the core when the far teleport lands (see Setup). Ranged specs
        // stand outside melee reach (RingSpot): inside it a mage's stock "blink back" fired 35 ms into the fight.
        Position const spot = RingSpot(place, count, index, PlayerbotAI::IsRanged(bot, true));
        member.moved = true; // Record intent before a possibly partially applied/throwing core operation.
        bool accepted = bot->TeleportTo(place.map, spot.GetPositionX(), spot.GetPositionY(), spot.GetPositionZ(),
            spot.GetOrientation());
        if (!accepted) Drop(job, member, "move_failed", now);
    }
    job.life.Moving();
}

// At the dummy, on the map the fight happens on. The core's LFGPlayerScript::OnPlayerMapChanged disbands a
// group of one member or fewer whenever its player is added to a map outside an LFG dungeon
// (DummySoloPartySurvivesMapEntry), so a party formed before a far teleport is gone when it lands; from
// 2026-11-10 to 11-14 every trial lost every member that way (ownership_changed). Formed here, the party
// lives until ReturnMember releases it, before the teleport home. Random bots clear an ungrouped master every
// AI tick, so each candidate gets its own exact solo party.
void Setup(Job& job, Member& member, Player* bot, uint64 now)
{
    auto* driver = Driver(job);
    if (!driver) { job.life.Stop("driver_offline", now); return; }
    auto* ai = GET_PLAYERBOT_AI(bot);
    // Playerbots fully resets a bot's AI (travel target, RPG task, last movement, engines) only when it acks a
    // worldport. A member already on the dummy's map came by a near teleport and kept all of it: on 2026-11-23
    // 1507, from Tanaris, never hit the dummy while the three from Northrend did, and left the trial spot
    // (location_changed). Every member starts the fight from the same clean AI, however it arrived.
    ai->Reset(true);
    member.creatingGroup = std::make_unique<Group>();
    member.groupIdentity = Identity(member.creatingGroup.get());
    member.leaderFlag = bot->HasPlayerFlag(PLAYER_FLAGS_GROUP_LEADER);
    member.leaderRestored = false;
    member.finalResetDone = false;
    member.groupReleased = false; // Creation intent precedes Create, including its leader-insertion side effects.
    bool created = false;
    try { created = member.creatingGroup->Create(bot); }
    catch (...) { PublishCreatedGroup(member); throw; }
    PublishCreatedGroup(member);
    if (!created || member.creatingGroup || !Owned(job, member, bot) || !bot->GetGroup())
    {
        Drop(job, member, "group_create_failed", now);
        return;
    }
    member.released = false;
    member.resetDone = false;
    ai->SetMaster(driver);
    // Stock follow must not drag the bot to the driver parked elsewhere.
    ai->ResetStrategies();
    JournalReset(member.guid);
    ai->ChangeStrategy("-follow", BOT_STATE_COMBAT);
    ai->ChangeStrategy("-follow", BOT_STATE_NON_COMBAT);
    if (!Owned(job, member, bot) || ai->HasStrategy("follow", BOT_STATE_COMBAT)
        || ai->HasStrategy("follow", BOT_STATE_NON_COMBAT))
    {
        Drop(job, member, "setup_failed", now);
        return;
    }
    member.setUp = true;
    member.setupAt = now;
}

void Move(Job& job, uint64 now)
{
    for (auto& member : job.members)
    {
        if (!member.dropped.empty()) { ReturnMember(job, member, now); continue; }
        auto* connected = ObjectAccessor::FindConnectedPlayer(Guid(member.guid));
        if (!SameSession(member, connected))
        {
            Drop(job, member, "offline", now);
            if (connected) Block(job, member, "session_changed");
            continue;
        }
        bool const teleporting = connected->IsBeingTeleported();
        auto* bot = teleporting ? nullptr : Guild::FindOnline(member.guid);
        if (bot) Reassert(job, member, bot); // Trial strategies are applied only at StartFight, so nothing is lost yet.
        bool const owned = !teleporting && Owned(job, member, bot);
        DummyMoveState state{teleporting, owned, owned && bot->IsAlive(), owned && bot->IsInCombat(),
            owned && AtTrial(job, bot), member.setUp, member.reasserted > 0,
            now >= member.setupAt ? now - member.setupAt : 0};
        switch (DecideDummyMove(state))
        {
            case DummyMoveStep::Wait:
            case DummyMoveStep::Settle:
                break;
            case DummyMoveStep::Block:
                Drop(job, member, "ownership_changed", now);
                Block(job, member, "ownership_changed", Missing(job, member, bot));
                break;
            case DummyMoveStep::Died: Drop(job, member, "died", now); break;
            case DummyMoveStep::Attacked: Drop(job, member, "attacked", now); break;
            case DummyMoveStep::Setup: Setup(job, member, bot, now); break;
            case DummyMoveStep::Arrived: member.arrived = true; break;
        }
    }
}

void StartFight(Job& job, uint64 now)
{
    std::vector<uint32> guids;
    Creature* dummy = nullptr;
    for (auto& member : job.members)
    {
        if (!member.dropped.empty()) continue;
        auto* bot = Guild::FindOnline(member.guid);
        Reassert(job, member, bot);
        if (!Owned(job, member, bot) || !AtTrial(job, bot))
        {
            Drop(job, member, "ownership_changed", now);
            continue;
        }
        if (!dummy) dummy = TrialDummy(job, bot);
        if (!dummy || dummy->GetMap() != bot->GetMap() || !bot->InSamePhase(dummy))
        {
            job.life.Stop("no_dummy", now);
            return;
        }
        if (!member.co.empty())
            if (auto reason = ApplyTrainingStrategies(bot, member.co, Engine::Combat); !reason.empty())
            {
                Drop(job, member, std::string(reason), now);
                continue;
            }
        guids.push_back(member.guid);
    }
    if (guids.empty()) { job.life.Stop("no_participants", now); return; }
    if (!TrainingAllowed()) { job.life.Stop(std::string(TrainingBlockReason()), now); return; }
    job.dummy = dummy->GetGUID();
    job.slot = TrainingTap::Instance().OpenDummy(guids, job.dummy, kDummyPlaces[job.command.place].map);
    if (job.slot < 0) { job.life.Stop("recorder_unavailable", now); return; }
    if (!job.life.Fighting(now)) { job.life.Stop("move_timeout", now); return; }
    for (auto& member : job.members)
    {
        if (!member.dropped.empty()) continue;
        auto* bot = Guild::FindOnline(member.guid);
        if (!Owned(job, member, bot) || !DummyAttack(GET_PLAYERBOT_AI(bot)).Begin(dummy))
            Drop(job, member, "attack_failed", now);
        else
        {
            member.attacking = true;
            member.started = now;
        }
    }
    Progress(job, "fighting");
}

void Fight(Job& job, uint64 now)
{
    auto& tap = TrainingTap::Instance();
    if (!tap.Dummy(job.slot)) { job.life.Stop("recorder_lost", now); return; }
    for (auto& member : job.members)
    {
        if (!member.attacking)
        {
            if (!member.dropped.empty()) ReturnMember(job, member, now);
            continue;
        }
        auto* bot = Guild::FindOnline(member.guid);
        if (!SameSession(member, bot)) Drop(job, member, "offline", now);
        else if (Reassert(job, member, bot)) Drop(job, member, "ai_reset", now); // The reset took its trial strategies.
        else if (bool const owned = Owned(job, member, bot); !owned)
        {
            Drop(job, member, "ownership_changed", now);
            Block(job, member, "ownership_changed", Missing(job, member, bot));
        }
        else if (!bot->IsAlive()) Drop(job, member, "died", now);
        else if (tap.DummyAttacked(job.slot, member.guid)) Drop(job, member, "attacked", now);
        else if (auto const place = DecideDummyFightPlace({owned, bot->IsBeingTeleportedNear(),
                bot->IsBeingTeleportedFar(), bot->GetMapId() == kDummyPlaces[job.command.place].map,
                bot->GetInstanceId() != 0});
            place == DummyFightPlace::Block)
        {
            Drop(job, member, "moved", now);
            Block(job, member, "location_changed", Where(job, member, bot));
        }
        else if (place == DummyFightPlace::Wait) NoteNearTeleport(job, member);
        else
        {
            member.landing = false;
            auto* dummy = bot->GetMap()->GetCreature(job.dummy);
            if (!dummy || !dummy->IsAlive()) { job.life.Stop("no_dummy", now); break; }
            if (bot->GetDistance(dummy) > 30.0f)
            {
                if (!member.outsideSince) member.outsideSince = now;
                if (now >= *member.outsideSince && now - *member.outsideSince >= 5000)
                {
                    LOG_INFO("module.leeroy", "mod-leeroy: dummy trial {} dropped member {}: moved dummy_yd={:.0f} {}",
                        job.event.id, member.guid, bot->GetDistance(dummy), Where(job, member, bot));
                    Drop(job, member, "moved", now);
                }
            }
            else member.outsideSince.reset();
            Snapshot(job, member, now);
        }
    }
}

void BeginReturn(Job& job, uint64 now)
{
    if (job.returning) return;
    for (auto& member : job.members)
    {
        Snapshot(job, member, now);
        member.frozen = true;
        member.attacking = false;
        if (member.dropped.empty() && !job.life.Reason().empty()) member.dropped = job.life.Reason();
    }
    TrainingTap::Instance().CloseDummy(job.slot);
    job.slot = -1;
    job.returning = true;
    Progress(job, "returning");
}

void PublishTerminal(Job& job, bool cleanupFailed)
{
    if (job.terminalPublished) return;
    job.event.guids.clear();
    job.event.skipped.clear();
    for (auto const& member : job.members)
    {
        auto dropped = DummyDropReport(member.dropped, member.cleanupError);
        LeeroyModule::Instance().Publish(EncodeEvent(NowUnixMs(), {{"type", "dummy_result"}, {"id", job.event.id},
            {"guid", member.guid}, {"damage", member.damage}, {"ms", member.ms}, {"hits", member.hits},
            {"dropped", dropped}, {"damage_basis", "requested_pre_ai"}, {"spell_basis", "best_effort_thread_pair"}}),
            false);
        if (dropped.empty()) job.event.guids.push_back(member.guid);
        else job.event.skipped.emplace_back(member.guid, std::move(dropped));
    }
    job.event.state = cleanupFailed || job.life.TerminalFailed() ? "failed" : "done";
    job.event.stage.clear();
    job.event.reason = cleanupFailed ? "cleanup_failed:" + job.life.Reason() : job.life.Reason();
    Guild::PublishJob(job.event);
    job.terminalPublished = true;
    LOG_INFO("module.leeroy", "mod-leeroy: dummy trial {} {}: {}", job.event.id, job.event.state, job.event.reason);
}

bool AllReturned(Job const& job)
{
    return std::all_of(job.members.begin(), job.members.end(), [](Member const& member) { return member.returned; });
}

DummyHoldState HoldState(Job const& job, Member const& member)
{
    auto* group = OwnedGroup(member);
    auto* connected = ObjectAccessor::FindConnectedPlayer(Guid(member.guid));
    auto* ai = connected ? GET_PLAYERBOT_AI(connected) : nullptr;
    return {member.prepared, member.returned, member.creatingGroup != nullptr,
        group && group->IsMember(Guid(member.guid)),
        connected && Identity(connected) == member.player && Identity(connected->GetSession()) == member.session
            && Identity(ai) == member.ai,
        ai && Identity(ai->GetMaster()) == job.driverIdentity};
}

std::vector<uint32> Unreturned(Job const& job)
{
    std::vector<uint32> guids;
    for (auto const& member : job.members)
        if (member.prepared && !member.returned) guids.push_back(member.guid);
    return guids;
}

std::string GuidList(std::vector<uint32> const& guids)
{
    std::string text;
    for (auto guid : guids) text += (text.empty() ? "" : ",") + std::to_string(guid);
    return text.empty() ? "-" : text;
}

// Ten minutes after cleanup_failed the job sorts its unreturned members (DummyFateAfterBound). It keeps
// and fences a member it still holds or is still returning; it abandons a blocked member it no longer
// holds (cleanup_abandoned, never touched again, no longer fenced by the trial), journaled or not: its
// journal keeps fencing that body and RestoreTrainingBots restores it. With nobody kept the job ends: its
// slot frees and its global fence can clear. The dummy_cleanup "released" receipt names the members
// whose return was never proved. A blocked member our party or driver still holds is first let go where it
// stands while it is still provably ours (DecideDummyLetGo), so it cannot keep the job for good.
bool ReleaseStuck(Job& job, uint64 now)
{
    if (job.life.Phase() != DummyPhase::Recovery || now < job.releaseCheckAt) return false;
    job.releaseCheckAt = now + 1000;
    uint64 const since = now >= job.failedAt ? now - job.failedAt : 0;
    if (!DummyBoundPassed(job.life.FailureReported(), since)) return false;
    std::vector<uint32> kept;
    for (auto& member : job.members)
    {
        if (member.abandoned) continue;
        if (member.blocked && !member.letGo && DummyMemberHeld(HoldState(job, member)) && LetGo(job, member))
        {
            member.letGo = true;
            LOG_WARN("module.leeroy", "mod-leeroy: dummy trial {} let_go guid={} blocked={}; our party and master "
                "released where it stands, without the teleport home", job.event.id, member.guid, member.cleanupError);
        }
        auto const fate = DummyFateAfterBound(HoldState(job, member), member.blocked);
        if (DummyTrialFences(fate)) kept.push_back(member.guid);
        else if (fate == DummyMemberFate::Abandon)
        {
            member.abandoned = true;
            LOG_WARN("module.leeroy", "mod-leeroy: dummy trial {} cleanup_abandoned guid={} blocked={} journal={}; "
                "the job holds none of it and never touches it again", job.event.id, member.guid,
                member.cleanupError, HasTrainingJournal(member.guid));
        }
    }
    job.bounded = true;
    job.held = kept;
    if (DecideDummyRelease(true, since, static_cast<uint32>(kept.size())) == DummyRelease::Keep)
    {
        if (!job.heldNoted)
            LOG_WARN("module.leeroy", "mod-leeroy: dummy trial {} cleanup_held kept={} after_ms={}; only these "
                "members stay fenced and the job keeps its slot", job.event.id, GuidList(kept), since);
        job.heldNoted = true;
        return false;
    }
    auto const unfinished = Unreturned(job);
    LOG_WARN("module.leeroy", "mod-leeroy: dummy trial {} cleanup_released after_ms={} unfinished={} (journals "
        "are retained and fence their own bodies)", job.event.id, since, GuidList(unfinished));
    LeeroyModule::Instance().Publish(EncodeEvent(NowUnixMs(),
        MakeDummyCleanup(job.event.id, "released", unfinished, since)), false);
    NoteTrainingCleanupReleased("dummy_trial " + job.event.id + " unfinished=" + GuidList(unfinished));
    return true;
}
}
#endif

bool DummyTrialRunning()
{
#if LEEROY_HAS_DUNGEON_CLEAR
    return active != nullptr;
#else
    return false;
#endif
}

bool DummyTrialOwns(std::uint32_t guid)
{
#if LEEROY_HAS_DUNGEON_CLEAR
    if (!active) return false;
    auto const& guids = active->bounded ? active->held : active->command.guids;
    return std::find(guids.begin(), guids.end(), guid) != guids.end();
#else
    (void)guid;
    return false;
#endif
}

std::string DummyTrialCleanupDetail()
{
#if LEEROY_HAS_DUNGEON_CLEAR
    return active ? active->event.id + " unreturned=" + GuidList(Unreturned(*active)) : std::string();
#else
    return {};
#endif
}

LeeroyCommandResult HandleDummyTrial(nlohmann::json const& message)
{
    std::string error;
    auto command = ParseDummyTrial(message, error);
    if (!command) return {false, error};
#if !LEEROY_HAS_DUNGEON_CLEAR
    return {false, "driver_unavailable:no_dungeon_clear"};
#else
    if (!TrainingAllowed()) return {false, std::string(TrainingBlockReason())};
    if (active) return {false, "busy"};
    if (!Guild::KeepCast()) return {false, "keep_cast_off"};
    if (HumanConnected(Guild::HumanGuid())) return {false, "human_online"};
    for (auto guid : command->guids)
    {
        if (!Guild::IsCast(guid) || guid == Guild::HumanGuid()) return {false, "not_cast:" + std::to_string(guid)};
        if (Realm::RealmOwns(guid)) return {false, "busy:realm_owner:" + std::to_string(guid)};
        if (Guild::OfflineRaidOwns(guid)) return {false, "busy:" + std::to_string(guid)};
        if (Guild::StringStayHeld(guid)) return {false, "busy:string:" + std::to_string(guid)};
        if (HasNonPermanentBind(guid)) return {false, "instance_bound:" + std::to_string(guid)};
    }
    auto readiness = DcTestDriver::Ensure(&error);
    if (readiness == DcTestDriver::Readiness::PendingLogin) return {false, "driver_pending"};
    if (readiness != DcTestDriver::Readiness::Ready) return {false, "driver_unavailable:" + error};
    auto* driver = DcTestDriver::Get();
    if (!driver || driver->GetGUID().GetCounter() == Guild::HumanGuid()
        || Guild::IsCast(driver->GetGUID().GetCounter())) return {false, "driver_unavailable:driver_not_separate"};
    if (HumanConnected(Guild::HumanGuid())) return {false, "human_online"};
    auto job = std::make_unique<Job>(std::move(*command), Now());
    job->event.id = message.at("id").get<std::string>();
    job->event.kind = "dummy_trial";
    job->guild = LeeroyModule::Instance().GuildId();
    job->human = Guild::HumanGuid();
    job->driver = driver->GetGUID();
    job->driverIdentity = Identity(driver);
    job->epoch = TrainingTap::Instance().TrainingEpoch();
    for (std::size_t i = 0; i < job->command.guids.size(); ++i)
    {
        Member member;
        member.guid = job->command.guids[i];
        member.co = job->command.co[i];
        job->members.push_back(std::move(member));
    }
    active = std::move(job); // Ack first; all bot setup, movement and progress events start on the world tick.
    return {true, {}};
#endif
}

void EndDummyTrial(std::string const& reason)
{
#if LEEROY_HAS_DUNGEON_CLEAR
    if (active) active->life.Stop(reason, Now());
#else
    (void)reason;
#endif
}

void DummyPlayerLogout(std::uint32_t guid)
{
#if LEEROY_HAS_DUNGEON_CLEAR
    if (!active) return;
    if (active->driver.GetCounter() == guid) active->life.Stop("driver_offline", Now());
    for (auto& member : active->members)
        if (member.guid == guid && member.prepared && !member.returned)
        {
            // A new login is a different lease even if allocator addresses happen to be reused.
            if (!member.blocked)
                LOG_WARN("module.leeroy", "mod-leeroy: dummy trial {} blocked member {}: session_logged_out",
                    active->event.id, guid);
            member.blocked = true;
            member.cleanupError = "session_logged_out";
        }
#else
    (void)guid;
#endif
}

void TickDummyTrial(std::uint32_t /*diff*/)
{
#if LEEROY_HAS_DUNGEON_CLEAR
    if (!active) return;
    auto& job = *active;
    auto now = Now();
    try
    {
        if (job.human != Guild::HumanGuid() || job.guild != LeeroyModule::Instance().GuildId())
            job.life.Stop("owner_changed", now);
        else if (!Driver(job)) job.life.Stop("driver_offline", now);
        bool const allowed = TrainingAllowed(), keepCast = Guild::KeepCast();
        bool enabled = allowed && keepCast && job.epoch == TrainingTap::Instance().TrainingEpoch();
        std::string_view const off = !allowed ? TrainingBlockReason() : !keepCast ? "keep_cast_off" : "training_off";
        bool arrived = std::all_of(job.members.begin(), job.members.end(),
            [](Member const& member) { return member.arrived || !member.dropped.empty(); });
        bool fighting = std::any_of(job.members.begin(), job.members.end(),
            [](Member const& member) { return member.attacking; });
        auto step = job.life.Step(now, {enabled, HumanConnected(job.human) || HumanConnected(Guild::HumanGuid()),
            arrived, fighting, AllReturned(job), off});
        if (job.life.Phase() == DummyPhase::Returning || job.life.Phase() == DummyPhase::Recovery
            || step == DummyStep::Finish) BeginReturn(job, now);
        switch (step)
        {
            case DummyStep::Prepare: Prepare(job, now); break;
            case DummyStep::Move: Move(job, now); break;
            case DummyStep::StartFight: StartFight(job, now); break;
            case DummyStep::Fight: Fight(job, now); break;
            case DummyStep::Return:
                for (auto& member : job.members) ReturnMember(job, member, now);
                break;
            case DummyStep::ReportFailure:
                job.failedAt = now;
                PublishTerminal(job, true);
                RequireTrainingCleanup();
                break;
            case DummyStep::Finish:
                PublishTerminal(job, false);
                active.reset();
                break;
            case DummyStep::Recovered:
                LOG_INFO("module.leeroy", "mod-leeroy: dummy trial {} retained recovery completed", job.event.id);
                LeeroyModule::Instance().Publish(EncodeEvent(NowUnixMs(), MakeDummyCleanup(job.event.id, "restored",
                    {}, now >= job.failedAt ? now - job.failedAt : 0)), false);
                active.reset();
                break;
            case DummyStep::Wait: break;
        }
        // A phase action can discover a failure. Freeze counters and start owned cleanup on this same barrier.
        if (active && active->life.Phase() == DummyPhase::Returning && !active->returning)
        {
            BeginReturn(*active, now);
            for (auto& member : active->members) ReturnMember(*active, member, now);
        }
        if (active && ReleaseStuck(*active, now)) active.reset();
    }
    catch (...)
    {
        if (active)
        {
            active->life.Stop("internal_error", now);
            RequireTrainingCleanup();
        }
    }
#endif
}

void ShutdownDummyTrial()
{
#if LEEROY_HAS_DUNGEON_CLEAR
    EndDummyTrial("shutdown");
    TickDummyTrial(0); // One bounded world-barrier attempt; no blocking wait for worldport acknowledgements.
    if (active && !active->terminalPublished) PublishTerminal(*active, true);
#endif
}
}
