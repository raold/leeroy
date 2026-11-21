// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "GroupRezJob.h"
#include "GuildPorts.h"
#include "LeeroyGroupRezPolicy.h"
#include "Realm/RealmPorts.h"
#include "CombatManager.h"
#include "Config.h"
#include "Creature.h"
#include "Event.h"
#include "Group.h"
#include "Log.h"
#include "Map.h"
#include "MotionMaster.h"
#include "PathGenerator.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "Playerbots.h"
#include "Spell.h"
#include "SpellInfo.h"
#include "SpellMgr.h"
#include "WorldPacket.h"
#include "WorldSession.h"
#include <algorithm>
#include <chrono>
#include <map>
#include <set>
#include <string>
#include <utility>
#include <vector>

namespace Leeroy::Guild
{
namespace
{
// MovePoint id of a rezzer walking in to a body ("LRZ").
constexpr std::uint32_t kApproachPointId = 0x4c525a;
// Playerbots' AI is held this long on each second of a walk-in, so its follow does not undo it.
constexpr std::uint32_t kApproachHoldMs = 1500;

bool enabled = true;
std::uint64_t releaseMs = kGroupRezDefaultReleaseSeconds * 1000ull;
std::uint32_t untilPass = 0;
// World thread only. Keyed by the dead member; dropped once they stand up or release.
std::map<std::uint32_t, GroupRezMemory> memories;
// Keyed by the rezzer: refusals and drinks hold for every body.
GroupRezCasters casters;
std::map<std::uint32_t, std::uint32_t> walkingTo; // rezzer -> body it was sent toward
std::map<GroupRezBlock, std::uint64_t> blocksLogged;
std::set<std::pair<std::uint32_t, std::uint32_t>> approachesLogged; // (body, rezzer)
std::set<std::pair<std::uint32_t, std::uint32_t>> drinksLogged;     // (body, rezzer)
std::map<std::pair<std::uint32_t, int>, std::uint64_t> refusalsLogged; // (rezzer, result) -> when
std::map<std::uint32_t, std::uint64_t> combatClearsLogged;           // rezzer -> when

std::uint64_t NowMs()
{
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count());
}

bool IsRez(SpellInfo const* info)
{
    return info && (info->HasEffect(SPELL_EFFECT_RESURRECT) || info->HasEffect(SPELL_EFFECT_RESURRECT_NEW));
}

bool BeingRezzed(Player* body, std::vector<Player*> const& members)
{
    for (Player* member : members)
        if (Spell* spell = member->GetCurrentSpell(CURRENT_GENERIC_SPELL))
            if (spell->m_targets.GetUnitTargetGUID() == body->GetGUID() && IsRez(spell->GetSpellInfo()))
                return true;
    return false;
}

std::uint32_t KnownRez(Player* player)
{
    if (auto const* ranks = RezRanks(player->getClass()))
        for (auto spell : *ranks)
            if (player->HasSpell(spell))
                return spell;
    return 0;
}

std::string FenceCode(std::uint32_t guid)
{
    return Realm::RealmOwns(guid) ? std::string("busy:realm_owner") : Realm::NativeOwnershipReason(guid);
}

bool Fenced(std::uint32_t guid)
{
    return !FenceCode(guid).empty();
}

bool IsGroupBot(Player* player)
{
    return GET_PLAYERBOT_AI(player) && !IsSelfBot(player);
}

bool Grounded(Player* player)
{
    if (player->IsFalling())
        return false;
    float const ground = player->GetMapHeight(player->GetPositionX(), player->GetPositionY(), player->GetPositionZ());
    return ground > INVALID_HEIGHT && player->GetPositionZ() - ground <= kGroupRezGroundYards;
}

bool InFlightForm(Player* player)
{
    return player->GetShapeshiftForm() == FORM_FLIGHT || player->GetShapeshiftForm() == FORM_FLIGHT_EPIC;
}

// The enemy this combat reference holds the member to, when it is a real fight: a living unit on
// its map within kGroupRezEngagedYards that is not evading. Inside a dungeon the core never ends
// combat by distance (Player::Update ends it beyond the visibility range only outside one), so a
// reference to a creature a wing away, a reset encounter's trigger, say, keeps the flag up.
Unit* EngagedWith(Player* member, CombatReference const* ref)
{
    if (ref->IsSuppressedFor(member))
        return nullptr;
    Unit* other = ref->GetOther(member);
    if (!other || !other->IsInWorld() || !other->IsAlive() || other->GetMap() != member->GetMap())
        return nullptr;
    if (Creature* creature = other->ToCreature(); creature && creature->IsInEvadeMode())
        return nullptr;
    return member->IsWithinDistInMap(other, kGroupRezEngagedYards) ? other : nullptr;
}

// Calls see(enemy) for each enemy the member is really fighting; stops when it returns true.
template <typename See>
bool AnyEngaged(Player* member, See see)
{
    CombatManager const& combat = member->GetCombatManager();
    for (auto const& entry : combat.GetPvECombatRefs())
        if (Unit* enemy = EngagedWith(member, entry.second); enemy && see(enemy))
            return true;
    for (auto const& entry : combat.GetPvPCombatRefs())
        if (Unit* enemy = EngagedWith(member, entry.second); enemy && see(enemy))
            return true;
    return false;
}

// Someone of his group fights near the body: a member within kGroupRezCombatYards of it engaged
// with an enemy, or any member engaged with an enemy that close to it.
bool FightNear(Player* body, std::vector<Player*> const& members)
{
    for (Player* member : members)
    {
        if (!member->IsAlive() || !member->IsInCombat() || member->GetMap() != body->GetMap())
            continue;
        bool const near_body = member->IsWithinDistInMap(body, kGroupRezCombatYards);
        if (AnyEngaged(member, [&](Unit* enemy) { return near_body || enemy->IsWithinDistInMap(body, kGroupRezCombatYards); }))
            return true;
    }
    return false;
}

// A rezzer still flagged in combat by nothing it is fighting cannot cast an out-of-combat rez or
// drink. With no fight near the body, its references beyond kGroupRezEngagedYards are ended, as
// the core itself does outside dungeons (CombatManager::EndCombatBeyondRange).
void ClearStaleCombat(Player* rezzer, std::uint64_t now)
{
    if (!rezzer->IsInCombat() || AnyEngaged(rezzer, [](Unit*) { return true; }))
        return;
    auto const before = rezzer->GetCombatManager().GetPvECombatRefs().size();
    rezzer->GetCombatManager().EndCombatBeyondRange(kGroupRezEngagedYards, false);
    auto const after = rezzer->GetCombatManager().GetPvECombatRefs().size();
    std::uint32_t const guid = rezzer->GetGUID().GetCounter();
    auto& logged = combatClearsLogged[guid];
    if (before != after && (!logged || now - logged >= kGroupRezBlockLogMs))
    {
        logged = now;
        LOG_INFO("module.leeroy", "group_rez_combat_cleared guid={} refs={} in_combat={}", guid, before - after,
            rezzer->IsInCombat() ? 1 : 0);
    }
}

// As playerbots' CheckMountStateAction::Dismount: the cancel goes through the bot's session, whose
// SMSG_DISMOUNT puts it down at its feet; flight flags the mount left behind are cleared after.
void Dismount(Player* bot)
{
    if (bot->isMoving())
        bot->StopMoving();
    if (InFlightForm(bot))
        bot->RemoveAurasByType(SPELL_AURA_MOD_SHAPESHIFT);
    if (bot->IsMounted())
    {
        WorldPacket empty;
        bot->GetSession()->HandleCancelMountAuraOpcode(empty);
    }
    if (!bot->HasIncreaseMountedFlightSpeedAura() && !bot->HasFlyAura()
        && bot->HasUnitMovementFlag(MOVEMENTFLAG_FLYING | MOVEMENTFLAG_DISABLE_GRAVITY))
    {
        bot->RemoveUnitMovementFlag(MOVEMENTFLAG_FLYING | MOVEMENTFLAG_DISABLE_GRAVITY | MOVEMENTFLAG_CAN_FLY);
        if (!bot->IsRooted())
            bot->SendMovementFlagUpdate();
    }
}

SpellCastResult StartRez(Player* caster, Player* body, std::uint32_t spell)
{
    if (caster->IsMounted() || InFlightForm(caster))
        Dismount(caster);
    if (caster->GetShapeshiftForm() != FORM_NONE) // Shadowform, Ghost Wolf, a druid's forms
        caster->RemoveAurasByType(SPELL_AURA_MOD_SHAPESHIFT);
    // Playerbots waits out a cast in progress, so nothing walks it away mid-rez.
    caster->GetMotionMaster()->Clear();
    caster->StopMoving();
    if (!caster->IsStandState())
        caster->SetStandState(UNIT_STAND_STATE_STAND);
    caster->SetSelection(body->GetGUID());
    caster->SetFacingToObject(body);
    return caster->CastSpell(body, spell, false);
}

// Walks the rezzer to the body along the navmesh; the policy casts once it is within range and in
// sight. Its AI is held a little past the next pass so playerbots' follow does not take it back
// first. False when there is no path, or the path ends out of the spell's range of the body: a
// point move would then go in a straight line through walls or off a ledge.
bool Approach(Player* caster, Player* body, float castRange)
{
    std::uint32_t const id = caster->GetGUID().GetCounter();
    std::uint32_t const target = body->GetGUID().GetCounter();
    MotionMaster* motion = caster->GetMotionMaster();
    auto const sent = walkingTo.find(id);
    if (sent == walkingTo.end() || sent->second != target || !caster->isMoving()
        || motion->GetCurrentMovementGeneratorType() != POINT_MOTION_TYPE)
    {
        PathGenerator path(caster);
        path.CalculatePath(body->GetPositionX(), body->GetPositionY(), body->GetPositionZ(), false);
        if (path.GetPathType() & (PATHFIND_NOPATH | PATHFIND_SHORTCUT))
            return false;
        G3D::Vector3 const& end = path.GetActualEndPosition();
        if (body->GetExactDist(end.x, end.y, end.z) > castRange)
            return false;
        if (!caster->IsStandState())
            caster->SetStandState(UNIT_STAND_STATE_STAND);
        motion->MovePoint(kApproachPointId, end.x, end.y, end.z, FORCED_MOVEMENT_NONE, 0.0f, 0.0f, true, false);
        walkingTo[id] = target;
    }
    if (PlayerbotAI* ai = GET_PLAYERBOT_AI(caster))
        ai->SetNextCheckDelay(kApproachHoldMs);
    return true;
}

// Playerbots' own drink action (its food cheat sits the bot down to regain mana); a mount or form
// that forbids drinking is dropped first.
bool Drink(Player* caster)
{
    PlayerbotAI* ai = GET_PLAYERBOT_AI(caster);
    if (!ai)
        return false;
    if (caster->IsMounted() || InFlightForm(caster))
        Dismount(caster);
    if (caster->GetShapeshiftForm() != FORM_NONE)
        caster->RemoveAurasByType(SPELL_AURA_MOD_SHAPESHIFT);
    return ai->DoSpecificAction("drink", Event("leeroy"), true);
}

bool TransientRefusal(SpellCastResult result)
{
    return result == SPELL_FAILED_NOT_READY || result == SPELL_FAILED_SPELL_IN_PROGRESS
        || result == SPELL_FAILED_MOVING;
}

// As playerbots' AutoReleaseSpiritAction, which releases a bot farther than 100 yards from him.
void Release(Player* bot)
{
    bot->DurabilityRepairAll(false, 1.0f, false);
    WorldPacket packet(CMSG_REPOP_REQUEST);
    packet << uint8(0);
    bot->GetSession()->HandleRepopRequestOpcode(packet);
}

void Forget()
{
    memories.clear();
    casters.clear();
    walkingTo.clear();
    blocksLogged.clear();
    approachesLogged.clear();
    drinksLogged.clear();
    refusalsLogged.clear();
    combatClearsLogged.clear();
}

struct Rezzer
{
    Player* player = nullptr;
    std::uint32_t spell = 0;
    SpellInfo const* info = nullptr;
};
}

void ReloadGroupRez()
{
    enabled = sConfigMgr->GetOption<bool>("Leeroy.GroupRez.Enable", true);
    auto seconds = sConfigMgr->GetOption<std::uint32_t>("Leeroy.GroupRez.ReleaseSeconds",
        kGroupRezDefaultReleaseSeconds);
    releaseMs = std::min<std::uint32_t>(seconds, 600) * 1000ull;
    untilPass = 0;
}

void TickGroupRez(std::uint32_t diffMs)
{
    if (!enabled || !GuildEnabled())
    {
        Forget();
        return;
    }
    if (diffMs < untilPass) { untilPass -= diffMs; return; }
    untilPass = 1000;

    std::uint32_t const human = HumanGuid();
    Player* owner = human ? FindOnline(human) : nullptr;
    Group* group = owner ? owner->GetGroup() : nullptr;
    if (!group || group->isBGGroup() || group->isBFGroup())
    {
        Forget();
        return;
    }
    std::vector<Player*> members;
    for (GroupReference* ref = group->GetFirstMember(); ref; ref = ref->next())
        if (Player* member = ref->GetSource(); member && FindOnline(member->GetGUID().GetCounter()) == member)
            members.push_back(member);

    std::map<std::uint32_t, Rezzer> rezzers;
    for (Player* member : members)
        if (member->IsAlive() && IsGroupBot(member))
            if (std::uint32_t const spell = KnownRez(member))
                if (SpellInfo const* info = sSpellMgr->GetSpellInfo(spell))
                    rezzers.emplace(member->GetGUID().GetCounter(), Rezzer{member, spell, info});

    // A rezzer walking in to one body is not taken for another.
    std::map<std::uint32_t, std::uint32_t> claimed;
    for (auto const& [body, memory] : memories)
        if (memory.approachCaster)
            claimed.emplace(*memory.approachCaster, body);

    auto const now = NowMs();
    std::set<std::uint32_t> lying, used;
    std::map<GroupRezBlock, std::uint32_t> blocked;
    std::set<std::string> fences;
    for (Player* body : members)
    {
        if (body->IsAlive() || body->HasPlayerFlag(PLAYER_FLAGS_GHOST) || body->InBattleground() || body->InArena())
            continue;
        std::uint32_t const guid = body->GetGUID().GetCounter();
        bool const isOwner = guid == human;
        if (!isOwner && !IsGroupBot(body))
            continue; // another real player rezzes or releases themselves
        lying.insert(guid);

        GroupRezBody state;
        state.bot = !isOwner;
        state.instance = body->GetMap()->Instanceable();
        state.requested = body->isResurrectRequested();
        state.selfRes = body->GetUInt32Value(PLAYER_SELF_RES_SPELL) != 0;
        std::string const fence = FenceCode(guid);
        state.fenced = !fence.empty();
        state.rezzing = BeingRezzed(body, members);
        bool const fight = FightNear(body, members);

        std::vector<GroupRezCandidate> candidates;
        std::map<std::uint32_t, Rezzer const*> byGuid;
        if (!fight && !state.fenced && !state.requested && !state.rezzing && !state.selfRes)
            for (auto const& [id, rezzer] : rezzers)
            {
                Player* member = rezzer.player;
                if (member->GetMap() != body->GetMap())
                    continue;
                GroupRezCandidate candidate;
                candidate.guid = id;
                candidate.distance = member->GetDistance(body);
                candidate.fenced = Fenced(id);
                if (candidate.distance <= kGroupRezReachYards && !candidate.fenced)
                    ClearStaleCombat(member, now);
                candidate.castRange = rezzer.info->GetMaxRange(true);
                candidate.los = candidate.distance <= candidate.castRange && member->IsWithinLOSInMap(body);
                candidate.mounted = member->IsMounted() || InFlightForm(member);
                candidate.grounded = !candidate.mounted || Grounded(member);
                candidate.combat = member->IsInCombat();
                auto const other = claimed.find(id);
                candidate.busy = member->IsNonMeleeSpellCast(false) || member->GetVehicle() || member->IsInFlight()
                    || member->IsBeingTeleported() || used.contains(id)
                    || (other != claimed.end() && other->second != guid);
                auto const power = Powers(rezzer.info->PowerType);
                if (power < MAX_POWERS)
                {
                    candidate.mana = member->GetPower(power);
                    candidate.manaCost = static_cast<std::uint32_t>(
                        std::max(0, rezzer.info->CalcPowerCost(member, rezzer.info->GetSchoolMask())));
                }
                candidates.push_back(candidate);
                byGuid.emplace(id, &rezzer);
            }

        auto decision = DecideGroupRez(state, fight, candidates, memories[guid], casters, now, releaseMs);
        if (decision.block != GroupRezBlock::None)
        {
            ++blocked[decision.block];
            if (decision.block == GroupRezBlock::Fenced)
                fences.insert(fence);
        }
        switch (decision.step)
        {
            case GroupRezStep::Release:
                LOG_INFO("module.leeroy", "group_rez_released guid={} quiet_ms={}", guid,
                    now - memories[guid].quietSince.value_or(now));
                Release(body);
                memories.erase(guid);
                break;
            case GroupRezStep::Approach:
            {
                Rezzer const& rezzer = *byGuid.at(decision.caster);
                bool const path = Approach(rezzer.player, body, rezzer.info->GetMaxRange(true));
                used.insert(decision.caster);
                if (path)
                    claimed[decision.caster] = guid;
                else
                    NoteGroupRezUnreachable(memories[guid], decision.caster, now);
                if (approachesLogged.emplace(guid, decision.caster).second)
                    LOG_INFO("module.leeroy", "group_rez_approach guid={} body={} yards={} path={}", decision.caster,
                        guid, static_cast<int>(rezzer.player->GetDistance(body)), path ? 1 : 0);
                break;
            }
            case GroupRezStep::Drink:
            {
                Player* caster = byGuid.at(decision.caster)->player;
                auto const& candidate = *std::find_if(candidates.begin(), candidates.end(),
                    [&](GroupRezCandidate const& c) { return c.guid == decision.caster; });
                bool const ok = Drink(caster);
                NoteGroupRezDrinkAsked(casters, decision.caster, now);
                used.insert(decision.caster);
                if (drinksLogged.emplace(guid, decision.caster).second)
                    LOG_INFO("module.leeroy", "group_rez_drink guid={} body={} mana={} cost={} ok={}", decision.caster,
                        guid, candidate.mana, candidate.manaCost, ok ? 1 : 0);
                break;
            }
            case GroupRezStep::Cast:
            {
                Rezzer const& rezzer = *byGuid.at(decision.caster);
                auto const result = StartRez(rezzer.player, body, rezzer.spell);
                used.insert(decision.caster);
                walkingTo.erase(decision.caster);
                if (result == SPELL_CAST_OK)
                    LOG_INFO("module.leeroy", "group_rez_cast guid={} body={} spell={}", decision.caster, guid,
                        rezzer.spell);
                else
                {
                    NoteGroupRezRefused(casters, decision.caster, now, TransientRefusal(result));
                    auto& logged = refusalsLogged[{decision.caster, static_cast<int>(result)}];
                    if (!logged || now - logged >= kGroupRezBlockLogMs)
                    {
                        logged = now;
                        LOG_INFO("module.leeroy", "group_rez_cast_refused guid={} body={} result={}", decision.caster,
                            guid, static_cast<int>(result));
                    }
                }
                break;
            }
            case GroupRezStep::None:
                break;
        }
    }

    // Counts and codes only, once per reason a minute, so a body that lies there says why.
    for (auto const& [block, dead] : blocked)
    {
        if (!GroupRezBlockLogDue(blocksLogged, block, now))
            continue;
        if (block == GroupRezBlock::Fenced)
        {
            std::string codes;
            for (auto const& code : fences)
                codes += (codes.empty() ? "" : ",") + code;
            LOG_INFO("module.leeroy", "group_rez_blocked reason={} dead={} casters={} fence={}",
                GroupRezBlockCode(block), dead, rezzers.size(), codes);
        }
        else
            LOG_INFO("module.leeroy", "group_rez_blocked reason={} dead={} casters={}", GroupRezBlockCode(block), dead,
                rezzers.size());
    }

    std::erase_if(memories, [&](auto const& item) { return !lying.contains(item.first); });
    std::erase_if(approachesLogged, [&](auto const& item) { return !lying.contains(item.first); });
    std::erase_if(drinksLogged, [&](auto const& item) { return !lying.contains(item.first); });
    // A walk-in given up (a fight near the body, another rezzer's cast, the body up) stops where it
    // is, out of combat, so the walker does not go on to a body it will not rez.
    std::erase_if(walkingTo, [&](auto const& item) {
        auto const memory = memories.find(item.second);
        if (memory != memories.end() && memory->second.approachCaster == item.first)
            return false;
        if (auto const walker = rezzers.find(item.first); walker != rezzers.end())
            if (Player* bot = walker->second.player; !bot->IsInCombat()
                && bot->GetMotionMaster()->GetCurrentMovementGeneratorType() == POINT_MOTION_TYPE)
            {
                bot->GetMotionMaster()->Clear();
                bot->StopMoving();
            }
        return true;
    });
    std::erase_if(refusalsLogged, [&](auto const& item) { return now - item.second >= kGroupRezBlockLogMs; });
    std::erase_if(combatClearsLogged, [&](auto const& item) { return now - item.second >= kGroupRezBlockLogMs; });
    PruneGroupRezCasters(casters, now);
}
}
