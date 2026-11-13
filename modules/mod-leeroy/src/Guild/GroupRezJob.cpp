// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "GroupRezJob.h"
#include "GuildPorts.h"
#include "LeeroyGroupRezPolicy.h"
#include "Realm/RealmPorts.h"
#include "Config.h"
#include "Group.h"
#include "Log.h"
#include "Map.h"
#include "MotionMaster.h"
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
#include <tuple>
#include <utility>
#include <vector>

namespace Leeroy::Guild
{
namespace
{
bool enabled = true;
std::uint64_t releaseMs = kGroupRezDefaultReleaseSeconds * 1000ull;
std::uint32_t untilPass = 0;
// World thread only. Keyed by the dead member; dropped once they stand up or release.
std::map<std::uint32_t, GroupRezMemory> memories;
std::set<std::pair<std::uint32_t, std::uint32_t>> dismountsLogged;
std::set<std::tuple<std::uint32_t, std::uint32_t, int>> refusalsLogged;

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

bool Fenced(std::uint32_t guid)
{
    return Realm::RealmOwns(guid) || !Realm::NativeOwnershipReason(guid).empty();
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
    dismountsLogged.clear();
    refusalsLogged.clear();
}
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

    auto const now = NowMs();
    std::set<std::uint32_t> lying, busy;
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
        state.fenced = Fenced(guid);
        state.rezzing = BeingRezzed(body, members);
        bool combat = false;
        for (Player* member : members)
            if (member->GetMap() == body->GetMap() && member->IsAlive() && member->IsInCombat()
                && member->IsWithinDistInMap(body, kGroupRezCombatYards))
                combat = true;

        std::vector<GroupRezCandidate> candidates;
        std::map<std::uint32_t, std::pair<Player*, std::uint32_t>> casters;
        if (!combat && !state.fenced && !state.requested && !state.rezzing)
            for (Player* member : members)
            {
                std::uint32_t const id = member->GetGUID().GetCounter();
                if (member == body || member->GetMap() != body->GetMap() || !member->IsAlive()
                    || member->IsInCombat() || busy.contains(id) || !IsGroupBot(member))
                    continue;
                if (member->IsNonMeleeSpellCast(false) || member->GetVehicle() || member->IsInFlight()
                    || member->IsBeingTeleported() || Fenced(id))
                    continue;
                std::uint32_t const spell = KnownRez(member);
                SpellInfo const* info = spell ? sSpellMgr->GetSpellInfo(spell) : nullptr;
                if (!info)
                    continue;
                GroupRezCandidate candidate;
                candidate.guid = id;
                candidate.distance = member->GetDistance(body);
                candidate.castRange = info->GetMaxRange(true);
                candidate.los = candidate.distance <= kGroupRezSearchYards && member->IsWithinLOSInMap(body);
                candidate.mounted = member->IsMounted() || InFlightForm(member);
                candidate.grounded = !candidate.mounted || Grounded(member);
                candidates.push_back(candidate);
                casters.emplace(id, std::make_pair(member, spell));
            }

        auto decision = DecideGroupRez(state, combat, candidates, memories[guid], now, releaseMs);
        switch (decision.step)
        {
            case GroupRezStep::Release:
                LOG_INFO("module.leeroy", "group_rez_released guid={} quiet_ms={}", guid,
                    now - memories[guid].quietSince.value_or(now));
                Release(body);
                memories.erase(guid);
                break;
            case GroupRezStep::Dismount:
            {
                Player* caster = casters.at(decision.caster).first;
                Dismount(caster);
                busy.insert(decision.caster);
                if (dismountsLogged.emplace(guid, decision.caster).second)
                    LOG_INFO("module.leeroy", "group_rez_dismount guid={} body={}", decision.caster, guid);
                break;
            }
            case GroupRezStep::Cast:
            {
                auto [caster, spell] = casters.at(decision.caster);
                auto const result = StartRez(caster, body, spell);
                busy.insert(decision.caster);
                if (result == SPELL_CAST_OK)
                    LOG_INFO("module.leeroy", "group_rez_cast guid={} body={} spell={}", decision.caster, guid, spell);
                else
                {
                    NoteGroupRezRefused(memories[guid], decision.caster, now);
                    if (refusalsLogged.emplace(guid, decision.caster, static_cast<int>(result)).second)
                        LOG_INFO("module.leeroy", "group_rez_cast_refused guid={} body={} result={}", decision.caster,
                            guid, static_cast<int>(result));
                }
                break;
            }
            case GroupRezStep::None:
                break;
        }
    }

    std::erase_if(memories, [&](auto const& item) { return !lying.contains(item.first); });
    std::erase_if(dismountsLogged, [&](auto const& item) { return !lying.contains(item.first); });
    std::erase_if(refusalsLogged, [&](auto const& item) { return !lying.contains(std::get<0>(item)); });
}
}
