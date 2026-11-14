// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "HazardJob.h"
#include "GuildPorts.h"
#include "LeeroyHazardPolicy.h"
#include "Realm/RealmPorts.h"
#include "CellImpl.h"
#include "Config.h"
#include "Creature.h"
#include "DynamicObject.h"
#include "GameObject.h"
#include "GridNotifiers.h"
#include "Group.h"
#include "Log.h"
#include "Map.h"
#include "ObjectAccessor.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "PlayerbotAIConfig.h"
#include "Playerbots.h"
#include "SpellAuraEffects.h"
#include "SpellInfo.h"
#include "SpellMgr.h"
#include "WorldSession.h"
#include <algorithm>
#include <atomic>
#include <cmath>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <unordered_map>
#include <utility>
#include <vector>

// The follow-spot and exit hook comes from the local playerbots patch; without it only the non-combat avoid aoe
// part works.
#if __has_include("PlayerbotHazardFilter.h")
#include "PlayerbotHazardFilter.h"
#define LEEROY_HAS_HAZARD_FILTER 1
#else
#define LEEROY_HAS_HAZARD_FILTER 0
#endif

namespace Leeroy::Guild
{
namespace
{
using MapKey = std::pair<std::uint32_t, std::uint32_t>; // map id, instance id

constexpr std::uint32_t kPassMs = 500;
constexpr std::uint32_t kPassesPerSummary = 120;  // a minute
constexpr float kScanYards = 70.0f;              // zones that can reach a spot moved the most
constexpr float kAnchorYards = 15.0f;            // a member this far from every scan centre gets its own

std::atomic<bool> enabled{false};
std::uint32_t untilPass = 0;
std::uint32_t passes = 0;
std::uint64_t version = 0;

// World thread: the bodies whose non-combat avoid aoe this job added, by the life it was added to.
struct Owned
{
    Player* player = nullptr;
    PlayerbotAI* ai = nullptr;
    WorldSession* session = nullptr;
    std::uint32_t readds = 0;
};
std::map<std::uint32_t, Owned> owned;

// Written whole by the world thread each pass, read by map threads.
struct Snapshot
{
    std::uint64_t version = 0;
    std::unordered_map<std::uint32_t, MapKey> aware;
    std::map<MapKey, std::vector<HazardZone>> zones;
    std::size_t zoneCount = 0;
};
std::mutex snapshotLock;
std::shared_ptr<Snapshot const> snapshot;

// Map threads: each member's last answer, reused while the question barely moved.
struct Memo
{
    std::uint64_t version = 0;
    HazardPoint asked;
    bool moved = false;
    HazardPoint answer;
};
std::mutex memoLock;
std::unordered_map<std::uint32_t, Memo> spotMemo;
std::unordered_map<std::uint32_t, Memo> exitMemo;
std::set<std::uint32_t> spotMembers; // members whose follow spot moved, for the minute's summary
std::set<std::uint32_t> exitMembers;

MapKey KeyOf(Player* player) { return {player->GetMapId(), player->GetInstanceId()}; }

HazardPoint PointOf(WorldObject const* object)
{
    return {object->GetPositionX(), object->GetPositionY(), object->GetPositionZ()};
}

bool SameLife(Owned const& life, Player* bot, PlayerbotAI* ai)
{
    return life.player == bot && life.ai == ai && life.session == bot->GetSession();
}

HazardMemberFacts ReadFacts(Player* bot, PlayerbotAI* ai, Player* owner, Group* group, bool on)
{
    std::uint32_t const guid = bot->GetGUID().GetCounter();
    HazardMemberFacts facts;
    facts.enabled = on;
    facts.cast = guid != HumanGuid() && IsCast(guid);
    facts.bot = ai && !IsSelfBot(bot);
    facts.ownerGroup = group && bot->GetGroup() == group;
    facts.ownerMaster = ai && owner && ai->GetMaster() == owner;
    facts.fenced = Realm::RealmOwns(guid) || !Realm::NativeOwnershipReason(guid).empty();
    return facts;
}

// Adds or removes this job's non-combat avoid aoe on one body; true when its spots and exits use the zones.
bool Reconcile(Player* bot, Player* owner, Group* group, bool on)
{
    std::uint32_t const guid = bot->GetGUID().GetCounter();
    PlayerbotAI* ai = GET_PLAYERBOT_AI(bot);
    HazardMemberFacts const facts = ReadFacts(bot, ai, owner, group, on);
    auto found = owned.find(guid);
    HazardHold hold;
    hold.journaled = found != owned.end();
    hold.present = ai && ai->HasStrategy("avoid aoe", BOT_STATE_NON_COMBAT);
    hold.sameLife = !hold.journaled || SameLife(found->second, bot, ai);
    switch (DecideHazardStrategy(facts, hold))
    {
        case HazardStep::Add:
            ai->ChangeStrategy("+avoid aoe", BOT_STATE_NON_COMBAT);
            if (!ai->HasStrategy("avoid aoe", BOT_STATE_NON_COMBAT))
                break;
            if (found == owned.end())
            {
                owned.emplace(guid, Owned{bot, ai, bot->GetSession(), 0});
                LOG_INFO("module.leeroy", "hazard_avoid_on guid={}", guid);
            }
            else if (++found->second.readds % 50 == 1) // a reset (a leader change, raid habits) dropped it
                LOG_INFO("module.leeroy", "hazard_avoid_readded guid={} readds={}", guid, found->second.readds);
            break;
        case HazardStep::Remove:
            ai->ChangeStrategy("-avoid aoe", BOT_STATE_NON_COMBAT);
            owned.erase(found);
            LOG_INFO("module.leeroy", "hazard_avoid_off guid={} reason={}", guid, HazardOffReason(facts));
            break;
        case HazardStep::Forget:
            owned.erase(found);
            break;
        case HazardStep::None:
            break;
    }
    return HazardAware(facts);
}

bool DamagingTrigger(std::uint32_t spellId)
{
    SpellInfo const* info = sSpellMgr->GetSpellInfo(spellId);
    if (!info)
        return false;
    for (std::uint8_t i = 0; i < MAX_SPELL_EFFECTS; ++i)
        if (info->Effects[i].Effect == SPELL_EFFECT_SCHOOL_DAMAGE)
            return true;
    return false;
}

// A persistent area that hurts: damage over time, or a periodic trigger of a damage spell. Dummy areas are
// mostly markers; stock avoid aoe still flees one once it applies.
bool DamagingArea(SpellInfo const* info)
{
    for (std::uint8_t i = 0; i < MAX_SPELL_EFFECTS; ++i)
    {
        auto const& effect = info->Effects[i];
        if (effect.Effect != SPELL_EFFECT_PERSISTENT_AREA_AURA)
            continue;
        switch (effect.ApplyAuraName)
        {
            case SPELL_AURA_PERIODIC_DAMAGE:
            case SPELL_AURA_PERIODIC_DAMAGE_PERCENT:
            case SPELL_AURA_PERIODIC_LEECH:
                return true;
            case SPELL_AURA_PERIODIC_TRIGGER_SPELL:
            case SPELL_AURA_PERIODIC_TRIGGER_SPELL_WITH_VALUE:
                if (DamagingTrigger(effect.TriggerSpell))
                    return true;
                break;
            default:
                break;
        }
    }
    return false;
}

// As playerbots' "nearest trap with damage".
bool PeriodicDamageTrap(SpellInfo const* info)
{
    for (std::uint8_t i = 0; i < MAX_SPELL_EFFECTS; ++i)
        if (info->Effects[i].Effect == SPELL_EFFECT_APPLY_AURA
            && info->Effects[i].ApplyAuraName == SPELL_AURA_PERIODIC_DAMAGE)
            return true;
    return false;
}

// The three kinds stock avoid aoe knows, measured as it measures them, around one scan centre.
struct HazardScanner
{
    Player* reference;
    float x;
    float y;
    float stockCap;
    std::set<uint32> const& whitelist;
    std::set<ObjectGuid>& seen;
    std::vector<HazardZone>& zones;

    bool Near(WorldObject const* object) const
    {
        return object->IsInWorld() && reference->InSamePhase(object) && object->GetExactDist2d(x, y) <= kScanYards;
    }

    void Note(WorldObject const* object, float radius)
    {
        if (radius > 0.0f && seen.insert(object->GetGUID()).second)
            zones.push_back({object->GetPositionX(), object->GetPositionY(), object->GetPositionZ(), radius});
    }

    void Visit(DynamicObjectMapType& m)
    {
        for (auto itr = m.begin(); itr != m.end(); ++itr)
        {
            DynamicObject* area = itr->GetSource();
            if (!area || !Near(area))
                continue;
            SpellInfo const* info = sSpellMgr->GetSpellInfo(area->GetSpellId());
            Unit* caster = area->GetCaster();
            if (!info || info->IsPositive() || !caster || caster->IsFriendlyTo(reference) || !DamagingArea(info))
                continue;
            Note(area, HazardAvoidRadius(info->Id, area->GetRadius(), 0.0f, stockCap, whitelist.contains(info->Id)));
        }
    }

    void Visit(GameObjectMapType& m)
    {
        for (auto itr = m.begin(); itr != m.end(); ++itr)
        {
            GameObject* trap = itr->GetSource();
            if (!trap || !Near(trap) || !trap->isSpawned() || trap->GetGoType() != GAMEOBJECT_TYPE_TRAP)
                continue;
            if (Unit* owner = trap->GetOwner(); owner && owner->IsFriendlyTo(reference))
                continue;
            GameObjectTemplate const* info = trap->GetGOInfo();
            if (!info || info->trap.type != 0 || !info->trap.spellId || info->trap.stealthed
                || info->trap.invisible) // a player sees neither
                continue;
            SpellInfo const* spell = sSpellMgr->GetSpellInfo(info->trap.spellId);
            if (!spell || spell->IsPositive() || !PeriodicDamageTrap(spell))
                continue;
            float const radius = static_cast<float>(info->trap.diameter) / 2.0f + trap->GetCombatReach();
            Note(trap, HazardAvoidRadius(spell->Id, radius, 0.0f, stockCap, whitelist.contains(spell->Id)));
        }
    }

    void Visit(CreatureMapType& m)
    {
        for (auto itr = m.begin(); itr != m.end(); ++itr)
        {
            Creature* unit = itr->GetSource();
            if (!unit || !Near(unit) || !unit->IsAlive() || unit->IsCritter()
                || !unit->HasUnitFlag(UNIT_FLAG_NOT_SELECTABLE) || reference->IsFriendlyTo(unit))
                continue;
            float radius = 0.0f;
            for (AuraType type : {SPELL_AURA_PERIODIC_TRIGGER_SPELL, SPELL_AURA_PERIODIC_TRIGGER_SPELL_WITH_VALUE})
                for (AuraEffect const* effect : unit->GetAuraEffectsByType(type))
                {
                    SpellInfo const* aura = effect->GetSpellInfo();
                    SpellInfo const* trigger =
                        aura ? sSpellMgr->GetSpellInfo(aura->Effects[effect->GetEffIndex()].TriggerSpell) : nullptr;
                    if (!trigger)
                        continue;
                    for (std::uint8_t j = 0; j < MAX_SPELL_EFFECTS; ++j)
                        if (trigger->Effects[j].Effect == SPELL_EFFECT_SCHOOL_DAMAGE)
                        {
                            radius = std::max(radius, HazardAvoidRadius(trigger->Id, trigger->Effects[j].CalcRadius(),
                                unit->GetCombatReach(), stockCap, whitelist.contains(trigger->Id)));
                            break;
                        }
                }
            Note(unit, radius);
        }
    }

    template <class NotInterested>
    void Visit(GridRefMgr<NotInterested>&)
    {
    }
};

// One scan per map around him and around any member far from him, shared by every aware member there.
void Publish(Player* owner, std::vector<Player*> const& aware, bool on)
{
    auto next = std::make_shared<Snapshot>();
    next->version = ++version;
    if (on && !aware.empty())
    {
        float const stockCap = sPlayerbotAIConfig.MaxAoeAvoidRadius;
        auto const& whitelist = sPlayerbotAIConfig.AoeAvoidSpellWhitelist;
        std::map<MapKey, std::vector<Player*>> byMap;
        for (Player* bot : aware)
        {
            next->aware[bot->GetGUID().GetCounter()] = KeyOf(bot);
            byMap[KeyOf(bot)].push_back(bot);
        }
        for (auto const& [key, bots] : byMap)
        {
            Map* map = bots.front()->GetMap();
            if (!map)
                continue;
            bool const ownerHere = owner && owner->IsInMap(bots.front());
            Player* reference = ownerHere ? owner : bots.front();
            std::vector<HazardPoint> anchors;
            if (ownerHere)
                anchors.push_back(PointOf(owner));
            for (Player* bot : bots)
            {
                HazardPoint const at = PointOf(bot);
                if (std::none_of(anchors.begin(), anchors.end(), [&](HazardPoint const& anchor)
                        { return std::hypot(anchor.x - at.x, anchor.y - at.y) <= kAnchorYards; }))
                    anchors.push_back(at);
            }
            std::set<ObjectGuid> seen;
            auto& zones = next->zones[key];
            for (HazardPoint const& anchor : anchors)
            {
                HazardScanner scanner{reference, anchor.x, anchor.y, stockCap, whitelist, seen, zones};
                Cell::VisitObjects(anchor.x, anchor.y, map, scanner, kScanYards);
            }
            next->zoneCount += zones.size();
        }
    }
    {
        std::lock_guard<std::mutex> lock(memoLock);
        std::erase_if(spotMemo, [&](auto const& item) { return !next->aware.contains(item.first); });
        std::erase_if(exitMemo, [&](auto const& item) { return !next->aware.contains(item.first); });
    }
    std::lock_guard<std::mutex> lock(snapshotLock);
    snapshot = std::move(next);
}

void Summarise()
{
    std::size_t spots = 0;
    std::size_t exits = 0;
    {
        std::lock_guard<std::mutex> lock(memoLock);
        spots = spotMembers.size();
        exits = exitMembers.size();
        spotMembers.clear();
        exitMembers.clear();
    }
    std::shared_ptr<Snapshot const> current;
    {
        std::lock_guard<std::mutex> lock(snapshotLock);
        current = snapshot;
    }
    if (spots || exits)
        LOG_INFO("module.leeroy", "hazards_minute aware={} zones={} spot_members={} exit_members={}",
            current ? current->aware.size() : std::size_t(0), current ? current->zoneCount : std::size_t(0), spots,
            exits);
}

#if LEEROY_HAS_HAZARD_FILTER
constexpr std::size_t kCandidates = 6; // safe spots tried against the navmesh per answer
constexpr float kMemoYards = 0.5f;

bool Close(HazardPoint a, HazardPoint b)
{
    return std::hypot(a.x - b.x, a.y - b.y) < kMemoYards && std::fabs(a.z - b.z) < 2.0f;
}

// Map thread. The zones on the bot's map when the last pass found it aware, else null; `hold` keeps them alive.
std::vector<HazardZone> const* ZonesFor(Player* bot, PlayerbotAI*& ai, std::shared_ptr<Snapshot const>& hold)
{
    if (!enabled.load(std::memory_order_relaxed))
        return nullptr;
    ai = GET_PLAYERBOT_AI(bot);
    Player* master = ai ? ai->GetMaster() : nullptr;
    if (!master || master->GetGUID().GetCounter() != HumanGuid())
        return nullptr; // every other bot, at once
    {
        std::lock_guard<std::mutex> lock(snapshotLock);
        hold = snapshot;
    }
    if (!hold)
        return nullptr;
    auto const member = hold->aware.find(bot->GetGUID().GetCounter());
    if (member == hold->aware.end() || member->second != KeyOf(bot))
        return nullptr;
    auto const zones = hold->zones.find(member->second);
    return zones == hold->zones.end() || zones->second.empty() ? nullptr : &zones->second;
}

// The first candidate the navmesh reaches from `from` and that is still clear where it lands.
bool FirstReachable(Player* bot, HazardPoint from, std::vector<HazardPoint> const& candidates,
    std::vector<HazardZone> const& zones, float reach, HazardPoint& out)
{
    for (HazardPoint const& candidate : candidates)
    {
        float x = candidate.x;
        float y = candidate.y;
        float z = candidate.z;
        if (!bot->GetMap()->CheckCollisionAndGetValidCoords(bot, from.x, from.y, from.z, x, y, z))
            continue;
        HazardPoint const landed{x, y, z};
        if (!HazardClear(landed, zones, reach)
            || std::hypot(landed.x - from.x, landed.y - from.y) > kHazardMaxShiftYards)
            continue;
        out = landed;
        return true;
    }
    return false;
}

class GroupHazardFilter final : public PlayerbotHazardFilter
{
public:
    bool SafeFollowSpot(Player* bot, float& x, float& y, float& z) override
    {
        PlayerbotAI* ai = nullptr;
        std::shared_ptr<Snapshot const> hold;
        std::vector<HazardZone> const* zones = ZonesFor(bot, ai, hold);
        if (!zones)
            return false;
        std::uint32_t const guid = bot->GetGUID().GetCounter();
        HazardPoint const spot{x, y, z};
        Memo memo{hold->version, spot, false, spot};
        bool known = false;
        {
            std::lock_guard<std::mutex> lock(memoLock);
            auto const found = spotMemo.find(guid);
            if (found != spotMemo.end() && found->second.version == hold->version && Close(found->second.asked, spot))
            {
                memo = found->second;
                known = true;
            }
        }
        if (!known)
        {
            float const reach = bot->GetCombatReach();
            auto const candidates = HazardSafeSpots(spot, PointOf(bot), *zones, reach, kCandidates);
            memo.moved = !candidates.empty() && FirstReachable(bot, spot, candidates, *zones, reach, memo.answer);
            std::lock_guard<std::mutex> lock(memoLock);
            spotMemo[guid] = memo;
            if (memo.moved)
                spotMembers.insert(guid);
        }
        if (!memo.moved)
            return false;
        x = memo.answer.x;
        y = memo.answer.y;
        z = memo.answer.z;
        return true;
    }

    bool HazardExit(Player* bot, float& x, float& y, float& z) override
    {
        PlayerbotAI* ai = nullptr;
        std::shared_ptr<Snapshot const> hold;
        std::vector<HazardZone> const* zones = ZonesFor(bot, ai, hold);
        if (!zones)
            return false;
        std::uint32_t const guid = bot->GetGUID().GetCounter();
        float const reach = bot->GetCombatReach();
        HazardPoint const at = PointOf(bot);
        if (!HazardAt(at, *zones, reach))
        {
            std::lock_guard<std::mutex> lock(memoLock);
            exitMemo.erase(guid);
            return false;
        }
        Memo memo{hold->version, at, false, at};
        bool known = false;
        {
            std::lock_guard<std::mutex> lock(memoLock);
            auto const found = exitMemo.find(guid);
            if (found != exitMemo.end())
            {
                Memo const& last = found->second;
                // Keep walking to the exit already chosen while it stays clear: no new path every tick.
                if (last.moved && HazardClear(last.answer, *zones, reach)
                    && std::hypot(last.answer.x - at.x, last.answer.y - at.y) <= kHazardMaxShiftYards)
                {
                    memo = last;
                    known = true;
                }
                else if (!last.moved && last.version == hold->version && Close(last.asked, at))
                    known = true; // nothing reachable a moment ago
            }
        }
        if (!known)
        {
            HazardPoint toward = at;
            if (Player* master = ai->GetMaster(); master && master->IsInMap(bot))
                toward = PointOf(master);
            auto const candidates = HazardSafeSpots(at, toward, *zones, reach, kCandidates);
            memo.moved = !candidates.empty() && FirstReachable(bot, at, candidates, *zones, reach, memo.answer);
            std::lock_guard<std::mutex> lock(memoLock);
            exitMemo[guid] = memo;
            if (memo.moved)
                exitMembers.insert(guid);
        }
        if (!memo.moved)
            return false;
        x = memo.answer.x;
        y = memo.answer.y;
        z = memo.answer.z;
        return true;
    }
};
#endif
}

void ReloadHazards()
{
    enabled.store(sConfigMgr->GetOption<bool>("Leeroy.Hazards.Enable", false), std::memory_order_relaxed);
    untilPass = 0;
}

void InstallHazardFilter()
{
#if LEEROY_HAS_HAZARD_FILTER
    static GroupHazardFilter filter;
    PlayerbotHazardFilter::Set(&filter);
    LOG_INFO("module.leeroy", "mod-leeroy: hazard filter installed");
#else
    LOG_INFO("module.leeroy", "mod-leeroy: hazard filter unavailable: playerbots lacks PlayerbotHazardFilter");
#endif
}

void TickHazards(std::uint32_t diffMs)
{
    if (diffMs < untilPass)
    {
        untilPass -= diffMs;
        return;
    }
    untilPass = kPassMs;

    bool const on = enabled.load(std::memory_order_relaxed) && GuildEnabled();
    std::uint32_t const human = HumanGuid();
    Player* owner = on && human ? FindOnline(human) : nullptr;
    Group* group = owner ? owner->GetGroup() : nullptr;
    if (group && (group->isBGGroup() || group->isBFGroup()))
        group = nullptr;

    std::set<std::uint32_t> guids;
    for (auto const& item : owned)
        guids.insert(item.first);
    if (group)
        for (GroupReference* ref = group->GetFirstMember(); ref; ref = ref->next())
            if (Player* member = ref->GetSource())
                guids.insert(member->GetGUID().GetCounter());
    guids.erase(human);

    std::vector<Player*> aware;
    for (std::uint32_t guid : guids)
    {
        if (!ObjectAccessor::FindConnectedPlayer(ObjectGuid::Create<HighGuid::Player>(guid)))
        {
            owned.erase(guid); // logged out: its AI and strategies went with it
            continue;
        }
        Player* bot = FindOnline(guid);
        if (!bot || bot->IsBeingTeleported())
            continue; // between maps: decided once it lands
        if (Reconcile(bot, owner, group, on))
            aware.push_back(bot);
    }
    Publish(owner, aware, on);

    if (++passes >= kPassesPerSummary)
    {
        passes = 0;
        Summarise();
    }
}

void ApplyHazards(Player* bot)
{
    bool const on = enabled.load(std::memory_order_relaxed) && GuildEnabled();
    Player* owner = on ? FindOnline(HumanGuid()) : nullptr;
    Group* group = owner ? owner->GetGroup() : nullptr;
    if (!bot || !group || group->isBGGroup() || group->isBFGroup())
        return;
    Reconcile(bot, owner, group, on);
}
}
