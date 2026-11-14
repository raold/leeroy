// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "RaidLead.h"
#include "GuildPorts.h"
#include "PlayStyle.h"
#include "LeeroyRaidLeadPolicy.h"
#include "Realm/RealmPorts.h"

#include "AiObjectContext.h"
#include "Creature.h"
#include "Event.h"
#include "Group.h"
#include "Log.h"
#include "Map.h"
#include "MotionMaster.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "Playerbots.h"
#include "PositionValue.h"
#include "WorldPacket.h"
#include "WorldSession.h"

#include <algorithm>
#include <chrono>
#include <limits>
#include <map>
#include <string_view>
#include <utility>
#include <vector>

namespace Leeroy::Guild
{
namespace
{
// World thread only: the bots a hold made passive, until RaidLead's tick lifts them.
std::map<uint32, RaidHold> held;

LeeroyCommandResult Fail(std::string reason) { return {false, std::move(reason)}; }
ObjectGuid Guid(uint32 low) { return ObjectGuid::Create<HighGuid::Player>(low); }

std::uint64_t NowMs()
{
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count());
}

// The group the owner is in, unless it is a battleground or battlefield raid.
Group* HisGroup(Player* human)
{
    Group* group = human ? human->GetGroup() : nullptr;
    if (!group || group->isBGGroup() || group->isBFGroup())
        return nullptr;
    return group;
}

// Anyone of his group on his map, him included, in combat: his pull has started a fight.
bool GroupFighting(Player* human, Group* group)
{
    if (!human || !group)
        return false;
    for (GroupReference* ref = group->GetFirstMember(); ref; ref = ref->next())
        if (Player* member = ref->GetSource(); member && member->GetMap() == human->GetMap() && member->IsInCombat())
            return true;
    return false;
}

// His selection, when it is a living creature on his map he could attack.
Unit* HisHostileTarget(Player* human)
{
    Unit* target = human ? human->GetSelectedUnit() : nullptr;
    if (!target || !target->ToCreature() || !target->IsAlive() || !target->IsInWorld()
        || target->GetMap() != human->GetMap() || !human->IsValidAttackTarget(target))
        return nullptr;
    return target;
}

// Every "+name" is on and every "-name" off in that state afterwards: the order was applied, not only requested.
bool ChangeAndCheck(PlayerbotAI* ai, std::string_view changes, BotState state)
{
    if (changes.empty())
        return true;
    ai->ChangeStrategy(std::string(changes), state);
    std::string_view rest = changes;
    for (;;)
    {
        auto const end = rest.find(',');
        auto const part = rest.substr(0, end);
        if (part.size() > 1 && ai->HasStrategy(std::string(part.substr(1)), state) != (part.front() == '+'))
            return false;
        if (end == std::string_view::npos)
            return true;
        rest.remove_prefix(end + 1);
    }
}

void SetPositions(PlayerbotAI* ai, WorldObject const* at)
{
    PositionMap& positions = ai->GetAiObjectContext()->GetValue<PositionMap&>("position")->Get();
    for (char const* name : {"stay", "return"})
    {
        if (at)
            positions[name].Set(at->GetPositionX(), at->GetPositionY(), at->GetPositionZ(), at->GetMapId());
        else
            positions[name].Reset();
    }
}

// As GroupRezJob and playerbots' AutoReleaseSpiritAction: repaired, then released; playerbots runs the ghost back.
void Release(Player* bot)
{
    bot->DurabilityRepairAll(false, 1.0f, false);
    WorldPacket packet(CMSG_REPOP_REQUEST);
    packet << uint8(0);
    bot->GetSession()->HandleRepopRequestOpcode(packet);
}

std::string Taunt(Player* bot, PlayerbotAI* ai, Unit* target)
{
    std::string const spell(TauntSpell(bot->getClass()));
    if (auto why = TauntRefusal(target != nullptr, target && target->GetVictim() == bot, !spell.empty()); !why.empty())
        return std::string(why);
    if (!ai->CanCastSpell(spell, target) || !ai->CastSpell(spell, target))
        return "failed";
    return {};
}

// Playerbots' own "move from group", once: no strategy, so nothing passive stays behind. A bot already that far
// from everyone in his group is done.
std::string MoveAway(Player* bot, PlayerbotAI* ai, Group* group, float yards)
{
    float nearest = std::numeric_limits<float>::max();
    for (GroupReference* ref = group->GetFirstMember(); ref; ref = ref->next())
        if (Player* member = ref->GetSource(); member && member != bot && member->IsAlive()
            && member->GetMapId() == bot->GetMapId())
            nearest = std::min(nearest, bot->GetDistance2d(member));
    if (nearest >= yards)
        return {};
    return ai->DoSpecificAction("move from group", Event("leeroy", std::to_string(static_cast<int>(yards))), true)
        ? std::string() : std::string("failed");
}

// One move to just behind him; the bot's own combat logic goes on from there.
bool ToHisSide(Player* bot, Player* human)
{
    float x = 0, y = 0, z = 0;
    if (!human->GetClosePoint(x, y, z, bot->GetCombatReach(), 3.0f, 3.14159265f))
        return false;
    bot->GetMotionMaster()->MovePoint(0, x, y, z);
    return true;
}

void Lift(PlayerbotAI* ai)
{
    ai->ChangeStrategy("-passive", BOT_STATE_NON_COMBAT);
    ai->ChangeStrategy("-passive", BOT_STATE_COMBAT);
}

LeeroyCommandResult HandleRaidLead(nlohmann::json const& message)
{
    std::string error;
    auto const cmd = ParseRaidLead(message, error);
    if (!cmd)
        return Fail(std::move(error));
    if (!GuildEnabled())
        return Fail("disabled");
    Player* human = FindOnline(HumanGuid());
    Group* group = HisGroup(human);
    ObjectGuid const member = Guid(cmd->guid);
    RaidLeadFacts facts;
    facts.enabled = RaidLeadEnabled();
    facts.ownerOnline = human != nullptr;
    facts.ownerLeads = group && group->GetLeaderGUID() == human->GetGUID();
    facts.memberInGroup = group && cmd->guid && group->IsMember(member);
    facts.memberIsOwner = cmd->guid && cmd->guid == HumanGuid();
    facts.memberIsCast = cmd->guid && IsCast(cmd->guid);
    facts.fenced = GroupHasRealmOwner(group);
    Unit* target = cmd->action == "mark" ? HisHostileTarget(human) : nullptr;
    facts.targetHostile = target != nullptr;
    if (auto why = RaidLeadRefusal(cmd->action, facts); !why.empty())
        return Fail(std::string(why));
    if (cmd->action == "probe")
        return {true, {}};
    if (cmd->action == "sync")
    {
        // A battleground or battlefield raid is not his group; outside one, no group is no group.
        if (group || !human->GetGroup())
        {
            std::vector<uint32> members;
            if (group)
                for (auto const& slot : group->GetMemberSlots())
                    members.push_back(slot.guid.GetCounter());
            LeeroyModule::Instance().Publish(Leeroy::EncodeGroup(NowUnixMs(),
                group ? group->GetLeaderGUID().GetCounter() : 0, group && group->isRaidGroup(), members), false);
        }
        return {true, {}};
    }
    // Set server-side for her: no raid assistant flag, so playerbots' assist orders never shift.
    std::uint8_t const icon = *RaidIconIndex(cmd->icon);
    group->SetTargetIcon(icon, member, target->GetGUID());
    if (group->GetTargetIcon(icon) != target->GetGUID())
        return Fail("failed");
    return {true, {}};
}
}

bool RaidLeadEnabled() { return LeeroyModule::Instance().Config().raidLeadEnable; }

std::string ApplyRaidOrder(Player* bot, BotDoCmd const& command)
{
    uint32 const guid = bot->GetGUID().GetCounter();
    Player* human = FindOnline(HumanGuid());
    Group* group = HisGroup(human);
    bool const lift = command.param == "lift";
    RaidOrderBody body;
    body.enabled = RaidLeadEnabled();
    body.ownerOnline = human != nullptr;
    body.ownerLeads = group && group->GetLeaderGUID() == human->GetGUID();
    body.inHisGroup = group && bot->GetGroup() == group;
    body.sameMap = human && bot->GetMap() == human->GetMap();
    body.alive = bot->IsAlive();
    body.ghost = bot->HasPlayerFlag(PLAYER_FLAGS_GHOST);
    body.tankOrHealer = PlayerbotAI::IsTank(bot, true) || PlayerbotAI::IsHeal(bot, true);
    if (Realm::RealmOwns(guid) || GroupHasRealmOwner(group))
        body.fence = "busy:realm_owner";
    else if (!lift)
        body.fence = Realm::NativeOwnershipReason(guid);
    if (auto why = RaidOrderRefusal(command.param, body); !why.empty())
        return std::string(why);
    RaidOrderPlan const plan = *PlanRaidOrder(command.param);
    PlayerbotAI* ai = GET_PLAYERBOT_AI(bot);
    if (!ai || IsSelfBot(bot))
        return "not_bot";
    if (plan.release)
    {
        Release(bot);
        return bot->HasPlayerFlag(PLAYER_FLAGS_GHOST) ? std::string() : std::string("failed");
    }
    if (lift)
    {
        Lift(ai);
        held.erase(guid);
        return ai->HasStrategy("passive", BOT_STATE_COMBAT) ? std::string("failed") : std::string();
    }
    if (plan.lust)
    {
        std::string const spell(LustSpell(bot->GetTeamId() == TEAM_HORDE));
        if (!bot->IsNonMeleeSpellCast(false) && ai->CanCastSpell(spell, bot) && ai->CastSpell(spell, bot))
            return {};
        return "failed";
    }
    if (plan.moveAway > 0)
        return MoveAway(bot, ai, group, plan.moveAway);
    if (plan.toHisSide)
        return ToHisSide(bot, human) ? std::string() : std::string("failed");
    Unit* target = (plan.attackHisTarget || plan.taunt) ? HisHostileTarget(human) : nullptr;
    if (plan.taunt)
        return Taunt(bot, ai, target);
    // Explicit strategy writers unwind a play style first (PlayStyle README).
    if (!PlayStyle::Restore(bot))
        return "playstyle_cleanup";
    // His order: the bots he leads follow and attack for him, as RaidHabits makes them.
    if (ai->GetMaster() != human)
        ai->SetMaster(human);
    WorldObject const* at = human;
    if (plan.stayAtTarget && command.targetGuid && command.targetGuid != HumanGuid())
    {
        Player* other = FindOnline(command.targetGuid);
        if (!other || other->GetGroup() != group || other->GetMap() != bot->GetMap() || !other->IsAlive())
            return "no_target";
        at = other;
    }
    if (plan.attackHisTarget && !target)
        return "no_target";
    if (plan.stayHere || plan.stayAtTarget)
        ai->Reset();
    if (!ChangeAndCheck(ai, plan.nonCombat, BOT_STATE_NON_COMBAT) || !ChangeAndCheck(ai, plan.combat, BOT_STATE_COMBAT))
        return "failed";
    if (plan.hold)
        held[guid] = {NowMs() + kRaidHoldMs, GroupFighting(human, group)};
    else
        held.erase(guid); // every other strategy order here clears "passive"
    if (plan.clearPositions)
        SetPositions(ai, nullptr);
    if (plan.stayHere)
        SetPositions(ai, bot);
    if (plan.stayAtTarget)
        SetPositions(ai, at);
    if (plan.rtiSkull)
    {
        auto* rti = ai->GetAiObjectContext()->GetValue<std::string>("rti");
        if (!rti)
            return "failed";
        rti->Set("skull");
        if (rti->Get() != "skull")
            return "failed";
    }
    if (plan.attackHisTarget)
    {
        bool const attacked = ai->DoSpecificAction("attack my target", Event("leeroy"), true);
        if (!AttackCounts(attacked, bot->GetVictim() == target, bot->IsWithinLOSInMap(target)))
            return "failed";
    }
    return {};
}

void TickRaidLead()
{
    if (held.empty())
        return;
    Player* human = FindOnline(HumanGuid());
    Group* group = HisGroup(human);
    bool const fighting = GroupFighting(human, group);
    bool const enabled = RaidLeadEnabled();
    std::uint64_t const now = NowMs();
    for (auto it = held.begin(); it != held.end();)
    {
        uint32 const guid = it->first;
        // FindOnline is empty between maps and after a logout; the hold waits for the body to be back.
        Player* bot = FindOnline(guid);
        HoldStep const step = DecideHold(it->second, now, fighting, bot != nullptr,
            bot && group && bot->GetGroup() == group, enabled);
        if (step == HoldStep::Keep || Realm::RealmOwns(guid))
        {
            ++it; // never touch a body a Realm lease holds; try again next tick
            continue;
        }
        if (PlayerbotAI* ai = GET_PLAYERBOT_AI(bot))
        {
            Lift(ai);
            LOG_INFO("module.leeroy", "mod-leeroy: raidlead_hold_lifted guid={}", guid);
        }
        it = held.erase(it); // lifted, or no bot AI left to hold anything
    }
}

void RegisterRaidLeadHandler() { RegisterCommand("raid_lead", HandleRaidLead); }
}
