// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "MechanicJob.h"
#include "GuildPorts.h"
#include "HazardJob.h"
#include "LeeroyMechanicPolicy.h"
#include "OfflineRaidJob.h"
#include "Realm/RealmPorts.h"
#include "Config.h"
#include "Creature.h"
#include "Group.h"
#include "Log.h"
#include "Map.h"
#include "ObjectAccessor.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "Playerbots.h"
#include "ScriptMgr.h"
#include "Spell.h"
#include "SpellAuras.h"
#include "SpellInfo.h"
#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <map>
#include <memory>
#include <mutex>
#include <set>
#include <unordered_map>
#include <utility>
#include <vector>

// The trigger, action and move refusal come from the local playerbots patch 0012; without it nobody moves.
#if __has_include("PlayerbotMechanicGuard.h")
#include "PlayerbotMechanicGuard.h"
#define LEEROY_HAS_MECHANIC_GUARD 1
#else
#define LEEROY_HAS_MECHANIC_GUARD 0
#endif

namespace Leeroy::Guild
{
namespace
{
using MapKey = std::pair<std::uint32_t, std::uint32_t>; // map id, instance id
using TableKey = std::pair<std::uint32_t, std::uint8_t>; // map id, difficulty

constexpr std::uint32_t kPassMs = 250;
constexpr std::uint32_t kPassesPerSummary = 240; // a minute
constexpr std::size_t kMaxEvents = 64;           // in force at once, every instance together
constexpr std::uint64_t kRepeatMs = 2000;        // a run out started again this soon is the same one
constexpr float kAnchorYards = 15.0f;            // a responder this far from every hazard scan centre gets its own

std::atomic<bool> enabled{false};
std::uint32_t untilPass = 0;
std::uint32_t passes = 0;

// World thread: the tables the connected mind sent, and the connection that sent them.
std::map<TableKey, std::shared_ptr<MechanicTable const>> tables;
std::uint64_t tablesSerial = 0;

std::uint64_t NowMs()
{
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count());
}

MapKey KeyOf(WorldObject const* object) { return {object->GetMapId(), object->GetInstanceId()}; }

std::uint64_t Packed(MapKey key) { return (std::uint64_t(key.first) << 32) | key.second; }

HazardPoint PointOf(WorldObject const* object)
{
    return {object->GetPositionX(), object->GetPositionY(), object->GetPositionZ()};
}

// Written whole by the world thread each pass, read by map threads.
struct Member
{
    MapKey key;
    std::uint8_t role = 0;
};
struct Instance
{
    std::shared_ptr<MechanicTable const> table;
    std::vector<std::uint32_t> party; // every player of the group or raid there: whom spacing counts
    std::vector<HazardZone> hazards;  // the known ground hazards, while a mechanic is in force there
};
struct Snapshot
{
    std::unordered_map<std::uint32_t, Member> members;
    std::map<MapKey, Instance> instances;
};
std::mutex snapshotLock;
std::shared_ptr<Snapshot const> snapshot;
std::atomic<bool> responders{false};

// Map threads add, the world thread ends: the mechanics in force.
struct Event
{
    std::uint64_t serial = 0;
    MapKey key;
    std::shared_ptr<MechanicTable const> table;
    std::size_t rule = 0;
    std::uint32_t spell = 0;
    MechanicCast cast;
    ObjectGuid caster;
    ObjectGuid target;
    std::uint32_t debuffed = 0;
    std::uint64_t start = 0;
    std::uint64_t end = 0;
    bool fromAura = false;  // started by the debuff landing, not by a cast
    bool cancelled = false; // the boss stopped the cast or the channel: released early
};
std::mutex eventLock;
std::vector<Event> events;
std::map<std::uint64_t, std::set<std::uint32_t>> moved; // by event serial: whom it sent somewhere
std::uint64_t nextSerial = 0;
std::uint64_t eventVersion = 0;
std::uint32_t droppedEvents = 0;
std::atomic<bool> anyActive{false};
// The instances with a mechanic in force, so a bot elsewhere is answered without a lock.
std::array<std::atomic<std::uint64_t>, 4> activeKeys{};
std::atomic<bool> keysOverflow{false};

// Under eventLock.
void RefreshKeys()
{
    std::set<std::uint64_t> keys;
    for (Event const& event : events)
        keys.insert(Packed(event.key));
    std::size_t slot = 0;
    for (std::uint64_t key : keys)
    {
        if (slot == activeKeys.size())
            break;
        activeKeys[slot++].store(key, std::memory_order_relaxed);
    }
    for (; slot < activeKeys.size(); ++slot)
        activeKeys[slot].store(0, std::memory_order_relaxed);
    keysOverflow.store(keys.size() > activeKeys.size(), std::memory_order_relaxed);
    anyActive.store(!events.empty(), std::memory_order_release);
}

[[maybe_unused]] bool KeyActive(MapKey key)
{
    if (!anyActive.load(std::memory_order_acquire))
        return false;
    if (keysOverflow.load(std::memory_order_relaxed))
        return true;
    std::uint64_t const packed = Packed(key);
    return std::any_of(activeKeys.begin(), activeKeys.end(), [&](auto const& slot)
        { return slot.load(std::memory_order_relaxed) == packed; });
}

std::shared_ptr<Snapshot const> Current()
{
    std::lock_guard<std::mutex> lock(snapshotLock);
    return snapshot;
}

// Map thread: the table of a raid instance with responders in it.
std::shared_ptr<MechanicTable const> TableFor(Map const* map)
{
    if (!enabled.load(std::memory_order_relaxed) || !responders.load(std::memory_order_relaxed) || !map
        || !map->IsRaid())
        return nullptr;
    std::shared_ptr<Snapshot const> const hold = Current();
    if (!hold)
        return nullptr;
    auto const found = hold->instances.find({map->GetId(), map->GetInstanceId()});
    return found == hold->instances.end() ? nullptr : found->second.table;
}

// Map thread.
void AddEvent(Event event)
{
    std::lock_guard<std::mutex> lock(eventLock);
    // A run out's cast and the aura it puts on the member (either may come first), or a reapplication: the same
    // run out, held longer.
    if (event.debuffed)
        for (Event& known : events)
            if (known.key == event.key && known.rule == event.rule && known.debuffed == event.debuffed
                && known.table == event.table && event.start < known.start + kRepeatMs)
            {
                known.end = std::max(known.end, event.end);
                return;
            }
    if (events.size() >= kMaxEvents)
    {
        ++droppedEvents;
        return;
    }
    event.serial = ++nextSerial;
    events.push_back(std::move(event));
    ++eventVersion;
    RefreshKeys();
}

void CastStart(Spell* spell, Unit* caster, SpellInfo const* info)
{
    if (!enabled.load(std::memory_order_relaxed) || !responders.load(std::memory_order_relaxed) || !spell || !caster
        || !info || !caster->IsInWorld() || !caster->ToCreature() || caster->IsControlledByPlayer())
        return;
    Map* map = caster->GetMap();
    std::shared_ptr<MechanicTable const> const table = TableFor(map);
    if (!table)
        return;
    std::vector<std::size_t> const rules = MechanicRulesFor(*table, info->Id);
    if (rules.empty())
        return;
    MechanicCast cast;
    cast.caster = PointOf(caster);
    cast.facing = caster->GetOrientation();
    Unit* target = spell->m_targets.GetUnitTarget();
    if (target && (target == caster || !target->IsInWorld() || target->GetMap() != map))
        target = nullptr;
    if (target)
        cast.target = PointOf(target);
    if (spell->m_targets.HasDst())
        if (WorldLocation const* dest = spell->m_targets.GetDstPos())
            cast.dest = HazardPoint{dest->GetPositionX(), dest->GetPositionY(), dest->GetPositionZ()};
    cast.castMs = static_cast<std::uint32_t>(std::max(0, spell->GetCastTime()));
    cast.spellDurationMs = static_cast<std::uint32_t>(std::max(0, info->GetMaxDuration()));
    std::uint64_t const now = NowMs();
    for (std::size_t index : rules)
    {
        MechanicRule const& rule = table->rules[index];
        Event event;
        event.key = KeyOf(caster);
        event.table = table;
        event.rule = index;
        event.spell = info->Id;
        event.cast = cast;
        event.caster = caster->GetGUID();
        if (target)
            event.target = target->GetGUID();
        if (rule.kind == MechanicKind::RunOutDebuff)
        {
            if (!target || !target->ToPlayer())
                continue; // no member named: its aura, when it lands, starts the run out
            event.debuffed = target->GetGUID().GetCounter();
        }
        event.start = now;
        event.end = now + MechanicLifetimeMs(rule, cast.castMs, cast.spellDurationMs);
        AddEvent(std::move(event));
    }
}

void AuraApplied(Unit* unit, Aura* aura)
{
    if (!enabled.load(std::memory_order_relaxed) || !responders.load(std::memory_order_relaxed) || !unit || !aura)
        return;
    Player* player = unit->ToPlayer();
    SpellInfo const* info = aura->GetSpellInfo();
    if (!player || !info || !player->IsInWorld())
        return;
    Unit* caster = aura->GetCaster();
    if (caster && (caster->IsControlledByPlayer() || !caster->ToCreature()))
        return;
    std::shared_ptr<MechanicTable const> const table = TableFor(player->GetMap());
    if (!table)
        return;
    std::uint64_t const now = NowMs();
    for (std::size_t index : MechanicRulesFor(*table, info->Id))
    {
        MechanicRule const& rule = table->rules[index];
        if (rule.kind != MechanicKind::RunOutDebuff)
            continue;
        Event event;
        event.key = KeyOf(player);
        event.table = table;
        event.rule = index;
        event.spell = info->Id;
        event.cast.caster = caster && caster->IsInWorld() && caster->GetMap() == player->GetMap() ? PointOf(caster)
            : PointOf(player);
        event.cast.facing = caster ? caster->GetOrientation() : 0.0f;
        event.cast.target = PointOf(player);
        event.cast.spellDurationMs = static_cast<std::uint32_t>(std::max(0, aura->GetMaxDuration()));
        if (caster)
            event.caster = caster->GetGUID();
        event.target = player->GetGUID();
        event.debuffed = player->GetGUID().GetCounter();
        event.start = now;
        event.end = now + MechanicLifetimeMs(rule, 0, event.cast.spellDurationMs);
        event.fromAura = true;
        AddEvent(std::move(event));
    }
}

// Map thread: the boss stopped a covered cast before it landed, or broke off a channel (Hellfire): what that cast
// started ends now. A debuff already on a member, and anything its aura started, stays.
void CastCancelled(Unit* caster, SpellInfo const* info)
{
    if (!enabled.load(std::memory_order_relaxed) || !anyActive.load(std::memory_order_acquire) || !caster || !info
        || !caster->IsInWorld() || !caster->ToCreature())
        return;
    MapKey const key = KeyOf(caster);
    std::uint64_t const now = NowMs();
    std::lock_guard<std::mutex> lock(eventLock);
    bool changed = false;
    for (Event& event : events)
        if (event.key == key && event.caster == caster->GetGUID() && event.spell == info->Id && !event.fromAura
            && event.table && event.table->rules[event.rule].kind != MechanicKind::RunOutDebuff && now < event.end)
        {
            event.end = now;
            event.cancelled = true;
            changed = true;
        }
    if (changed)
        ++eventVersion;
}

// Map threads: each bot's last answer, reused while the mechanics in force and its own place hold.
struct Memo
{
    std::uint64_t version = 0;
    std::uint64_t at = 0; // when it was worked out
    HazardPoint asked;
    bool found = false;
    HazardPoint spot;
};
std::mutex memoLock;
std::unordered_map<std::uint32_t, Memo> memos;

#if LEEROY_HAS_MECHANIC_GUARD
constexpr float kOthersYards = 60.0f;  // members farther than this never crowd a spot
constexpr std::size_t kCandidates = 6; // spots tried against the navmesh per answer
constexpr float kMemoYards = 0.5f;
constexpr std::uint64_t kRetryMs = 1000; // a failed search is tried again this soon: the others and targets move

// Map thread: what the mechanics in force ask of one bot now.
struct Plan
{
    std::vector<MechanicDuty> duties;
    std::vector<std::uint64_t> serials;
    std::vector<HazardPoint> others;
    std::vector<HazardZone> hazards;
    std::vector<Creature*> sightCasters;
    std::uint64_t version = 0;
};

bool Resolve(Player* bot, bool full, Plan& plan)
{
    if (!enabled.load(std::memory_order_relaxed) || !bot || !bot->IsInWorld() || !KeyActive(KeyOf(bot)))
        return false; // every other bot, at once
    std::uint32_t const guid = bot->GetGUID().GetCounter();
    std::shared_ptr<Snapshot const> const hold = Current();
    if (!hold)
        return false;
    auto const member = hold->members.find(guid);
    if (member == hold->members.end() || member->second.key != KeyOf(bot))
        return false;
    auto const instance = hold->instances.find(member->second.key);
    if (instance == hold->instances.end())
        return false;

    std::vector<Event> bound;
    {
        std::lock_guard<std::mutex> lock(eventLock);
        std::uint64_t const now = NowMs();
        plan.version = eventVersion;
        for (Event const& event : events)
            if (event.key == member->second.key && now < event.end && event.table
                && event.rule < event.table->rules.size()
                && MechanicBinds(event.table->rules[event.rule], member->second.role, guid, event.debuffed))
                bound.push_back(event);
    }
    if (bound.empty())
        return false;
    if (!full)
    {
        for (Event const& event : bound)
            plan.serials.push_back(event.serial);
        return true;
    }

    bool spacing = false;
    for (Event const& event : bound)
    {
        Creature* caster = !event.caster.IsEmpty() ? ObjectAccessor::GetCreature(*bot, event.caster) : nullptr;
        if (caster && (!caster->IsInWorld() || !caster->IsAlive()))
            caster = nullptr;
        MechanicRule const& rule = event.table->rules[event.rule];
        if (rule.area == MechanicArea::Caster && !caster)
            continue; // a whirl or a hellfire ends with its caster
        Unit* target = !event.target.IsEmpty() ? ObjectAccessor::GetUnit(*bot, event.target) : nullptr;
        std::optional<HazardPoint> targetNow;
        if (target && target->IsInWorld() && target->IsAlive())
            targetNow = PointOf(target);
        auto duty = MechanicDutyFor(rule, event.cast, caster ? PointOf(caster) : event.cast.caster, targetNow);
        if (!duty)
            continue;
        if (duty->hide && caster)
            plan.sightCasters.push_back(caster);
        spacing = spacing || duty->spacing > 0;
        plan.duties.push_back(*duty);
        plan.serials.push_back(event.serial);
    }
    if (plan.duties.empty())
        return false;
    if (spacing)
        for (std::uint32_t other : instance->second.party)
        {
            if (other == guid)
                continue;
            Player* player = ObjectAccessor::GetPlayer(bot->GetMap(), ObjectGuid::Create<HighGuid::Player>(other));
            if (player && player->IsInWorld() && player->IsAlive() && player->GetInstanceId() == bot->GetInstanceId()
                && bot->GetExactDist2d(player) <= kOthersYards)
                plan.others.push_back(PointOf(player));
        }
    plan.hazards = instance->second.hazards;
    return true;
}

MechanicSightFn SightOf(Plan const& plan)
{
    if (plan.sightCasters.empty())
        return nullptr;
    std::vector<Creature*> const casters = plan.sightCasters;
    return [casters](HazardPoint p)
    {
        for (Creature* caster : casters)
            if (caster->IsWithinLOS(p.x, p.y, p.z + 2.0f))
                return false;
        return true;
    };
}

void NoteMoved(Plan const& plan, std::uint32_t guid)
{
    std::lock_guard<std::mutex> lock(eventLock);
    for (std::uint64_t serial : plan.serials)
        if (std::any_of(events.begin(), events.end(), [&](Event const& e) { return e.serial == serial; }))
            moved[serial].insert(guid);
}

void Forget(std::uint32_t guid)
{
    std::lock_guard<std::mutex> lock(memoLock);
    memos.erase(guid);
}

class Responder final : public PlayerbotMechanicGuard
{
public:
    bool Active() const override { return anyActive.load(std::memory_order_acquire); }

    bool Binds(Player* bot) override
    {
        Plan plan;
        return Resolve(bot, false, plan);
    }

    bool MechanicSpot(Player* bot, float& x, float& y, float& z) override
    {
        Plan plan;
        std::uint32_t const guid = bot ? bot->GetGUID().GetCounter() : 0;
        if (!Resolve(bot, true, plan))
        {
            Forget(guid);
            return false;
        }
        float const reach = bot->GetCombatReach();
        HazardPoint const at = PointOf(bot);
        MechanicSightFn const hidden = SightOf(plan);
        if (MechanicKept(at, plan.duties, plan.others, reach, hidden))
        {
            Forget(guid);
            return false;
        }
        {
            std::lock_guard<std::mutex> lock(memoLock);
            auto const found = memos.find(guid);
            if (found != memos.end() && found->second.version == plan.version)
            {
                Memo const& memo = found->second;
                if (!memo.found && NowMs() < memo.at + kRetryMs
                    && std::hypot(memo.asked.x - at.x, memo.asked.y - at.y) < kMemoYards)
                    return false; // nothing reachable a moment ago
                if (memo.found && std::hypot(memo.spot.x - at.x, memo.spot.y - at.y) <= kMechanicMaxShiftYards
                    && MechanicKept(memo.spot, plan.duties, plan.others, reach, nullptr)
                    && HazardClear(memo.spot, plan.hazards, reach))
                {
                    x = memo.spot.x; // keep walking to the spot already chosen: no new path every tick
                    y = memo.spot.y;
                    z = memo.spot.z;
                    return true;
                }
            }
        }
        Memo memo{plan.version, NowMs(), at, false, at};
        for (HazardPoint const& candidate :
             MechanicSpots(at, plan.duties, plan.others, plan.hazards, reach, kCandidates, hidden))
        {
            float cx = candidate.x;
            float cy = candidate.y;
            float cz = candidate.z;
            if (!bot->GetMap()->CheckCollisionAndGetValidCoords(bot, at.x, at.y, at.z, cx, cy, cz))
                continue;
            HazardPoint const landed{cx, cy, cz};
            if (std::hypot(landed.x - at.x, landed.y - at.y) > kMechanicMaxShiftYards
                || !MechanicKept(landed, plan.duties, plan.others, reach, hidden)
                || !HazardClear(landed, plan.hazards, reach))
                continue;
            memo.found = true;
            memo.spot = landed;
            break;
        }
        {
            std::lock_guard<std::mutex> lock(memoLock);
            memos[guid] = memo;
        }
        if (!memo.found)
            return false;
        x = memo.spot.x;
        y = memo.spot.y;
        z = memo.spot.z;
        return true;
    }

    // Counted for mechanic_response only once the move was issued.
    void MechanicMoved(Player* bot) override
    {
        Plan plan;
        if (Resolve(bot, false, plan))
            NoteMoved(plan, bot->GetGUID().GetCounter());
    }

    bool MechanicRefuses(Player* bot, float x, float y, float z) override
    {
        Plan plan;
        return Resolve(bot, true, plan)
            && MechanicRefusesMove(HazardPoint{x, y, z}, plan.duties, plan.others, bot->GetCombatReach());
    }
};
#endif

// World thread. Whether a body holding an offline raid's lease is fenced by anything but that raid.
bool OfflineFenced(std::uint32_t guid)
{
    return Realm::RealmOwns(guid) || Training::DummyTrialOwns(guid) || Training::TrainingCleanupPending()
        || HasPendingBotCommands({guid}) || Training::HasTrainingJournal(guid) || Training::TrainingDcOwns(guid);
}

struct Collected
{
    std::map<MapKey, std::vector<Player*>> responders;
    std::map<std::string_view, std::uint32_t> skipped;
};

void Consider(Player* player, bool ownerGroup, bool offline, Snapshot& next, Collected& out)
{
    std::uint32_t const guid = player->GetGUID().GetCounter();
    PlayerbotAI* ai = GET_PLAYERBOT_AI(player);
    Map* map = player->GetMap();
    MechanicMemberFacts facts;
    facts.enabled = true;
    facts.cast = guid != HumanGuid() && IsCast(guid);
    facts.bot = ai && !IsSelfBot(player);
    facts.ownerGroup = ownerGroup;
    facts.offlineRaid = offline;
    facts.fenced = offline ? OfflineFenced(guid)
        : Realm::RealmOwns(guid) || !Realm::NativeOwnershipReason(guid).empty();
    facts.held = ai && (ai->HasStrategy("stay", BOT_STATE_COMBAT) || ai->HasStrategy("passive", BOT_STATE_COMBAT));
    facts.alive = player->IsAlive();
    facts.free = !player->IsBeingTeleported() && !player->GetVehicle() && !player->GetTransport();
    auto const table = map && map->IsRaid()
        ? tables.find({map->GetId(), static_cast<std::uint8_t>(map->GetDifficulty())}) : tables.end();
    facts.covered = table != tables.end();
    if (std::string_view const reason = MechanicSkipReason(facts); !reason.empty())
    {
        ++out.skipped[reason];
        return;
    }
    MapKey const key = KeyOf(player);
    next.members[guid] = {key, MechanicRole(PlayerbotAI::IsTank(player, true), PlayerbotAI::IsHeal(player, true),
                                   PlayerbotAI::IsRanged(player, true))};
    next.instances[key].table = table->second;
    out.responders[key].push_back(player);
}

void Collect(Snapshot& next, Collected& out)
{
    std::uint32_t const human = HumanGuid();
    Player* owner = human ? FindOnline(human) : nullptr;
    Group* group = owner ? owner->GetGroup() : nullptr;
    if (group && (group->isBGGroup() || group->isBFGroup()))
        group = nullptr;
    std::map<MapKey, std::set<std::uint32_t>> party;
    if (group)
        for (GroupReference* ref = group->GetFirstMember(); ref; ref = ref->next())
        {
            Player* member = ref->GetSource();
            if (!member || !member->IsInWorld())
                continue;
            party[KeyOf(member)].insert(member->GetGUID().GetCounter());
            if (member != owner)
                Consider(member, true, false, next, out);
        }
    for (std::uint32_t guid : OfflineRaidFighters())
        if (Player* player = FindOnline(guid))
        {
            party[KeyOf(player)].insert(guid);
            Consider(player, false, true, next, out);
        }
    for (auto& [key, instance] : next.instances)
        if (auto const found = party.find(key); found != party.end())
            instance.party.assign(found->second.begin(), found->second.end());
}

void Summarise(std::size_t members, std::size_t instances, Collected const& last)
{
    std::uint32_t dropped = 0;
    {
        std::lock_guard<std::mutex> lock(eventLock);
        std::swap(dropped, droppedEvents);
    }
    if (!members && !dropped)
        return;
    auto skipped = [&](std::string_view reason)
    {
        auto const found = last.skipped.find(reason);
        return found == last.skipped.end() ? 0u : found->second;
    };
    LOG_INFO("module.leeroy", "mechanics_minute responders={} instances={} held={} fenced={} dropped={}", members,
        instances, skipped("held"), skipped("fenced"), dropped);
}
}

void ReloadMechanics()
{
    enabled.store(sConfigMgr->GetOption<bool>("Leeroy.Mechanics.Enable", false), std::memory_order_relaxed);
    untilPass = 0;
}

void InstallMechanicGuard()
{
#if LEEROY_HAS_MECHANIC_GUARD
    static Responder guard;
    PlayerbotMechanicGuard::Set(&guard);
    LOG_INFO("module.leeroy", "mod-leeroy: mechanic guard installed");
#else
    LOG_INFO("module.leeroy", "mod-leeroy: mechanic guard unavailable: playerbots lacks PlayerbotMechanicGuard");
#endif
}

void TickMechanics(std::uint32_t diffMs)
{
    if (diffMs < untilPass)
    {
        untilPass -= diffMs;
        return;
    }
    untilPass = kPassMs;

    // A table belongs to the mind connection that sent it: a new mind sends its own at hello, or none when its
    // [raidplay] respond is off.
    Bridge& bridge = LeeroyModule::Instance().Bridge();
    if (!tables.empty() && (!bridge.Connected() || bridge.ConnectionSerial() != tablesSerial))
    {
        LOG_INFO("module.leeroy", "mechanic_tables_dropped tables={}", tables.size());
        tables.clear();
    }
    bool const on = enabled.load(std::memory_order_relaxed) && GuildEnabled() && !tables.empty();
    auto next = std::make_shared<Snapshot>();
    Collected collected;
    if (on)
        Collect(*next, collected);

    // End the mechanics whose time is up, and every one in an instance nobody responds in any more.
    struct Ended
    {
        std::string id;
        std::uint32_t spell;
        MechanicKind kind;
        std::size_t members;
        MapKey key;
        bool cancelled;
    };
    std::vector<Ended> ended;
    std::set<MapKey> inForce;
    {
        std::lock_guard<std::mutex> lock(eventLock);
        std::uint64_t const now = NowMs();
        auto const before = events.size();
        std::erase_if(events, [&](Event const& event)
        {
            if (now < event.end && next->instances.contains(event.key))
            {
                inForce.insert(event.key);
                return false;
            }
            MechanicRule const& rule = event.table->rules[event.rule];
            auto const found = moved.find(event.serial);
            ended.push_back({rule.id, event.spell, rule.kind, found == moved.end() ? 0 : found->second.size(),
                event.key, event.cancelled});
            moved.erase(event.serial);
            return true;
        });
        if (events.size() != before)
        {
            ++eventVersion;
            RefreshKeys();
        }
    }
    for (Ended const& end : ended)
        LOG_INFO("module.leeroy", "mechanic_response id={} spell={} kind={} members={} map={} instance={} cancelled={}",
            end.id, end.spell, MechanicKindName(end.kind), end.members, end.key.first, end.key.second,
            end.cancelled ? 1 : 0);

    // Spots keep clear of the ground hazards there while a mechanic is in force.
    for (MapKey const& key : inForce)
    {
        auto const bodies = collected.responders.find(key);
        if (bodies == collected.responders.end() || bodies->second.empty())
            continue;
        std::vector<HazardPoint> anchors;
        for (Player* body : bodies->second)
        {
            HazardPoint const at = PointOf(body);
            if (std::none_of(anchors.begin(), anchors.end(), [&](HazardPoint const& anchor)
                    { return std::hypot(anchor.x - at.x, anchor.y - at.y) <= kAnchorYards; }))
                anchors.push_back(at);
        }
        next->instances[key].hazards = ScanHazardZones(bodies->second.front(), anchors);
    }

    std::size_t const members = next->members.size();
    std::size_t const instances = next->instances.size();
    {
        std::lock_guard<std::mutex> lock(memoLock);
        std::erase_if(memos, [&](auto const& item) { return !next->members.contains(item.first); });
    }
    responders.store(!next->instances.empty(), std::memory_order_relaxed);
    {
        std::lock_guard<std::mutex> lock(snapshotLock);
        snapshot = std::move(next);
    }

    if (++passes >= kPassesPerSummary)
    {
        passes = 0;
        Summarise(members, instances, collected);
    }
}

void RegisterMechanicsHandler()
{
    RegisterCommand("raid_mechanics", [](nlohmann::json const& message) -> LeeroyCommandResult
    {
        std::string error;
        auto table = ParseRaidMechanics(message, error);
        if (!table)
            return {false, error};
        TableKey const key{table->map, table->difficulty};
        std::uint64_t const serial = LeeroyModule::Instance().Bridge().ConnectionSerial();
        if (serial != tablesSerial)
        {
            tables.clear(); // the last mind's tables never mix with this one's
            tablesSerial = serial;
        }
        if (!tables.contains(key) && tables.size() >= kMechanicMaxTables)
            return {false, "too_many_tables"};
        std::size_t const rules = table->rules.size();
        tables[key] = std::make_shared<MechanicTable const>(std::move(*table));
        LOG_INFO("module.leeroy", "mechanic_table map={} difficulty={} rules={}", key.first, uint32(key.second),
            rules);
        return {true, {}};
    });
}

namespace
{
class MechanicSpellScript : public AllSpellScript
{
public:
    MechanicSpellScript()
        : AllSpellScript("LeeroyMechanicSpellScript", {ALLSPELLHOOK_ON_PREPARE, ALLSPELLHOOK_ON_CAST_CANCEL}) { }

    void OnSpellPrepare(Spell* spell, Unit* caster, SpellInfo const* info) override { CastStart(spell, caster, info); }

    void OnSpellCastCancel(Spell* /*spell*/, Unit* caster, SpellInfo const* info, bool /*bySelf*/) override
    {
        CastCancelled(caster, info);
    }
};

class MechanicUnitScript : public UnitScript
{
public:
    MechanicUnitScript() : UnitScript("LeeroyMechanicUnitScript", true, {UNITHOOK_ON_AURA_APPLY}) { }

    void OnAuraApply(Unit* unit, Aura* aura) override { AuraApplied(unit, aura); }
};
}

void AddMechanicScripts()
{
    new MechanicSpellScript();
    new MechanicUnitScript();
}
}
