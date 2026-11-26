// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "HazardJob.h"
#include "GuildPorts.h"
#include "LeeroyHazardPolicy.h"
#include "Realm/RealmPorts.h"
#include "CellImpl.h"
#include "Config.h"
#include "Creature.h"
#include "DBCStores.h"
#include "DynamicObject.h"
#include "Formations.h"
#include "GameObject.h"
#include "GridNotifiers.h"
#include "Group.h"
#include "Log.h"
#include "Map.h"
#include "MoveSpline.h"
#include "ObjectAccessor.h"
#include "PathGenerator.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "PlayerbotAIConfig.h"
#include "Playerbots.h"
#include "ScriptMgr.h"
#include "SpellAuraEffects.h"
#include "SpellInfo.h"
#include "SpellMgr.h"
#include "WorldSession.h"
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <set>
#include <unordered_map>
#include <unordered_set>
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
constexpr float kScanYards = 70.0f;              // zones that can reach a spot moved the most
constexpr float kAnchorYards = 15.0f;            // a member this far from every scan centre gets its own

std::atomic<bool> enabled{false};
#if defined(DOJO_RAID_ACCEPTANCE) && DOJO_RAID_ACCEPTANCE
std::uint32_t dojoOwner = 0;
bool dojoWasEnabled = false;
#endif

bool ContextEnabled()
{
#if defined(DOJO_RAID_ACCEPTANCE) && DOJO_RAID_ACCEPTANCE
    if (dojoOwner) return true;
#endif
    return enabled.load(std::memory_order_relaxed) && GuildEnabled();
}

std::uint32_t ContextOwner()
{
#if defined(DOJO_RAID_ACCEPTANCE) && DOJO_RAID_ACCEPTANCE
    if (dojoOwner) return dojoOwner;
#endif
    return HumanGuid();
}

bool ContextMember(Player* bot, Player* owner)
{
#if defined(DOJO_RAID_ACCEPTANCE) && DOJO_RAID_ACCEPTANCE
    if (dojoOwner && owner && bot->GetSession() && owner->GetSession())
        return bot != owner && bot->GetSession()->GetAccountId() == owner->GetSession()->GetAccountId();
#endif
    (void)owner;
    return bot->GetGUID().GetCounter() != HumanGuid() && IsCast(bot->GetGUID().GetCounter());
}
// The master whose bots the last pass made aware (0: none), so map threads turn every other bot away at once.
std::atomic<std::uint32_t> awareMaster{0};
std::uint32_t untilPass = 0;
std::uint64_t summaryMs = 0;
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
    std::unordered_set<std::uint32_t> tanks; // aware members playerbots reads as tanks: they hold an add in its fire
    std::map<MapKey, std::vector<HazardZone>> zones;
    std::size_t zoneCount = 0;
};
std::mutex snapshotLock;
std::shared_ptr<Snapshot const> snapshot;

struct PoisonWindow
{
    std::uint64_t hitEvents = 0;
    std::uint64_t botSamples = 0;
    std::uint64_t hazardSamples = 0;
};
std::mutex poisonLock;
std::map<MapKey, PoisonWindow> poisonWindows;
std::uint64_t poisonOverflow = 0;
constexpr std::size_t kPoisonWindows = 64;

PoisonWindow* PoisonCounter(MapKey key) // poisonLock held
{
    auto const found = poisonWindows.find(key);
    if (found != poisonWindows.end()) return &found->second;
    if (poisonWindows.size() >= kPoisonWindows)
    {
        ++poisonOverflow;
        return nullptr;
    }
    return &poisonWindows[key];
}

class HazardDamage final : public UnitScript
{
public:
    HazardDamage() : UnitScript("LeeroyHazardDamage", true, {UNITHOOK_ON_DAMAGE}) { }

    void OnDamage(Unit* attacker, Unit* victim, uint32& damage) override
    {
        if (!damage || !attacker || !attacker->IsCreature() || attacker->GetEntry() != 16027
            || !victim || !victim->IsPlayer() || victim->GetMapId() != 533
            || victim->ToPlayer()->GetCommandStatus(CHEAT_GOD))
            return;
        std::shared_ptr<Snapshot const> current;
        {
            std::lock_guard<std::mutex> lock(snapshotLock);
            current = snapshot;
        }
        if (!current) return;
        auto const member = current->aware.find(victim->GetGUID().GetCounter());
        MapKey const key{victim->GetMapId(), victim->GetInstanceId()};
        if (member == current->aware.end() || member->second != key) return;
        std::lock_guard<std::mutex> lock(poisonLock);
        if (auto* counter = PoisonCounter(key)) ++counter->hitEvents;
    }
};

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
std::unordered_map<std::uint32_t, Memo> detourMemo; // asked: the follow goal; answer: the waypoint
std::unordered_map<std::uint32_t, HazardExitGuard> exitGuards;
std::unordered_map<std::uint32_t, HazardDetourTrack> detourTracks;
std::set<std::uint32_t> spotMembers; // members whose follow spot moved, for the minute's summary
std::set<std::uint32_t> exitMembers;
std::set<std::uint32_t> detourMembers;

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
    facts.cast = ContextMember(bot, owner);
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

// The first (10-man or normal) difficulty version of a spell; 0 when it has no difficulty versions.
std::uint32_t FirstVersion(std::uint32_t spellId)
{
    if (std::uint32_t const row = sSpellMgr->GetSpellDifficultyId(spellId))
        if (SpellDifficultyEntry const* entry = sSpellDifficultyStore.LookupEntry(row))
            for (int32 spell : entry->SpellID)
                if (spell > 0)
                    return static_cast<std::uint32_t>(spell);
    return 0;
}

HazardSpellIds Ids(std::uint32_t spellId)
{
    return {spellId, FirstVersion(spellId), 0};
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

    // Every phase: each zone keeps its object's phase mask, and a body meets only the zones of its own phases
    // (HazardApplies), so members split between Halion's two realms each see their own.
    bool Near(WorldObject const* object) const
    {
        return object->IsInWorld() && object->GetExactDist2d(x, y) <= kScanYards;
    }

    void Note(WorldObject const* object, float radius, bool held = false)
    {
        if (radius > 0.0f && seen.insert(object->GetGUID()).second)
            zones.push_back({object->GetPositionX(), object->GetPositionY(), object->GetPositionZ(), radius,
                object->GetGUID().GetRawValue(), object->GetPhaseMask(), held, false});
    }

    // An allow-listed npc that goes off when a player comes near: its trigger or its blast (this difficulty's
    // version) where it stands and, while it walks, a second ahead along its path. Nobody holds it.
    void NoteProximity(Creature const* unit, ProximityHazardRule const& rule)
    {
        float blast = 0.0f;
        if (SpellInfo const* info = rule.blast ? sSpellMgr->GetSpellInfo(rule.blast) : nullptr)
        {
            info = sSpellMgr->GetSpellForDifficultyFromSpell(info, unit);
            for (std::uint8_t i = 0; i < MAX_SPELL_EFFECTS; ++i)
                blast = std::max(blast, info->Effects[i].CalcRadius());
        }
        float const radius = HazardProximityRadius(rule, blast, unit->GetCombatReach());
        if (radius <= 0.0f || !seen.insert(unit->GetGUID()).second)
            return;
        std::optional<HazardPoint> next;
        float speed = 0.0f;
        if (Movement::MoveSpline const* spline = unit->movespline;
            spline && spline->Initialized() && !spline->Finalized())
        {
            G3D::Vector3 const point = spline->CurrentDestination();
            next = HazardPoint{point.x, point.y, point.z};
            speed = spline->Velocity();
        }
        zones.push_back(HazardLeadZone({unit->GetPositionX(), unit->GetPositionY(), unit->GetPositionZ(), radius,
            unit->GetGUID().GetRawValue(), unit->GetPhaseMask(), false, false}, next, speed));
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
            Note(area, HazardAvoidRadius(Ids(info->Id), area->GetRadius(), 0.0f, stockCap,
                whitelist.contains(info->Id)));
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
            Note(trap, HazardAvoidRadius(Ids(spell->Id), radius, 0.0f, stockCap, whitelist.contains(spell->Id)));
        }
    }

    void Visit(CreatureMapType& m)
    {
        for (auto itr = m.begin(); itr != m.end(); ++itr)
        {
            Creature* unit = itr->GetSource();
            if (!unit || !Near(unit) || !unit->IsAlive() || unit->IsCritter() || reference->IsFriendlyTo(unit))
                continue;
            if (auto const rule = ProximityHazardFor(unit->GetEntry()))
            {
                NoteProximity(unit, *rule);
                continue;
            }
            // An unselectable trigger npc, as stock; any other unit only for an allow-listed burning aura of an add
            // (HeldAuraHazard), which tanks stand in.
            bool const trigger = unit->HasUnitFlag(UNIT_FLAG_NOT_SELECTABLE);
            float radius = 0.0f;
            for (AuraType type : {SPELL_AURA_PERIODIC_TRIGGER_SPELL, SPELL_AURA_PERIODIC_TRIGGER_SPELL_WITH_VALUE})
                for (AuraEffect const* effect : unit->GetAuraEffectsByType(type))
                {
                    SpellInfo const* aura = effect->GetSpellInfo();
                    SpellInfo const* damage =
                        aura ? sSpellMgr->GetSpellInfo(aura->Effects[effect->GetEffIndex()].TriggerSpell) : nullptr;
                    if (!damage)
                        continue;
                    // A 25-man aura (54362) triggers its own spell (54363): its 10-man twin's trigger names it too.
                    SpellInfo const* firstAura = sSpellMgr->GetSpellInfo(FirstVersion(aura->Id));
                    HazardSpellIds ids = Ids(damage->Id);
                    ids[2] = firstAura ? firstAura->Effects[effect->GetEffIndex()].TriggerSpell : 0;
                    for (std::uint8_t j = 0; j < MAX_SPELL_EFFECTS; ++j)
                        if (damage->Effects[j].Effect == SPELL_EFFECT_SCHOOL_DAMAGE)
                        {
                            float const spellRadius = damage->Effects[j].CalcRadius();
                            float const reach = unit->GetCombatReach();
                            bool const whitelisted = whitelist.contains(damage->Id);
                            radius = std::max(radius, trigger
                                ? HazardTriggerZoneRadius(ids, spellRadius, reach, stockCap, whitelisted)
                                : HazardHeldAuraRadius(ids, spellRadius, reach, whitelisted));
                            break;
                        }
                }
            Note(unit, radius, !trigger);
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
    if (on && !aware.empty())
    {
        float const stockCap = sPlayerbotAIConfig.MaxAoeAvoidRadius;
        auto const& whitelist = sPlayerbotAIConfig.AoeAvoidSpellWhitelist;
        std::map<MapKey, std::vector<Player*>> byMap;
        for (Player* bot : aware)
        {
            next->aware[bot->GetGUID().GetCounter()] = KeyOf(bot);
            if (PlayerbotAI::IsTank(bot, true))
                next->tanks.insert(bot->GetGUID().GetCounter());
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
        auto const gone = [&](auto const& item) { return !next->aware.contains(item.first); };
        std::erase_if(spotMemo, gone);
        std::erase_if(exitMemo, gone);
        std::erase_if(detourMemo, gone);
        std::erase_if(exitGuards, gone);
        std::erase_if(detourTracks, gone);
    }
    // Record exposure as well as hits, so zero hits without a nearby hazard is
    // not mistaken for a successful crossing. Only IDs/counts reach the log.
    for (auto const& [key, zones] : next->zones)
    {
        if (key.first != 533) continue;
        auto const poisons = std::count_if(zones.begin(), zones.end(), [](HazardZone const& zone)
        {
            ObjectGuid const guid(zone.id);
            return guid.IsCreature() && guid.GetEntry() == 16027;
        });
        if (!poisons) continue;
        auto const bots = std::count_if(next->aware.begin(), next->aware.end(),
            [&](auto const& member) { return member.second == key; });
        if (!bots) continue;
        std::lock_guard<std::mutex> lock(poisonLock);
        if (auto* counter = PoisonCounter(key))
        {
            counter->botSamples += bots;
            counter->hazardSamples += poisons;
        }
    }
    std::shared_ptr<Snapshot const> previous;
    {
        std::lock_guard<std::mutex> lock(snapshotLock);
        previous = snapshot;
    }
    // A new version only when the zones changed, so the answers memoised against them hold between passes.
    // Publish has one world-thread writer; compare the immutable vectors without
    // holding the lock used by every map-thread reader.
    next->version = previous && previous->zones == next->zones ? previous->version : ++version;
    awareMaster.store(owner && !next->aware.empty() ? owner->GetGUID().GetCounter() : 0, std::memory_order_relaxed);
    std::lock_guard<std::mutex> lock(snapshotLock);
    snapshot = std::move(next);
}

void Summarise(std::uint64_t windowMs)
{
    std::map<MapKey, PoisonWindow> poison;
    std::uint64_t overflow = 0;
    {
        std::lock_guard<std::mutex> lock(poisonLock);
        poison.swap(poisonWindows);
        overflow = std::exchange(poisonOverflow, 0);
    }
    for (auto const& [key, counts] : poison)
        LOG_INFO("module.leeroy", "living_poison_minute map={} instance={} window_ms={} hit_events={} bot_samples={} hazard_samples={}",
            key.first, key.second, windowMs, counts.hitEvents, counts.botSamples, counts.hazardSamples);
    if (overflow)
        LOG_WARN("module.leeroy", "living_poison_overflow window_ms={} unrecorded_updates={}", windowMs, overflow);
    std::size_t spots = 0;
    std::size_t exits = 0;
    std::size_t detours = 0;
    {
        std::lock_guard<std::mutex> lock(memoLock);
        spots = spotMembers.size();
        exits = exitMembers.size();
        detours = detourMembers.size();
        spotMembers.clear();
        exitMembers.clear();
        detourMembers.clear();
    }
    std::shared_ptr<Snapshot const> current;
    {
        std::lock_guard<std::mutex> lock(snapshotLock);
        current = snapshot;
    }
    if (spots || exits || detours)
        LOG_INFO("module.leeroy", "hazards_minute aware={} zones={} spot_members={} detour_members={} exit_members={}",
            current ? current->aware.size() : std::size_t(0), current ? current->zoneCount : std::size_t(0), spots,
            detours, exits);
}

#if LEEROY_HAS_HAZARD_FILTER
constexpr std::size_t kCandidates = 6; // safe spots tried against the navmesh per answer
constexpr float kMemoYards = 0.5f;

// How near the bot's follow walks it before it stops, from its edge: its formation's own distance (chaos, the
// default, widens it by up to about 3.5 yards every few seconds). A waypoint or leg must lie beyond it.
float FollowStop(PlayerbotAI* ai)
{
    if (Formation* formation = ai ? ai->GetAiObjectContext()->GetValue<Formation*>("formation")->Get() : nullptr)
        return formation->GetMaxDistance();
    return sPlayerbotAIConfig.FollowDistance;
}

bool Close(HazardPoint a, HazardPoint b)
{
    return std::hypot(a.x - b.x, a.y - b.y) < kMemoYards && std::fabs(a.z - b.z) < 2.0f;
}

std::uint64_t NowMs()
{
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count());
}

// Map thread. The zones on the bot's map that concern it (its phases; an add's own aura not for a tank) when the
// last pass found it aware, else null; `hold` keeps them alive, `own` holds the bot's share when it differs, and
// `selection` is what answers from that share are memoised against (HazardSelectionKey).
std::vector<HazardZone> const* ZonesFor(Player* bot, PlayerbotAI*& ai, std::shared_ptr<Snapshot const>& hold,
    std::vector<HazardZone>& own, std::uint64_t& selection)
{
    if (!enabled.load(std::memory_order_relaxed))
        return nullptr;
    ai = GET_PLAYERBOT_AI(bot);
    Player* master = ai ? ai->GetMaster() : nullptr;
    std::uint32_t const owner = awareMaster.load(std::memory_order_relaxed);
    if (!master || !owner || master->GetGUID().GetCounter() != owner)
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
    if (zones == hold->zones.end() || zones->second.empty())
        return nullptr;
    std::uint32_t const phase = bot->GetPhaseMask();
    bool const tank = hold->tanks.contains(member->first);
    selection = HazardSelectionKey(hold->version, phase, tank);
    if (std::all_of(zones->second.begin(), zones->second.end(),
            [&](HazardZone const& zone) { return HazardApplies(zone, phase, tank); }))
        return &zones->second;
    own = HazardZonesFor(zones->second, phase, tank);
    return own.empty() ? nullptr : &own;
}

// The first candidate the navmesh reaches from `from` that lands clear, no farther than the maximum shift and,
// for a detour, at least `minShift` away along a straight line that stays out of every zone.
bool FirstReachable(Player* bot, HazardPoint from, std::vector<HazardPoint> const& candidates,
    std::vector<HazardZone> const& zones, float reach, bool lineMustBeClear, float minShift, HazardPoint& out)
{
    for (HazardPoint const& candidate : candidates)
    {
        float x = candidate.x;
        float y = candidate.y;
        float z = candidate.z;
        if (!bot->GetMap()->CheckCollisionAndGetValidCoords(bot, from.x, from.y, from.z, x, y, z))
            continue;
        HazardPoint const landed{x, y, z};
        float const shift = std::hypot(landed.x - from.x, landed.y - from.y);
        if (!HazardClear(landed, zones, reach) || shift > kHazardMaxShiftYards || shift < minShift
            || (lineMustBeClear && !HazardSegmentClear(from, landed, zones, reach)))
            continue;
        out = landed;
        return true;
    }
    return false;
}

class GroupHazardFilter final : public PlayerbotHazardFilter
{
public:
    // The formation spot moved off a hazard (the goal), then, while the straight walk to it would cross one, a
    // waypoint round it: follow never walks the bot through a hazard to reach him.
    bool SafeFollowSpot(Player* bot, float& x, float& y, float& z) override
    {
        PlayerbotAI* ai = nullptr;
        std::shared_ptr<Snapshot const> hold;
        std::vector<HazardZone> own;
        std::uint64_t selection = 0;
        std::vector<HazardZone> const* zones = ZonesFor(bot, ai, hold, own, selection);
        if (!zones)
            return false;
        std::uint32_t const guid = bot->GetGUID().GetCounter();
        float const reach = bot->GetCombatReach();
        HazardPoint const spot{x, y, z};
        HazardPoint const at = PointOf(bot);

        Memo goal{selection, spot, false, spot};
        bool known = false;
        {
            std::lock_guard<std::mutex> lock(memoLock);
            auto const found = spotMemo.find(guid);
            if (found != spotMemo.end() && found->second.version == selection && Close(found->second.asked, spot))
            {
                goal = found->second;
                known = true;
            }
        }
        if (!known)
        {
            auto const candidates = HazardSafeSpots(spot, at, *zones, reach, kCandidates);
            goal.moved =
                !candidates.empty() && FirstReachable(bot, spot, candidates, *zones, reach, false, 0.0f, goal.answer);
            std::lock_guard<std::mutex> lock(memoLock);
            spotMemo[guid] = goal;
            if (goal.moved)
                spotMembers.insert(guid);
        }

        HazardPoint target = goal.answer;
        bool changed = goal.moved;
        float const step = HazardDetourStep(FollowStop(ai), reach);
        std::optional<HazardPoint> const waypoint =
            Detour(bot, guid, at, goal.answer, *zones, reach, selection, step);
        if (waypoint)
        {
            target = *waypoint;
            changed = true;
        }
        // Near a moving npc, short legs (or a wait), so follow plans again from each fresh pass.
        unsigned pathChecks = 0;
        auto const reachable = [&](HazardPoint const& candidate)
        {
            if (std::hypot(candidate.x - at.x, candidate.y - at.y) < 0.01f)
                return true; // waiting does not need a new route
            if (++pathChecks > kCandidates)
                return false;
            PathGenerator path(bot);
            path.SetUseRaycast(true); // preserve the straight leg's hazard proof
            path.SetPathLengthLimit(kScanYards);
            if (!path.CalculatePath(candidate.x, candidate.y, candidate.z)
                || path.GetPathType() != PATHFIND_NORMAL)
                return false;
            auto const& end = path.GetActualEndPosition();
            if (std::hypot(end.x - candidate.x, end.y - candidate.y) > 0.25f
                || std::fabs(end.z - candidate.z) > 0.25f)
                return false;
            float const height = bot->GetCollisionHeight() * 0.5f;
            // The route is already complete. Check collision trees directly;
            // CheckCollisionAndGetValidCoords would build a second navmesh ray.
            return bot->GetMap()->GetMapCollisionData().GetStaticTree().isInLineOfSight(
                       at.x, at.y, at.z + height, candidate.x, candidate.y, candidate.z + height,
                       VMAP::ModelIgnoreFlags::Nothing)
                && bot->GetMap()->GetDynamicMapTree().isInLineOfSight(
                       at.x, at.y, at.z + height, candidate.x, candidate.y, candidate.z + height,
                       bot->GetPhaseMask(), VMAP::ModelIgnoreFlags::Nothing);
        };
        if (auto const leg = HazardMovingLeg(at, goal.answer, waypoint, *zones, reach, step, std::cref(reachable)))
        {
            target = *leg;
            changed = true;
        }
        if (!changed)
            return false;
        x = target.x;
        y = target.y;
        z = target.z;
        return true;
    }

    bool HazardExit(Player* bot, float& x, float& y, float& z) override
    {
        PlayerbotAI* ai = nullptr;
        std::shared_ptr<Snapshot const> hold;
        std::vector<HazardZone> own;
        std::uint64_t selection = 0;
        std::vector<HazardZone> const* zones = ZonesFor(bot, ai, hold, own, selection);
        if (!zones)
            return false;
        std::uint32_t const guid = bot->GetGUID().GetCounter();
        float const reach = bot->GetCombatReach();
        HazardPoint const at = PointOf(bot);
        std::vector<HazardZone> standing;
        for (HazardZone const& zone : *zones)
            if (InHazard(at, zone, reach))
                standing.push_back(zone);
        if (standing.empty())
        {
            std::lock_guard<std::mutex> lock(memoLock);
            exitMemo.erase(guid);
            if (auto const guard = exitGuards.find(guid); guard != exitGuards.end())
                guard->second.standing.clear(); // stood clear: whatever it steps into next is an entry
            return false;
        }
        std::uint64_t const now = NowMs();

        Memo memo{selection, at, false, at};
        std::optional<HazardPoint> goal;
        bool known = false;
        bool yielded = false;
        bool yieldStarts = false;
        std::optional<Memo> previous;
        {
            std::lock_guard<std::mutex> lock(memoLock);
            auto const found = exitMemo.find(guid);
            // Entries count per zone, into a zone it did not stand in at its last answer and never into one that
            // moves (it walked onto the bot); an exit planned again inside the same zones counts nothing.
            if (HazardEntry const entry = NoteHazardZoneEntry(exitGuards[guid], standing, now);
                entry != HazardEntry::Exit)
            {
                yielded = true;
                yieldStarts = entry == HazardEntry::YieldStarts;
            }
            else if (found != exitMemo.end())
                previous = found->second;
            if (yielded)
                exitMemo.erase(guid);
            // Out of combat: his side is the side of the follow goal the last follow tick chose.
            if (auto const spot = spotMemo.find(guid); !yielded && spot != spotMemo.end()
                && ai->GetState() == BOT_STATE_NON_COMBAT)
                goal = spot->second.answer;
        }
        if (yieldStarts)
            LOG_INFO("module.leeroy", "hazard_exit_yield guid={} zones={}", guid, standing.size());
        if (yielded)
            return false;
        if (previous && previous->moved && HazardClear(previous->answer, *zones, reach)
            && std::hypot(previous->answer.x - at.x, previous->answer.y - at.y) <= kHazardMaxShiftYards)
        {
            memo = *previous;
            known = true;
        }
        else if (previous && !previous->moved && previous->version == selection && Close(previous->asked, at))
            known = true;
        if (!known)
        {
            HazardPoint toward = goal.value_or(at);
            if (Player* master = ai->GetMaster(); !goal && master && master->IsInMap(bot))
                toward = PointOf(master);
            auto const candidates = HazardExitSpots(at, goal, toward, *zones, reach, kCandidates);
            memo.moved =
                !candidates.empty() && FirstReachable(bot, at, candidates, *zones, reach, false, 0.0f, memo.answer);
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

private:
    // A waypoint round the hazard in the way, kept until the bot is near it; nullopt when the way is clear.
    // `reached`: a waypoint is kept until the bot is this near, and a new one must lie at least this far away,
    // beyond the distance at which follow stops walking (HazardDetourStep).
    static std::optional<HazardPoint> Detour(Player* bot, std::uint32_t guid, HazardPoint at, HazardPoint goal,
        std::vector<HazardZone> const& zones, float reach, std::uint64_t zonesVersion, float reached)
    {
        std::optional<Memo> previous;
        {
            std::lock_guard<std::mutex> lock(memoLock);
            auto const found = detourMemo.find(guid);
            if (found != detourMemo.end())
                previous = found->second;
        }
        if (previous && previous->version == zonesVersion && Close(previous->asked, goal)
            && std::hypot(previous->answer.x - at.x, previous->answer.y - at.y) > reached
            && HazardSegmentClear(at, previous->answer, zones, reach))
            return previous->answer;
        {
            std::lock_guard<std::mutex> lock(memoLock);
            detourMemo.erase(guid);
        }
        if (HazardSegmentClear(at, goal, zones, reach) || HazardAt(at, zones, reach))
            return std::nullopt; // the straight way is clear, or stepping out comes first
        bool const movingFirst = HazardFirstCrossingMoves(at, goal, zones, reach);
        {
            std::lock_guard<std::mutex> lock(memoLock);
            // A moving npc in the way passes: no pocket to give up on.
            if (!movingFirst && !HazardDetourAllowed(detourTracks[guid], at, goal))
                return std::nullopt; // circling in a pocket between hazards: straight, as stock
        }
        auto const waypoints = HazardDetour(at, goal, zones, reach, reached);
        HazardPoint waypoint;
        if (waypoints.empty() || !FirstReachable(bot, at, waypoints, zones, reach, true, reached, waypoint))
            return std::nullopt; // clear, standing in one (the exit's job), or no way round: straight as stock
        std::lock_guard<std::mutex> lock(memoLock);
        detourMemo[guid] = Memo{zonesVersion, goal, true, waypoint};
        detourMembers.insert(guid);
        return waypoint;
    }
};
#endif
}

std::vector<HazardZone> ScanHazardZones(Player* reference, std::vector<HazardPoint> const& anchors)
{
    std::vector<HazardZone> zones;
    Map* map = reference && reference->IsInWorld() ? reference->GetMap() : nullptr;
    if (!map)
        return zones;
    std::set<ObjectGuid> seen;
    for (HazardPoint const& anchor : anchors)
    {
        HazardScanner scanner{reference, anchor.x, anchor.y, sPlayerbotAIConfig.MaxAoeAvoidRadius,
            sPlayerbotAIConfig.AoeAvoidSpellWhitelist, seen, zones};
        Cell::VisitObjects(anchor.x, anchor.y, map, scanner, kScanYards);
    }
    return zones;
}

void ReloadHazards()
{
    enabled.store(sConfigMgr->GetOption<bool>("Leeroy.Hazards.Enable", true), std::memory_order_relaxed);
    untilPass = 0;
}

#if defined(DOJO_RAID_ACCEPTANCE) && DOJO_RAID_ACCEPTANCE
void DojoHazardOwner(std::uint32_t guid)
{
    // The map-thread movement callbacks also consult the master enable flag.
    // Preserve it across the synthetic group; no other group enters its snapshot.
    if (guid && !dojoOwner)
    {
        dojoWasEnabled = enabled.load(std::memory_order_relaxed);
        enabled.store(true, std::memory_order_relaxed);
    }
    else if (!guid && dojoOwner)
        enabled.store(dojoWasEnabled, std::memory_order_relaxed);
    dojoOwner = guid;
    untilPass = 0;
}
#endif

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
    summaryMs += diffMs;
    if (summaryMs >= 60000)
    {
        Summarise(summaryMs);
        summaryMs = 0;
    }
    if (diffMs < untilPass)
    {
        untilPass -= diffMs;
        return;
    }
    untilPass = kPassMs;

    bool const on = ContextEnabled();
    std::uint32_t const human = ContextOwner();
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
}

void AddHazardScripts() { new HazardDamage(); }

void FlushHazardSummary()
{
    if (summaryMs) Summarise(summaryMs);
    summaryMs = 0;
}

void ApplyHazards(Player* bot)
{
    bool const on = ContextEnabled();
    Player* owner = on ? FindOnline(ContextOwner()) : nullptr;
    Group* group = owner ? owner->GetGroup() : nullptr;
    if (!bot || !group || group->isBGGroup() || group->isBFGroup())
        return;
    Reconcile(bot, owner, group, on);
}
}
