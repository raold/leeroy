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
#include "Map.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "Playerbots.h"
#include "PositionValue.h"
#include "WorldPacket.h"
#include "WorldSession.h"

#include <string_view>
#include <utility>

namespace Leeroy::Guild
{
namespace
{
LeeroyCommandResult Fail(std::string reason) { return {false, std::move(reason)}; }
ObjectGuid Guid(uint32 low) { return ObjectGuid::Create<HighGuid::Player>(low); }

// The group the owner is in, unless it is a battleground or battlefield raid.
Group* HisGroup(Player* human)
{
    Group* group = human ? human->GetGroup() : nullptr;
    if (!group || group->isBGGroup() || group->isBFGroup())
        return nullptr;
    return group;
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
    facts.raid = group && group->isRaidGroup();
    facts.memberInGroup = group && group->IsMember(member);
    facts.memberIsOwner = cmd->guid == HumanGuid();
    facts.memberIsCast = IsCast(cmd->guid);
    facts.memberIsAssistant = group && (group->IsLeader(member) || group->IsAssistant(member));
    facts.fenced = GroupHasRealmOwner(group);
    Unit* target = cmd->action == "mark" ? HisHostileTarget(human) : nullptr;
    facts.targetHostile = target != nullptr;
    if (auto why = RaidLeadRefusal(cmd->action, facts); !why.empty())
        return Fail(std::string(why));
    if (cmd->action == "mark")
    {
        std::uint8_t const icon = *RaidIconIndex(cmd->icon);
        group->SetTargetIcon(icon, member, target->GetGUID());
        if (group->GetTargetIcon(icon) != target->GetGUID())
            return Fail("failed");
        return {true, {}};
    }
    bool const assist = cmd->action == "assist";
    // Never the leader flag: he stays the group's leader, so playerbots never resets every bot's strategies.
    group->SetGroupMemberFlag(member, assist, MEMBER_FLAG_ASSISTANT);
    group->SendUpdate();
    if (group->IsAssistant(member) != assist)
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
    RaidOrderBody body;
    body.enabled = RaidLeadEnabled();
    body.ownerOnline = human != nullptr;
    body.ownerLeads = group && group->GetLeaderGUID() == human->GetGUID();
    body.inHisGroup = group && bot->GetGroup() == group;
    body.sameMap = human && bot->GetMap() == human->GetMap();
    body.alive = bot->IsAlive();
    body.ghost = bot->HasPlayerFlag(PLAYER_FLAGS_GHOST);
    body.fence = (Realm::RealmOwns(guid) || GroupHasRealmOwner(group)) ? std::string("busy:realm_owner")
        : Realm::NativeOwnershipReason(guid);
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
    Unit* target = (plan.attackHisTarget || plan.taunt) ? HisHostileTarget(human) : nullptr;
    if (plan.attackHisTarget && !target)
        return "no_target";
    if (plan.taunt)
        return Taunt(bot, ai, target);
    if (plan.stayHere || plan.stayAtTarget)
        ai->Reset();
    if (!ChangeAndCheck(ai, plan.nonCombat, BOT_STATE_NON_COMBAT) || !ChangeAndCheck(ai, plan.combat, BOT_STATE_COMBAT))
        return "failed";
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
    if (plan.attackHisTarget && !ai->DoSpecificAction("attack my target", Event("leeroy"), true))
        return "failed";
    return {};
}

void RegisterRaidLeadHandler() { RegisterCommand("raid_lead", HandleRaidLead); }
}
