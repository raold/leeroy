// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#include "DojoExecutor.h"

#include "DojoObservation.h"
#include "DojoRuntime.h"
#include "DojoSha256.h"

#ifndef DOJO_TRAINING_BUILD
#error "DOJO_TRAINING_BUILD is not set: mod-dojo.cmake was skipped (the module folder must be named exactly mod-dojo)"
#endif

#if DOJO_TRAINING_BUILD

#include "Arena.h"
#include "Battleground.h"
#include "GameTime.h"
#include "Log.h"
#include "Map.h"
#include "MotionMaster.h"
#include "MoveSpline.h"
#include "MoveSplineInit.h"
#include "ObjectAccessor.h"
#include "Opcodes.h"
#include "Pet.h"
#include "TemporarySummon.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "Playerbots.h"
#include "Spell.h"
#include "SpellAuras.h"
#include "SpellInfo.h"
#include "SpellMgr.h"
#include "World.h"
#include "WorldConfig.h"
#include "WorldPacket.h"
#include "WorldSession.h"

#include <algorithm>
#include <cmath>
#include <sstream>

using nlohmann::json;

namespace Dojo
{
namespace
{
constexpr char const* kOwner = "mod-dojo";
constexpr std::size_t kTraceLimit = 4096;
constexpr float kBlockedYards = 0.25f;     // a move shorter than this ran into something at once
constexpr float kEyeHeight = 2.0f;
constexpr float kPi = 3.14159265358979323846f;
constexpr std::uint32_t kTeamMarkers[] = {SPELL_ALLIANCE_GOLD_FLAG, SPELL_ALLIANCE_GREEN_FLAG, SPELL_HORDE_GOLD_FLAG,
    SPELL_HORDE_GREEN_FLAG};

std::string CastResultName(std::uint8_t result)
{
    switch (SpellCastResult(result))
    {
        case SPELL_FAILED_AFFECTING_COMBAT: return "affecting_combat";
        case SPELL_FAILED_BAD_IMPLICIT_TARGETS: return "bad_implicit_targets";
        case SPELL_FAILED_BAD_TARGETS: return "bad_targets";
        case SPELL_FAILED_CASTER_AURASTATE: return "caster_aurastate";
        case SPELL_FAILED_CASTER_DEAD: return "caster_dead";
        case SPELL_FAILED_CONFUSED: return "confused";
        case SPELL_FAILED_DONT_REPORT: return "dont_report";
        case SPELL_FAILED_FIZZLE: return "fizzle";
        case SPELL_FAILED_FLEEING: return "fleeing";
        case SPELL_FAILED_IMMUNE: return "immune";
        case SPELL_FAILED_INTERRUPTED: return "interrupted";
        case SPELL_FAILED_INTERRUPTED_COMBAT: return "interrupted_combat";
        case SPELL_FAILED_LINE_OF_SIGHT: return "line_of_sight";
        case SPELL_FAILED_MOVING: return "moving";
        case SPELL_FAILED_NOT_HERE: return "not_here";
        case SPELL_FAILED_NOT_INFRONT: return "not_infront";
        case SPELL_FAILED_NOT_KNOWN: return "not_known";
        case SPELL_FAILED_NOT_READY: return "not_ready";
        case SPELL_FAILED_NO_POWER: return "no_power";
        case SPELL_FAILED_OUT_OF_RANGE: return "out_of_range";
        case SPELL_FAILED_PACIFIED: return "pacified";
        case SPELL_FAILED_PREVENTED_BY_MECHANIC: return "prevented_by_mechanic";
        case SPELL_FAILED_ROOTED: return "rooted";
        case SPELL_FAILED_SILENCED: return "silenced";
        case SPELL_FAILED_SPELL_IN_PROGRESS: return "spell_in_progress";
        case SPELL_FAILED_STUNNED: return "stunned";
        case SPELL_FAILED_TARGETS_DEAD: return "targets_dead";
        case SPELL_FAILED_TARGET_FRIENDLY: return "target_friendly";
        case SPELL_FAILED_TOO_CLOSE: return "too_close";
        case SPELL_FAILED_UNIT_NOT_INFRONT: return "unit_not_infront";
        case SPELL_FAILED_CANT_DO_THAT_RIGHT_NOW: return "cant_do_that_right_now";
        case SPELL_FAILED_NO_VALID_TARGETS: return "no_valid_targets";
        default: return "spell_failed_" + std::to_string(result);
    }
}

json ToJson(TraceRecord const& r)
{
    return {{"seq", r.seq}, {"generation", r.generation}, {"at_ms", r.atMs}, {"actor", r.actor}, {"producer", r.producer},
        {"kind", r.kind}, {"action_id", r.actionId}, {"origin", r.origin}, {"action", r.action}, {"input_ms", r.inputMs},
        {"due_ms", r.dueMs}, {"exec_ms", r.execMs}, {"outcome", r.outcome}, {"detail", r.detail}};
}

// Ruling 5: the server log keeps lifecycle lines and anomalies; everything else is drained from the buffer into the
// tooling's run files.
bool Logged(TraceRecord const& r)
{
    return r.kind == "ownership" || r.kind == "neutralize" || r.kind == "generation" || r.outcome == "dropped"
        || r.outcome == "refused" || r.outcome == "CHANGED" || r.producer.rfind("stock", 0) == 0;
}

std::optional<EventKind> PerceivedKind(std::uint16_t opcode)
{
    switch (opcode)
    {
        case SMSG_SPELL_START: return EventKind::CastStarted;
        case SMSG_SPELL_GO: return EventKind::CastCompleted;
        case SMSG_SPELL_FAILURE: return EventKind::CastStopped;
        default: return std::nullopt;
    }
}

std::string Join(std::vector<std::string> const& parts, char const* separator)
{
    std::string out;
    for (std::string const& p : parts)
        out += (out.empty() ? "" : separator) + p;
    return out;
}

std::uint64_t GuidOf(Unit const* unit)
{
    return unit ? unit->GetGUID().GetRawValue() : 0;
}

bool IsTeamMarker(std::uint32_t spellId)
{
    return std::find(std::begin(kTeamMarkers), std::end(kTeamMarkers), spellId) != std::end(kTeamMarkers);
}
}

Control& Control::Instance()
{
    static Control control;
    return control;
}

std::uint64_t Control::Now() const
{
    return std::uint64_t(GameTime::GetGameTimeMS().count());
}

std::string Control::Prepare(Manifest const& manifest)
{
    std::vector<KitAbility> kit;
    for (KitSpell const& k : manifest.loadout.kit)
    {
        SpellInfo const* info = sSpellMgr->GetSpellInfo(k.spellId);
        if (!info || info->IsPassive())
            return "kit spell " + std::to_string(k.spellId) + " is not an active spell in the spell data";
        kit.push_back({k.spellId, k.name, info->NeedsExplicitUnitTarget() ? TargetSlot::Enemy : TargetSlot::Self,
            (info->FacingCasterFlags & SPELL_FACING_FLAG_INFRONT) != 0});
    }
    std::string error;
    _catalog = BuildCatalog(kit, error);
    if (!_catalog)
        return error;
    LOG_INFO("module.dojo", "mod-dojo: action catalog {}: {} actions, sha256 {}", kActionSchema, _catalog->entries.size(),
        _catalog->sha256);
    return "";
}

void Control::Register(std::size_t slot, std::uint64_t guid)
{
    if (slot >= kScenarioActors || _slots[slot].guid == guid)
        return;
    _slots[slot] = Slot{};
    _slots[slot].guid = guid;
}

bool Control::IsRegistered(Player const* player) const
{
    if (!player)
        return false;
    std::uint64_t const guid = player->GetGUID().GetRawValue();
    return std::any_of(_slots.begin(), _slots.end(), [guid](Slot const& s) { return s.guid && s.guid == guid; });
}

bool Control::Owns(std::size_t slot) const
{
    return slot < kScenarioActors && _slots[slot].epoch != 0;
}

Player* Control::Find(std::size_t slot) const
{
    if (slot >= kScenarioActors || !_slots[slot].guid)
        return nullptr;
    return ObjectAccessor::FindConnectedPlayer(ObjectGuid(_slots[slot].guid));
}

Player* Control::Enemy(std::size_t slot) const
{
    return Find(slot == 0 ? 1 : 0);
}

bool Control::Visible(Player* self, Player* enemy) const
{
    return self && enemy && enemy->IsInWorld() && !enemy->IsBeingTeleported() && self->IsInMap(enemy)
        && self->CanSeeOrDetect(enemy);
}

void Control::Record(TraceRecord record)
{
    record.seq = ++_seq;
    record.generation = _generation;
    record.atMs = Now();
    if (Logged(record))
        LOG_INFO("module.dojo", "dojo-trace {}", CanonicalJson(ToJson(record)));
    _trace.push_back(std::move(record));
    while (_trace.size() > kTraceLimit)
        _trace.pop_front();
}

void Control::Note(std::size_t actor, std::string const& producer, std::string const& kind, std::string const& outcome,
    std::string const& detail)
{
    Record({0, 0, actor, producer, kind, 0, "", "", 0, 0, 0, outcome, detail});
}

void Control::BeginGeneration(std::uint64_t generation, std::string const& why)
{
    for (std::size_t i = 0; i < kScenarioActors; ++i)
    {
        RecordQueue(_queue.Drop(i, why, Now()));
        _slots[i].casts.clear();
    }
    _scope.reset();
    _stopping.reset();
    std::uint64_t const previous = _generation;
    _generation = generation;
    _firstActionId = _nextActionId;
    Record({0, 0, 0, "control", "generation", 0, "", "", 0, 0, 0, "began",
        "generation " + std::to_string(generation) + " after " + std::to_string(previous) + " (" + why
            + "); action ids from " + std::to_string(_firstActionId)});
}

std::optional<PendingView> Control::PendingFor(std::size_t slot) const
{
    std::optional<QueuedCommand> const pending = _catalog ? _queue.Pending(slot) : std::nullopt;
    if (!pending || pending->action >= _catalog->entries.size())
        return std::nullopt;
    CatalogEntry const& entry = _catalog->entries[pending->action];
    return PendingView{entry.ability, entry.movement != Movement::Hold, pending->dueMs};
}

std::uint64_t Control::Enqueue(std::size_t slot, std::size_t action, std::string const& producer, std::string const& origin,
    std::string& error)
{
    if (!_catalog || slot >= kScenarioActors || !_slots[slot].guid || action >= _catalog->entries.size())
    {
        error = "no such actor or action";
        return 0;
    }
    if (!_slots[slot].epoch)
    {
        error = "actor " + std::to_string(slot) + " is not owned";
        return 0;
    }
    std::uint64_t const actionId = _nextActionId++;
    ++_slots[slot].counters.commands;
    RecordQueue(_queue.Submit(slot, action, actionId, producer, origin, Now()));
    return actionId;
}

std::string Control::TraceSince(std::uint64_t seq, std::size_t max) const
{
    std::string out;
    std::size_t shown = 0;
    std::uint64_t first = 0;
    for (TraceRecord const& r : _trace)
    {
        if (r.seq <= seq)
            continue;
        if (!first)
            first = r.seq;
        if (shown == max)
            break;
        out += "dojo-trace " + CanonicalJson(ToJson(r)) + "\n";
        ++shown;
    }
    // A gap between the asked-for sequence number and the first one still held means records were lost from the
    // bounded buffer; the tooling treats that as missing evidence.
    bool const lost = first && first > seq + 1;
    return out + "dojo control trace: " + std::to_string(shown) + " records after " + std::to_string(seq) + ", newest "
        + std::to_string(_seq) + (lost ? ", LOST " + std::to_string(first - seq - 1) + " records" : "");
}

std::vector<TraceRecord> Control::RecordsSince(std::uint64_t seq, bool& lost) const
{
    std::vector<TraceRecord> out;
    lost = false;
    for (TraceRecord const& r : _trace)
        if (r.seq > seq)
        {
            if (out.empty() && r.seq > seq + 1)
                lost = true;
            out.push_back(r);
        }
    lost = lost || (out.empty() && _seq > seq);
    return out;
}

void Control::RecordQueue(std::vector<QueueEvent> const& events)
{
    for (QueueEvent const& e : events)
    {
        TraceRecord r;
        r.actor = e.command.actor;
        r.producer = e.command.producer;
        r.kind = "command";
        r.actionId = e.command.actionId;
        r.origin = e.command.origin;
        r.action = _catalog && e.command.action < _catalog->entries.size() ? _catalog->entries[e.command.action].name : "?";
        r.inputMs = e.command.inputMs;
        r.dueMs = e.command.dueMs;
        r.outcome = std::string(QueueOutcomeName(e.outcome));
        r.detail = e.reason + (e.byActionId ? " (action " + std::to_string(e.byActionId) + ")" : "");
        if (e.command.actor < kScenarioActors)
        {
            Counters& c = _slots[e.command.actor].counters;
            if (e.outcome == QueueOutcome::Superseded)
                ++c.superseded;
            else if (e.outcome == QueueOutcome::Kept)
                ++c.kept;
            else if (e.outcome == QueueOutcome::Dropped)
                ++c.dropped;
        }
        Record(std::move(r));
    }
}

void Control::Update()
{
    if (!_catalog)
        return;
    for (std::size_t i = 0; i < kScenarioActors; ++i)
    {
        Slot& s = _slots[i];
        if (!s.guid)
            continue;
        Player* player = Find(i);
        PlayerbotAI* ai = player ? GET_PLAYERBOT_AI(player) : nullptr;
        if (s.epoch && (!ai || ai->GetExternalControlEpoch() != s.epoch))
            Lose(i, !player ? "the actor logged out" : !ai ? "the actor has no AI" : "the actor's AI no longer carries the epoch");
        if (!ai)
            continue;
        if (!s.epoch && s.wanted && !_shuttingDown)
            Acquire(i, player);
        if (player->IsInWorld() && !player->IsBeingTeleported())
            Observe(i, player);
    }
    for (QueuedCommand const& c : _queue.TakeDue(Now()))
        Execute(c);
}

bool Control::Acquire(std::size_t slot, Player* player)
{
    Slot& s = _slots[slot];
    PlayerbotAI* ai = GET_PLAYERBOT_AI(player);
    std::uint64_t const epoch = ai ? ai->AcquireExternalControl(kOwner) : 0;
    if (!epoch)
    {
        if (!s.refusalTraced)
            Record({0, 0, slot, "control", "ownership", 0, "", "", 0, 0, 0, "refused",
                "mod-playerbots refused the acquisition (owner " + (ai ? ai->GetExternalControlOwner() : std::string("none")) + ")"});
        s.refusalTraced = true;
        return false;
    }
    s.refusalTraced = false;
    s.epoch = epoch;
    s.casts.clear();
    s.spline = 0;
    s.seenSpline = player->movespline->GetId();
    s.seenGenerator = IDLE_MOTION_TYPE;
    s.petSeen = false;
    ++s.counters.acquisitions;
    Record({0, 0, slot, "control", "ownership", 0, "", "", 0, 0, 0, "acquired", "epoch " + std::to_string(epoch)});
    if (player->IsInWorld() && !player->IsBeingTeleported())
        Neutralize(slot, player, "acquire");
    s.target = player->GetTarget().GetRawValue();
    s.victim = GuidOf(player->GetVictim());
    return true;
}

void Control::Neutralize(std::size_t slot, Player* player, std::string const& producer)
{
    Slot& s = _slots[slot];
    std::vector<std::string> done;
    if (player->IsNonMeleeSpellCast(false, false, true))
    {
        player->InterruptNonMeleeSpells(false);
        done.push_back("interrupted a cast");
    }
    if (!player->SpellQueue.empty())
    {
        player->SpellQueue.clear();
        done.push_back("cleared the spell queue");
    }
    if (!player->movespline->Finalized() || player->GetMotionMaster()->GetCurrentMovementGeneratorType() != IDLE_MOTION_TYPE)
    {
        player->GetMotionMaster()->Clear();
        player->StopMoving();
        done.push_back("stopped movement");
    }
    if (player->GetVictim())
    {
        player->AttackStop();
        done.push_back("stopped auto-attack");
    }
    if (Pet* pet = player->GetPet())
    {
        player->RemovePet(pet, PET_SAVE_NOT_IN_SLOT);
        done.push_back("dismissed a pet (pets are unsupported)");
    }
    if (Guardian* guardian = player->GetGuardianPet())
    {
        guardian->UnSummon();
        done.push_back("dismissed a guardian (pets are unsupported)");
    }
    s.seenSpline = player->movespline->GetId();
    s.victim = GuidOf(player->GetVictim());
    Record({0, 0, slot, producer, "neutralize", 0, "", "", 0, 0, 0, done.empty() ? "nothing" : "executed",
        done.empty() ? "nothing was in flight" : Join(done, "; ")});
}

void Control::Lose(std::size_t slot, std::string const& why)
{
    Slot& s = _slots[slot];
    std::uint64_t const epoch = s.epoch;
    s.epoch = 0;
    s.casts.clear();
    ++s.counters.losses;
    RecordQueue(_queue.Drop(slot, "ownership ended: " + why, Now()));
    Record({0, 0, slot, "control", "ownership", 0, "", "", 0, 0, 0, "ended", why + " (epoch " + std::to_string(epoch) + ")"});
}

std::string Control::Own(std::size_t slot)
{
    if (slot >= kScenarioActors || !_slots[slot].guid)
        return "dojo control own: no such actor";
    Slot& s = _slots[slot];
    s.wanted = true;
    if (s.epoch)
        return "dojo control own: actor " + std::to_string(slot) + " is already owned (epoch " + std::to_string(s.epoch) + ")";
    Player* player = Find(slot);
    if (!player || !GET_PLAYERBOT_AI(player))
        return "dojo control own: actor " + std::to_string(slot) + " is offline; it is acquired when it logs in";
    return Acquire(slot, player) ? "dojo control own: actor " + std::to_string(slot) + " acquired (epoch " + std::to_string(s.epoch) + ")"
                                 : "dojo control own: refused by mod-playerbots";
}

std::string Control::Release(std::size_t slot)
{
    if (slot >= kScenarioActors || !_slots[slot].guid)
        return "dojo control release: no such actor";
    Slot& s = _slots[slot];
    s.wanted = false;
    if (!s.epoch)
        return "dojo control release: actor " + std::to_string(slot) + " is not owned";
    Player* player = Find(slot);
    PlayerbotAI* ai = player ? GET_PLAYERBOT_AI(player) : nullptr;
    std::uint64_t const epoch = s.epoch;
    RecordQueue(_queue.Drop(slot, "released", Now()));
    s.casts.clear();
    bool const released = ai && ai->ReleaseExternalControl(epoch);
    s.epoch = 0;
    ++s.counters.releases;
    if (player)
    {
        s.seenSpline = player->movespline->GetId();
        s.target = player->GetTarget().GetRawValue();
        s.victim = GuidOf(player->GetVictim());
    }
    Record({0, 0, slot, "release", "ownership", 0, "", "", 0, 0, 0, released ? "released" : "ended",
        "epoch " + std::to_string(epoch) + (released ? "; stock behaviour resumes" : "; the AI no longer carried it")});
    return "dojo control release: actor " + std::to_string(slot) + (released ? " handed back to stock behaviour" : " was no longer owned");
}

std::string Control::Submit(std::size_t slot, std::vector<std::string> const& actions, bool force)
{
    if (!_catalog)
        return "dojo control act: no catalog (the scenario is not ready)";
    if (slot >= kScenarioActors || !_slots[slot].guid)
        return "dojo control act: no such actor";
    if (!_slots[slot].epoch)
        return "dojo control act: actor " + std::to_string(slot) + " is not owned";
    Player* self = Find(slot);
    if (!self || !self->IsInWorld())
        return "dojo control act: actor " + std::to_string(slot) + " is not in the world";
    std::vector<std::size_t> indices;
    for (std::string const& name : actions)
    {
        std::optional<std::size_t> index = FindAction(*_catalog, name);
        if (!index)
        {
            bool numeric = !name.empty() && std::all_of(name.begin(), name.end(), [](char c) { return c >= '0' && c <= '9'; });
            if (numeric && name.size() < 6 && std::stoul(name) < _catalog->entries.size())
                index = std::stoul(name);
        }
        if (!index)
            return "dojo control act: unknown action " + name + " (dojo control actions lists them)";
        indices.push_back(*index);
    }
    // The mask answers for what the actor knew when the command was decided: its delayed observation. A masked action
    // is refused unless forced, which the acceptance uses to show the executor's own validation.
    std::uint64_t const now = Now();
    std::string why;
    std::optional<ActorObservation> const seen = Observer::Instance().Observe(slot, now, why);
    if (!seen && !force)
        return "dojo control act: actor " + std::to_string(slot) + " has no observation yet (" + why + "); nothing submitted";
    MaskResult const mask = ComputeMask(*_catalog, seen ? seen->knowledge : ActorKnowledge{}, Observer::Instance().Rules());
    std::string out;
    for (std::size_t index : indices)
    {
        std::string const origin = "console:" + std::to_string(_nextOrigin++);
        if (!mask.mask[index] && !force)
        {
            Record({0, 0, slot, "console", "command", 0, origin, _catalog->entries[index].name, now, 0, 0, "refused",
                "masked: " + mask.abilityReasons[_catalog->entries[index].ability]});
            out += _catalog->entries[index].name + ": refused (masked)\n";
            continue;
        }
        std::string error;
        std::uint64_t const actionId = Enqueue(slot, index, force ? "console (forced)" : "console", origin, error);
        if (!actionId)
            return out + "dojo control act: " + error;
        std::optional<QueuedCommand> const pending = _queue.Pending(slot);
        out += _catalog->entries[index].name + ": action " + std::to_string(actionId) + " "
            + (index == 0 ? (pending ? "kept" : "idle") : "queued") + (mask.mask[index] ? "" : " (masked, forced)") + "\n";
    }
    return out + "dojo control act: " + std::to_string(indices.size()) + " submitted, command delay " + std::to_string(_queue.CommandDelayMs())
        + " ms, knowledge " + (seen ? std::to_string(now - seen->perceptMs) + " ms old" : std::string("none (forced)"));
}

void Control::Execute(QueuedCommand const& command)
{
    TraceRecord r;
    r.actor = command.actor;
    r.producer = "executor";
    r.kind = "command";
    r.actionId = command.actionId;
    r.origin = command.origin;
    r.action = _catalog->entries[command.action].name;
    r.inputMs = command.inputMs;
    r.dueMs = command.dueMs;
    r.execMs = Now();
    Slot& s = _slots[command.actor];
    if (command.actionId < _firstActionId)
    {
        // BeginGeneration drops these; one that still arrives belongs to an episode that is over.
        r.outcome = "rejected";
        r.detail = "from before generation " + std::to_string(_generation);
        ++s.counters.rejected;
        Record(std::move(r));
        return;
    }
    Player* self = Find(command.actor);
    PlayerbotAI* ai = self ? GET_PLAYERBOT_AI(self) : nullptr;
    if (!s.epoch || !ai || ai->GetExternalControlEpoch() != s.epoch || !self->IsInWorld() || self->IsBeingTeleported())
    {
        r.outcome = "rejected";
        r.detail = "not owned or not in the world";
        ++s.counters.rejected;
        Record(std::move(r));
        return;
    }
    CatalogEntry const& entry = _catalog->entries[command.action];
    Ability const& ability = _catalog->abilities[entry.ability];
    Player* enemy = Enemy(command.actor);
    std::vector<Part> parts;
    if (ability.kind == AbilityKind::StopCast)
        parts.push_back(StopCast(command.actor, self, command));
    if (entry.movement == Movement::Hold && !self->movespline->Finalized())
    {
        self->StopMoving();
        s.spline = self->movespline->GetId();
        parts.push_back({"hold", true, "stopped"});
    }
    if (entry.facing != Facing::Keep)
        parts.push_back(Face(command.actor, self, enemy, entry.facing));
    switch (ability.kind)
    {
        case AbilityKind::Cast: parts.push_back(Cast(command.actor, self, enemy, ability, command)); break;
        case AbilityKind::TargetEnemy: parts.push_back(Select(command.actor, self, enemy, false)); break;
        case AbilityKind::ClearTarget: parts.push_back(Select(command.actor, self, enemy, true)); break;
        case AbilityKind::AttackOn: parts.push_back(Attack(command.actor, self, enemy, true)); break;
        case AbilityKind::AttackOff: parts.push_back(Attack(command.actor, self, enemy, false)); break;
        default: break;
    }
    if (entry.movement != Movement::Hold)
        parts.push_back(Move(command.actor, self, entry.movement));
    // Whatever the core changed while it carried out this command is the executor's doing.
    s.target = self->GetTarget().GetRawValue();
    s.victim = GuidOf(self->GetVictim());
    s.seenSpline = self->movespline->GetId();
    std::vector<std::string> details;
    bool ok = true;
    for (Part const& p : parts)
    {
        ok = ok && p.ok;
        details.push_back(p.part + " " + (p.ok ? "ok" : "FAILED") + (p.detail.empty() ? "" : ": " + p.detail));
    }
    r.outcome = ok ? "executed" : "rejected";
    r.detail = details.empty() ? "nothing to do" : Join(details, "; ");
    ++(ok ? s.counters.executed : s.counters.rejected);
    Record(std::move(r));
}

Control::Part Control::Cast(std::size_t slot, Player* self, Player* enemy, Ability const& ability, QueuedCommand const& command)
{
    KitAbility const& k = _catalog->kit[ability.kit];
    Part part{"cast " + ability.name, false, ""};
    if (ability.target == TargetSlot::Enemy && !Visible(self, enemy))
    {
        part.detail = "no visible enemy";
        return part;
    }
    Slot& s = _slots[slot];
    s.castCount = s.castCount == 255 ? 1 : std::uint8_t(s.castCount + 1);
    SpellCastTargets targets;
    if (ability.target == TargetSlot::Enemy)
        targets.SetUnitTarget(enemy);
    // The client's own request: CMSG_CAST_SPELL with its cast count, the spell and no cast flags.
    WorldPacket packet(CMSG_CAST_SPELL, 1 + 4 + 1 + 9);
    packet << uint8(s.castCount) << uint32(k.spellId) << uint8(0);
    targets.Write(packet);
    std::size_t const queued = self->SpellQueue.size();
    _scope = Scope{slot, command.actionId, command.origin, s.castCount, k.spellId, std::nullopt, false, false};
    self->GetSession()->HandleCastSpellOpcode(packet);
    Scope const scope = *_scope;
    _scope.reset();
    if (scope.failed)
    {
        part.detail = CastResultName(*scope.failed);
        return part;
    }
    part.ok = true;
    if (scope.went)
        part.detail = "cast";
    else if (scope.started)
    {
        s.casts.push_back({scope.castCount, k.spellId, command.actionId, command.origin, true});
        part.detail = "casting";
    }
    else if (self->SpellQueue.size() > queued)
    {
        s.casts.push_back({scope.castCount, k.spellId, command.actionId, command.origin, false});
        part.detail = "queued in the core's spell queue";
    }
    else
    {
        part.ok = false;
        part.detail = "the core ignored the request";
    }
    return part;
}

Control::Part Control::StopCast(std::size_t slot, Player* self, QueuedCommand const& command)
{
    Spell* spell = self->GetCurrentSpell(CURRENT_GENERIC_SPELL);
    if (!spell)
        spell = self->GetCurrentSpell(CURRENT_CHANNELED_SPELL);
    if (!spell && self->SpellQueue.empty())
        return {"stop_cast", false, "not casting"};
    std::uint32_t const spellId = spell ? spell->GetSpellInfo()->Id : 0;
    // The client's CMSG_CANCEL_CAST: a counter, then the spell it is cancelling.
    WorldPacket packet(CMSG_CANCEL_CAST, 1 + 4);
    packet << uint8(0) << uint32(spellId);
    _stopping = std::make_pair(slot, command.actionId);
    self->GetSession()->HandleCancelCastOpcode(packet);
    _stopping.reset();
    bool const stopped = !self->IsNonMeleeSpellCast(false, false, true) && self->SpellQueue.empty();
    return {"stop_cast", stopped, spellId ? "spell " + std::to_string(spellId) : std::string("the spell queue")};
}

Control::Part Control::Select(std::size_t slot, Player* self, Player* enemy, bool clear)
{
    Part part{clear ? "clear_target" : "target_enemy", false, ""};
    if (!clear && !Visible(self, enemy))
    {
        part.detail = "no visible enemy";
        return part;
    }
    ObjectGuid const guid = clear ? ObjectGuid::Empty : enemy->GetGUID();
    WorldPacket packet(CMSG_SET_SELECTION, 8);
    packet << guid;
    self->GetSession()->HandleSetSelectionOpcode(packet);
    _slots[slot].target = self->GetTarget().GetRawValue();
    part.ok = self->GetTarget() == guid;
    part.detail = part.ok ? "" : "the core kept the old selection";
    return part;
}

Control::Part Control::Attack(std::size_t slot, Player* self, Player* enemy, bool on)
{
    Part part{on ? "attack_on" : "attack_off", false, ""};
    if (on)
    {
        if (!Visible(self, enemy))
        {
            part.detail = "no visible enemy";
            return part;
        }
        WorldPacket packet(CMSG_ATTACKSWING, 8);
        packet << enemy->GetGUID();
        self->GetSession()->HandleAttackSwingOpcode(packet);
        part.ok = self->GetVictim() == enemy;
        part.detail = part.ok ? "" : "not a valid attack target";
    }
    else
    {
        WorldPacket packet(CMSG_ATTACKSTOP, 0);
        self->GetSession()->HandleAttackStopOpcode(packet);
        part.ok = !self->GetVictim();
    }
    _slots[slot].victim = GuidOf(self->GetVictim());
    return part;
}

Control::Part Control::Face(std::size_t slot, Player* self, Player* enemy, Facing facing)
{
    Part part{std::string("face ") + std::string(FacingName(facing)), false, ""};
    if (!self->IsAlive())
        part.detail = "dead";
    else if (self->HasUnitState(UNIT_STATE_LOST_CONTROL))
        part.detail = "cannot turn without control";
    else if (!Visible(self, enemy))
        part.detail = "no visible enemy";
    else
    {
        float angle = self->GetAngle(enemy);
        if (facing == Facing::Away)
            angle = Position::NormalizeOrientation(angle + kPi);
        // As a client turning in place: the orientation changes now and an orientation-only spline shows it.
        self->SetOrientation(angle);
        self->SetFacingTo(angle);
        _slots[slot].spline = self->movespline->GetId();
        part.ok = true;
    }
    return part;
}

Control::Part Control::Move(std::size_t slot, Player* self, Movement movement)
{
    Part part{"move " + std::string(MovementName(movement)), false, ""};
    if (!self->IsAlive())
        part.detail = "dead";
    else if (self->HasUnitState(UNIT_STATE_STUNNED))
        part.detail = "stunned";
    else if (self->HasUnitState(UNIT_STATE_ROOT) || self->IsRooted())
        part.detail = "rooted";
    else if (self->HasUnitState(UNIT_STATE_CONFUSED | UNIT_STATE_FLEEING | UNIT_STATE_DISTRACTED))
        part.detail = "without control";
    else if (self->IsInFlight() || self->GetVehicle() || !self->GetTransGUID().IsEmpty())
        part.detail = "cannot move from here";
    else
    {
        // StopMoving first brings the position up to where the current spline has the actor now.
        if (!self->movespline->Finalized())
            self->StopMoving();
        float const facing = self->GetOrientation();
        float const speed = self->GetSpeed(IsBackward(movement) ? MOVE_RUN_BACK : MOVE_RUN);
        Position destination = self->GetPosition();
        self->MovePositionToFirstCollision(destination, speed * float(kMoveHorizonMs) / 1000.0f, MovementAngle(movement));
        float const distance = self->GetExactDist2d(destination.GetPositionX(), destination.GetPositionY());
        if (distance < kBlockedYards)
            part.detail = "blocked";
        else
        {
            // The actor keeps its facing while it walks, strafes or backs off, as a client does.
            ::Movement::MoveSplineInit init(self);
            init.MoveTo(destination.GetPositionX(), destination.GetPositionY(), destination.GetPositionZ(), false, true);
            init.SetOrientationFixed(true);
            init.SetFacing(facing);
            init.SetVelocity(speed);
            if (init.Launch() > 0)
            {
                _slots[slot].spline = self->movespline->GetId();
                part.ok = true;
                std::ostringstream detail;
                detail.precision(2);
                detail << std::fixed << distance << " yd at " << speed << " yd/s";
                part.detail = detail.str();
            }
            else
                part.detail = "the core refused the movement";
        }
    }
    return part;
}

void Control::Observe(std::size_t slot, Player* player)
{
    Slot& s = _slots[slot];
    bool const owned = s.epoch != 0;
    std::string const producer = owned ? "stock" : "stock (released)";
    auto foreign = [&](std::string const& kind, std::string const& detail, std::uint32_t& counter)
    {
        if (owned)
            ++counter;
        Record({0, 0, slot, producer, kind, 0, "", "", 0, 0, 0, "observed", detail});
    };
    std::uint32_t releasedCounter = 0;
    std::uint32_t& moves = owned ? s.counters.stockMoves : releasedCounter;
    std::uint32_t const spline = player->movespline->GetId();
    if (!player->movespline->Finalized() && spline != s.spline && spline != s.seenSpline)
    {
        s.seenSpline = spline;
        foreign("move", "a movement spline the executor did not launch", moves);
    }
    std::uint8_t const generator = std::uint8_t(player->GetMotionMaster()->GetCurrentMovementGeneratorType());
    if (generator != IDLE_MOTION_TYPE && generator != s.seenGenerator)
        foreign("move", "motion generator " + std::to_string(generator), moves);
    s.seenGenerator = generator;

    std::uint64_t const target = player->GetTarget().GetRawValue();
    if (target != s.target)
    {
        if (target)
            foreign("target", "selection changed to " + ObjectGuid(target).ToString(), owned ? s.counters.stockTargets : releasedCounter);
        else
            Record({0, 0, slot, "core", "target", 0, "", "", 0, 0, 0, "observed", "selection cleared (teleport or the core's rules)"});
        s.target = target;
    }
    std::uint64_t const victim = GuidOf(player->GetVictim());
    if (victim != s.victim)
    {
        if (victim)
            foreign("attack", "auto-attack started on " + ObjectGuid(victim).ToString(), owned ? s.counters.stockAttacks : releasedCounter);
        else
            Record({0, 0, slot, "core", "attack", 0, "", "", 0, 0, 0, "observed", "auto-attack ended by the core's rules"});
        s.victim = victim;
    }
    bool const pet = player->GetPet() || player->GetGuardianPet();
    if (pet && !s.petSeen)
        foreign("pet", "a pet or guardian appeared (pets are unsupported)", owned ? s.counters.pets : releasedCounter);
    s.petSeen = pet;
}

void Control::OnPacketSent(WorldSession* session, WorldPacket const& packet)
{
    Player* player = session ? session->GetPlayer() : nullptr;
    if (!player)
        return;
    std::uint16_t const opcode = packet.GetOpcode();
    if (opcode != SMSG_CAST_FAILED && opcode != SMSG_SPELL_START && opcode != SMSG_SPELL_GO && opcode != SMSG_SPELL_FAILURE)
        return;
    std::uint64_t const guid = player->GetGUID().GetRawValue();
    std::size_t slot = kScenarioActors;
    for (std::size_t i = 0; i < kScenarioActors; ++i)
        if (_slots[i].guid && _slots[i].guid == guid)
            slot = i;
    if (slot == kScenarioActors)
        return;
    // What this actor's client was told is also what it may perceive (the observer filters by sight).
    if (PerceivedKind(opcode))
        Perceive(slot, packet);
    if (opcode == SMSG_SPELL_FAILURE)
        return;
    Slot& s = _slots[slot];
    WorldPacket p(packet);
    p.rpos(0);
    std::uint8_t castCount = 0;
    std::uint32_t spellId = 0;
    if (opcode == SMSG_CAST_FAILED)
    {
        std::uint8_t result = 0;
        p >> castCount >> spellId >> result;
        if (_scope && _scope->slot == slot && _scope->castCount == castCount && _scope->spellId == spellId)
        {
            _scope->failed = result;
            return;
        }
        auto it = std::find_if(s.casts.begin(), s.casts.end(),
            [&](InFlight const& c) { return c.castCount == castCount && c.spellId == spellId; });
        if (it != s.casts.end())
        {
            if (_stopping && _stopping->first == slot)
                Record({0, 0, slot, "executor", "cast", it->actionId, it->origin, "", 0, 0, 0, "stopped",
                    "spell " + std::to_string(spellId) + ": " + CastResultName(result) + " by stop_cast (action "
                        + std::to_string(_stopping->second) + ")"});
            else
            {
                ++s.counters.coreCancels;
                Record({0, 0, slot, "core", "cast", it->actionId, it->origin, "", 0, 0, 0,
                    result == SPELL_FAILED_INTERRUPTED ? "cancelled" : "failed",
                    "spell " + std::to_string(spellId) + ": " + CastResultName(result)
                        + (player->isMoving() ? " (the actor was moving)" : "")});
            }
            s.casts.erase(it);
        }
        return;
    }
    ObjectGuid caster;
    ObjectGuid item;
    p >> item.ReadAsPacked() >> caster.ReadAsPacked() >> castCount >> spellId;
    if (caster.GetRawValue() != guid)
        return;
    bool const inScope = _scope && _scope->slot == slot && _scope->castCount == castCount && _scope->spellId == spellId;
    if (inScope)
    {
        (opcode == SMSG_SPELL_START ? _scope->started : _scope->went) = true;
        return;
    }
    auto it = std::find_if(s.casts.begin(), s.casts.end(),
        [&](InFlight const& c) { return castCount && c.castCount == castCount && c.spellId == spellId; });
    if (it == s.casts.end())
        return;
    if (opcode == SMSG_SPELL_START)
    {
        if (!it->started)
            Record({0, 0, slot, "executor", "cast", it->actionId, it->origin, "", 0, 0, 0, "started",
                "spell " + std::to_string(spellId) + " left the core's spell queue"});
        it->started = true;
        return;
    }
    Record({0, 0, slot, "executor", "cast", it->actionId, it->origin, "", 0, 0, 0, "completed", "spell " + std::to_string(spellId)});
    s.casts.erase(it);
}

void Control::Perceive(std::size_t observer, WorldPacket const& packet)
{
    std::optional<EventKind> const kind = PerceivedKind(packet.GetOpcode());
    if (!kind)
        return;
    WorldPacket p(packet);
    p.rpos(0);
    ObjectGuid caster;
    std::uint8_t castCount = 0;
    std::uint32_t spellId = 0;
    if (*kind == EventKind::CastStopped)
        p >> caster.ReadAsPacked() >> castCount >> spellId;
    else
    {
        ObjectGuid item;
        p >> item.ReadAsPacked() >> caster.ReadAsPacked() >> castCount >> spellId;
    }
    for (std::size_t source = 0; source < kScenarioActors; ++source)
        if (_slots[source].guid && _slots[source].guid == caster.GetRawValue())
            Observer::Instance().OnSessionEvent(observer, source, *kind, spellId, Now());
}

bool Control::OnSpellCast(Player* player, Spell* spell)
{
    std::uint64_t const guid = player->GetGUID().GetRawValue();
    for (std::size_t slot = 0; slot < kScenarioActors; ++slot)
    {
        Slot& s = _slots[slot];
        if (!s.guid || s.guid != guid)
            continue;
        std::uint32_t const spellId = spell->GetSpellInfo()->Id;
        std::uint8_t const castCount = spell->m_cast_count;
        if (spell->IsTriggered())
        {
            ++s.counters.triggeredCasts;
            return false;
        }
        bool const executor = (_scope && _scope->slot == slot && _scope->castCount == castCount && _scope->spellId == spellId)
            || std::any_of(s.casts.begin(), s.casts.end(),
                [&](InFlight const& c) { return c.castCount == castCount && c.spellId == spellId; });
        if (executor)
        {
            ++s.counters.executorCasts;
            return true;
        }
        if (s.epoch)
            ++s.counters.stockCasts;
        Record({0, 0, slot, s.epoch ? "stock" : "stock (released)", "cast", 0, "", "", 0, 0, 0, "observed",
            "spell " + std::to_string(spellId) + " (" + spell->GetSpellInfo()->SpellName[0] + ") that no executor command asked for"});
        return false;
    }
    return false;
}

bool Control::SuppressesProgress(Player const* player)
{
    if (!player)
        return false;
    std::uint64_t const guid = player->GetGUID().GetRawValue();
    for (Slot& s : _slots)
        if (s.guid && s.guid == guid)
        {
            ++s.counters.suppressedProgress;
            return true;
        }
    return false;
}

void Control::OnShutdown(char const* phase)
{
    _shuttingDown = true;
    for (std::size_t i = 0; i < kScenarioActors; ++i)
    {
        RecordQueue(_queue.Drop(i, "shutdown", Now()));
        if (_slots[i].epoch)
            Record({0, 0, i, "control", "ownership", 0, "", "", 0, 0, 0, "held",
                std::string("shutdown ") + phase + ": epoch " + std::to_string(_slots[i].epoch)
                    + " stays until the actor logs out"});
    }
}

void Control::OnShutdownCancel()
{
    _shuttingDown = false;
    Record({0, 0, 0, "control", "ownership", 0, "", "", 0, 0, 0, "resumed", "shutdown cancelled"});
}

MaskRules Control::ExecutionRules() const
{
    MaskRules rules;
    rules.commandDelayMs = _queue.CommandDelayMs();
    rules.queueWindowMs = sWorld->getBoolConfig(CONFIG_SPELL_QUEUE_ENABLED) ? sWorld->getIntConfig(CONFIG_SPELL_QUEUE_WINDOW) : 0;
    return rules;
}

std::string Control::Fingerprint(Player* self) const
{
    json auras = json::array();
    for (auto const& [spellId, application] : self->GetAppliedAuras())
    {
        Aura const* aura = application->GetBase();
        auras.push_back({spellId, aura->GetCasterGUID().GetRawValue(), aura->GetStackAmount(), aura->GetCharges(),
            aura->GetDuration(), application->GetEffectMask()});
    }
    json cooldowns = json::array();
    for (auto const& [spellId, cooldown] : self->GetSpellCooldownMap())
        cooldowns.push_back({spellId, cooldown.end, cooldown.category});
    json gcd = json::array();
    for (KitAbility const& ability : _catalog->kit)
        if (SpellInfo const* info = sSpellMgr->GetSpellInfo(ability.spellId))
            gcd.push_back(self->GetGlobalCooldownMgr().HasGlobalCooldown(info));
    Spell const* current = self->GetCurrentSpell(CURRENT_GENERIC_SPELL);
    json doc = {{"health", self->GetHealth()}, {"mana", self->GetPower(POWER_MANA)},
        {"target", self->GetTarget().GetRawValue()}, {"victim", GuidOf(self->GetVictim())}, {"auras", auras},
        {"cooldowns", cooldowns}, {"gcd", gcd},
        {"spell", current ? json{current->GetSpellInfo()->Id, int(current->getState()), current->m_cast_count} : json(nullptr)},
        {"spell_queue", self->SpellQueue.size()}, {"mods_taking_spell", self->m_spellModTakingSpell != nullptr},
        {"position", {self->GetPositionX(), self->GetPositionY(), self->GetPositionZ(), self->GetOrientation()}},
        {"spline", {self->movespline->GetId(), self->movespline->Finalized()}},
        {"generator", int(self->GetMotionMaster()->GetCurrentMovementGeneratorType())}};
    return CanonicalJson(doc);
}

std::string Control::Masks(std::size_t slot, bool all)
{
    if (!_catalog || slot >= kScenarioActors)
        return "dojo control masks: no catalog or no such actor";
    std::uint64_t const now = Now();
    std::string why;
    std::optional<ActorObservation> const seen = Observer::Instance().Observe(slot, now, why);
    if (!seen)
        return "dojo control masks: actor " + std::to_string(slot) + " has no observation (" + why + ")";
    ActorKnowledge const& k = seen->knowledge;
    MaskRules const rules = Observer::Instance().Rules();
    MaskResult const r = ComputeMask(*_catalog, seen->knowledge, Observer::Instance().Rules());
    std::size_t const open = std::size_t(std::count(r.mask.begin(), r.mask.end(), std::uint8_t(1)));
    json abilities = json::object();
    for (std::size_t a = 0; a < _catalog->abilities.size(); ++a)
        abilities[_catalog->abilities[a].name] = r.abilityReasons[a];
    json facings = json::object();
    for (std::size_t f = 0; f < kFacings; ++f)
        facings[std::string(FacingName(Facing(f)))] = r.facingReasons[f];
    json kit = json::array();
    for (KitKnowledge const& s : k.kit)
        kit.push_back({{"known", s.known}, {"cooldown_ms", s.cooldownMs}, {"gcd_ms", s.gcdMs}, {"cost_known", s.costKnown},
            {"cost", s.cost}, {"range_known", s.rangeKnown}, {"max_range", s.maxRange}});
    json doc = {{"actor", slot}, {"open", open}, {"size", r.mask.size()}, {"abilities", abilities}, {"facings", facings},
        {"decision_ms", seen->decisionMs}, {"percept_ms", seen->perceptMs}, {"generation", seen->generation},
        {"wait_slack_ms", rules.perceptualDelayMs + rules.commandDelayMs + rules.queueWindowMs},
        {"knowledge", {{"alive", k.alive}, {"mana", k.mana}, {"mana_regen_bound", k.manaRegenBound}, {"casting", k.casting},
            {"cast_remaining_ms", k.castRemainingMs}, {"cast_queued", k.castQueued}, {"kit", kit},
            {"enemy_known", k.enemyKnown}, {"enemy_alive", k.enemyAlive}, {"enemy_distance", k.enemyDistance}}}};
    if (all)
    {
        std::string bits;
        for (std::uint8_t b : r.mask)
            bits += b ? '1' : '0';
        doc["mask"] = bits;
    }
    return "dojo-masks " + CanonicalJson(doc);
}

std::string Control::MaskCheck(std::size_t slot, std::uint32_t repeats)
{
    if (!_catalog || slot >= kScenarioActors)
        return "dojo control maskcheck: no catalog or no such actor";
    Player* self = Find(slot);
    if (!self || !self->IsInWorld())
        return "dojo control maskcheck: actor " + std::to_string(slot) + " is not in the world";
    repeats = std::clamp<std::uint32_t>(repeats, 1, 100000);
    Player* enemy = Enemy(slot);
    std::string const before = Fingerprint(self);
    std::string const enemyBefore = enemy && enemy->IsInWorld() ? Fingerprint(enemy) : "";
    // Each repetition reads both actors' state, makes the actor's percept and computes its mask from a copy of its
    // perception: the whole path a decision uses.
    std::vector<std::vector<std::uint8_t>> const masks = Observer::Instance().Probe(slot, Now(), repeats);
    std::vector<std::uint8_t> const first = masks.empty() ? std::vector<std::uint8_t>{} : masks.front();
    bool stable = masks.size() == repeats;
    for (std::vector<std::uint8_t> const& m : masks)
        stable = stable && m == first;
    std::string const after = Fingerprint(self);
    std::string const enemyAfter = enemy && enemy->IsInWorld() ? Fingerprint(enemy) : "";
    json doc = {{"actor", slot}, {"repeats", repeats}, {"stable", stable}, {"unchanged", before == after},
        {"enemy_unchanged", enemyBefore == enemyAfter}, {"fingerprint", Sha256Hex(before)},
        {"open", std::count(first.begin(), first.end(), std::uint8_t(1))}};
    if (before != after)
        doc["before"] = before, doc["after"] = after;
    Record({0, 0, slot, "control", "mask", 0, "", "", 0, 0, 0, before == after && stable ? "unchanged" : "CHANGED",
        std::to_string(repeats) + " mask queries"});
    return "dojo-maskcheck " + CanonicalJson(doc);
}

std::string Control::Status() const
{
    std::ostringstream out;
    out << "control: catalog " << (_catalog ? std::to_string(_catalog->entries.size()) + " actions, sha256 " + _catalog->sha256 : "none")
        << "; command delay " << _queue.CommandDelayMs() << " ms; spell queue window " << ExecutionRules().queueWindowMs
        << " ms; perceptual delay " << Observer::Instance().Config().perceptualDelayMs << " ms\n";
    json actors = json::array();
    for (std::size_t i = 0; i < kScenarioActors; ++i)
    {
        Slot const& s = _slots[i];
        Player* player = Find(i);
        PlayerbotAI* ai = player ? GET_PLAYERBOT_AI(player) : nullptr;
        Counters const& c = s.counters;
        std::optional<QueuedCommand> const pending = _queue.Pending(i);
        json state = nullptr;
        if (player && player->IsInWorld())
        {
            Spell const* current = player->GetCurrentSpell(CURRENT_GENERIC_SPELL);
            Battleground const* bg = player->GetBattleground();
            state = {{"map", player->GetMapId()}, {"instance", player->GetInstanceId()},
                {"position", {player->GetPositionX(), player->GetPositionY(), player->GetPositionZ(), player->GetOrientation()}},
                {"alive", player->IsAlive()}, {"health", player->GetHealth()}, {"mana", player->GetPower(POWER_MANA)},
                {"rooted", player->HasUnitState(UNIT_STATE_ROOT) || player->IsRooted()},
                {"moving", !player->movespline->Finalized()}, {"casting", current ? current->GetSpellInfo()->Id : 0},
                {"spell_queue", player->SpellQueue.size()}, {"target", player->GetTarget().GetRawValue()},
                {"victim", GuidOf(player->GetVictim())}, {"in_combat", player->IsInCombat()},
                {"bg_status", bg ? int(bg->GetStatus()) : 0}, {"pet", player->GetPet() != nullptr}};
        }
        actors.push_back({{"actor", i}, {"registered", s.guid != 0}, {"online", player != nullptr}, {"owned", s.epoch != 0},
            {"epoch", s.epoch}, {"wanted", s.wanted},
            {"ai_owner", ai ? ai->GetExternalControlOwner() : ""}, {"ai_epoch", ai ? ai->GetExternalControlEpoch() : 0},
            {"skipped_updates", ai ? ai->GetExternalControlSkippedUpdates() : 0},
            {"refused_inputs", ai ? ai->GetExternalControlRefusedInputs() : 0},
            {"pending", pending ? json{{"action_id", pending->actionId}, {"action", _catalog->entries[pending->action].name},
                {"due_ms", pending->dueMs}} : json(nullptr)},
            {"casts_in_flight", s.casts.size()}, {"state", state},
            {"counters", {{"commands", c.commands}, {"executed", c.executed}, {"rejected", c.rejected},
                {"superseded", c.superseded}, {"kept", c.kept}, {"dropped", c.dropped}, {"executor_casts", c.executorCasts},
                {"triggered_casts", c.triggeredCasts}, {"stock_casts", c.stockCasts}, {"core_cancels", c.coreCancels},
                {"stock_moves", c.stockMoves}, {"stock_targets", c.stockTargets}, {"stock_attacks", c.stockAttacks},
                {"pets", c.pets}, {"acquisitions", c.acquisitions}, {"releases", c.releases}, {"losses", c.losses},
                {"suppressed_progress", c.suppressedProgress}}}});
    }
    out << "dojo-control " << CanonicalJson(json{{"now_ms", Now()}, {"trace_seq", _seq}, {"generation", _generation},
        {"first_action_id", _firstActionId}, {"next_action_id", _nextActionId}, {"actors", actors}});
    return out.str();
}

std::string Control::Trace(std::size_t last) const
{
    std::string out;
    std::size_t const from = _trace.size() > last ? _trace.size() - last : 0;
    for (std::size_t i = from; i < _trace.size(); ++i)
        out += "dojo-trace " + CanonicalJson(ToJson(_trace[i])) + "\n";
    return out + "dojo control trace: " + std::to_string(_trace.size() - from) + " of " + std::to_string(_trace.size()) + " records";
}

std::string Control::CatalogText() const
{
    if (!_catalog)
        return "dojo control actions: no catalog";
    return "dojo-catalog " + _catalog->canonical + "\nsha256 " + _catalog->sha256 + ", " + std::to_string(_catalog->entries.size())
        + " actions; entry 0 is noop, then <ability>/<movement>/<facing>";
}

std::string Control::Place(std::size_t slot, float x, float y, float z, float o)
{
    Player* self = Find(slot);
    if (!self || !self->IsInWorld() || !Owns(slot))
        return "dojo harness place: actor " + std::to_string(slot) + " is not owned and in the world";
    if (!State().manifest || self->GetMapId() != State().manifest->arena.mapId || !self->InBattleground())
        return "dojo harness place: actor " + std::to_string(slot) + " is not in the training arena";
    self->NearTeleportTo(x, y, z, o);
    std::ostringstream detail;
    detail << "near teleport to " << x << " " << y << " " << z << " " << o;
    Record({0, 0, slot, "harness", "place", 0, "", "", 0, 0, 0, "executed", detail.str()});
    return "dojo harness place: " + detail.str() + " at " + std::to_string(Now());
}

std::string Control::SetMana(std::size_t slot, std::uint32_t mana)
{
    Player* self = Find(slot);
    if (!self || !self->IsInWorld() || !Owns(slot))
        return "dojo harness mana: actor " + std::to_string(slot) + " is not owned and in the world";
    self->SetPower(POWER_MANA, std::min(mana, self->GetMaxPower(POWER_MANA)));
    Record({0, 0, slot, "harness", "mana", 0, "", "", 0, 0, 0, "executed", "mana set to " + std::to_string(self->GetPower(POWER_MANA))});
    return "dojo harness mana: " + std::to_string(self->GetPower(POWER_MANA));
}

std::string Control::Restore(std::size_t slot)
{
    Player* self = Find(slot);
    if (!self || !self->IsInWorld() || !Owns(slot))
        return "dojo harness restore: actor " + std::to_string(slot) + " is not owned and in the world";
    RecordQueue(_queue.Drop(slot, "harness restore", Now()));
    self->InterruptNonMeleeSpells(false);
    self->SpellQueue.clear();
    self->AttackStop();
    self->StopMoving();
    std::uint32_t const removed = RestoreBaseline(self);
    for (KitAbility const& ability : _catalog->kit)
        if (SpellInfo const* info = sSpellMgr->GetSpellInfo(ability.spellId))
            self->GetGlobalCooldownMgr().CancelGlobalCooldown(info);
    Slot& s = _slots[slot];
    s.casts.clear();
    s.spline = self->movespline->GetId();
    s.victim = 0;
    Record({0, 0, slot, "harness", "restore", 0, "", "", 0, 0, 0, "executed",
        "full health, mana and durability, no cooldowns, " + std::to_string(removed) + " transient auras removed"});
    return "dojo harness restore: actor " + std::to_string(slot) + " restored";
}

std::string Control::Kill(std::size_t slot)
{
    Player* self = Find(slot);
    if (!self || !self->IsInWorld() || !Owns(slot))
        return "dojo harness kill: actor " + std::to_string(slot) + " is not owned and in the world";
    if (!State().manifest || self->GetMapId() != State().manifest->arena.mapId || !self->InBattleground())
        return "dojo harness kill: actor " + std::to_string(slot) + " is not in the training arena";
    if (!self->IsAlive())
        return "dojo harness kill: actor " + std::to_string(slot) + " is already dead";
    // A death by its own hand: no killer, so no kill credit or honor for anyone; no durability loss.
    self->KillSelf(false);
    Record({0, 0, slot, "harness", "kill", 0, "", "", 0, 0, 0, "executed", self->IsAlive() ? "still alive" : "dead"});
    return std::string("dojo harness kill: actor ") + std::to_string(slot) + (self->IsAlive() ? " is still alive" : " is dead");
}

std::string Control::ClearCooldowns(std::size_t slot)
{
    Player* self = Find(slot);
    if (!self || !self->IsInWorld() || !Owns(slot))
        return "dojo harness cooldowns: actor " + std::to_string(slot) + " is not owned and in the world";
    std::size_t const before = self->GetSpellCooldownMap().size();
    self->RemoveAllSpellCooldown();
    for (KitAbility const& ability : _catalog->kit)
        if (SpellInfo const* info = sSpellMgr->GetSpellInfo(ability.spellId))
            self->GetGlobalCooldownMgr().CancelGlobalCooldown(info);
    std::uint64_t const at = Now();
    Record({0, 0, slot, "harness", "cooldowns", 0, "", "", 0, 0, 0, "executed",
        std::to_string(before) + " cooldowns and the global cooldown cleared at " + std::to_string(at)});
    return "dojo-harness " + CanonicalJson(json{{"change", "cooldowns"}, {"actor", slot}, {"at_ms", at}, {"cleared", before}});
}

std::uint32_t Control::RestoreBaseline(Player* player)
{
    std::vector<std::uint32_t> transient;
    for (auto const& [spellId, application] : player->GetAppliedAuras())
        if (!application->GetBase()->GetSpellInfo()->IsPassive() && !IsTeamMarker(spellId))
            transient.push_back(spellId);
    for (std::uint32_t const spellId : transient)
        player->RemoveAurasDueToSpell(spellId);
    player->RemoveAllSpellCooldown();
    player->DurabilityRepairAll(false, 0.0f, false);
    player->SetFullHealth();
    player->SetPower(POWER_MANA, player->GetMaxPower(POWER_MANA));
    return std::uint32_t(transient.size());
}

std::string Control::LineOfSight(float x1, float y1, float z1, float x2, float y2, float z2) const
{
    Player* self = Find(0);
    if (!self || !self->IsInWorld())
        self = Find(1);
    if (!self || !self->IsInWorld() || !self->GetMap())
        return "dojo harness los: no actor in the world";
    Map* map = self->GetMap();
    uint32 const phase = self->GetPhaseMask();
    bool const los = map->isInLineOfSight(x1, y1, z1 + kEyeHeight, x2, y2, z2 + kEyeHeight, phase, LINEOFSIGHT_ALL_CHECKS,
        VMAP::ModelIgnoreFlags::Nothing);
    json doc = {{"map", map->GetId()}, {"los", los}, {"ground", {map->GetHeight(phase, x1, y1, z1 + kEyeHeight),
        map->GetHeight(phase, x2, y2, z2 + kEyeHeight)}}};
    return "dojo-los " + CanonicalJson(doc);
}
}

#endif
