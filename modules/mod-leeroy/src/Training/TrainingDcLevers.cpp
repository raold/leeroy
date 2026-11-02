// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "TrainingCommands.h"
#include "GuildPorts.h"
#include "OfflineRaidJob.h"
#include "LeeroyTrainingHold.h"
#include "LeeroyTrainingOverrides.h"
#include "LeeroyTrainingRuns.h"

#if !defined(LEEROY_HAS_DUNGEON_CLEAR)
#error "LEEROY_HAS_DUNGEON_CLEAR comes from mod-leeroy.cmake; re-run cmake"
#endif
#if LEEROY_HAS_DUNGEON_CLEAR
#include "AiObjectContext.h"
#include "Event.h"
#include "Group.h"
#include "Log.h"
#include "Map.h"
#include "PlayerbotAI.h"
#include "Playerbots.h"
#include "Ai/Dungeon/DungeonClear/Settings/DcSettings.h"
#include "Ai/Dungeon/DungeonClear/Util/DcLeaderSignal.h"
#include "Ai/Dungeon/DungeonClear/Util/DcRun.h"
#include "DcModuleEnable.h"
#include "TestRun/DcTestDriver.h"
#include <algorithm>
#include <chrono>
#include <map>
#include <optional>
#endif

namespace Leeroy::Training
{
#if LEEROY_HAS_DUNGEON_CLEAR
namespace
{
struct TrainingRun
{
    uint64 token = 0;
    RunScope scope;
    TrainingOverrides overrides;
    TrainingHold hold;
    std::optional<uint64> dcOffSince;
    uint64 resumeRetryAt = 0;
    bool jobOwned = false, clearing = false, holdEnabled = false, resumePending = false;
};
std::map<uint32, TrainingRun> runs; // World thread only; no player/AI pointers survive a tick.
TrainingRuns jobs; // Read-only job identities survive training off and explicit per-run override cleanup.
uint32 tickRemaining = 0;
uint64 Now()
{
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}
ObjectGuid Guid(uint32 guid) { return ObjectGuid::Create<HighGuid::Player>(guid); }

DcRunState* State(Player* leader)
{
    if (!leader || !GET_PLAYERBOT_AI(leader)) return nullptr;
    auto* value = GET_PLAYERBOT_AI(leader)->GetAiObjectContext()->GetValue<DcRunState&>(DcKey::RunState);
    return value ? &value->Get() : nullptr;
}

RunScope Scope(Player* leader)
{
    if (!leader || !leader->GetGroup()) return {};
    return {leader->GetMapId(), leader->GetInstanceId(), LeeroyModule::Instance().GuildId(), Guild::HumanGuid(),
        leader->GetGroup()->GetGUID().GetRawValue()};
}

bool SameScope(Player* leader, RunScope const& scope)
{
    return leader && leader->GetGroup() && Scope(leader) == scope
        && LeeroyModule::Instance().GuildId() == scope.guild;
}

bool SameRun(Player* leader, TrainingRun const& run) { return SameScope(leader, run.scope); }

Player* OwnedLeader(uint32 guid)
{
    if (!guid || guid == Guild::HumanGuid()) return nullptr;
    // The benchmark exception is an exact live job/group/driver/instance proof, never a general noncast permission.
    bool const benchmark = Guild::BenchmarkOwnsRun(guid);
    if (!Guild::IsCast(guid) && !benchmark) return nullptr;
    auto* leader = Guild::FindOnline(guid);
    if (!leader || (!benchmark && leader->GetGuildId() != LeeroyModule::Instance().GuildId())
        || !GET_PLAYERBOT_AI(leader) || IsSelfBot(leader) || !leader->GetGroup() || leader->IsBeingTeleported()
        || DcLeaderSignal::FindLeaderTank(leader) != leader) return nullptr;
    if (!benchmark)
        for (auto const& member : leader->GetGroup()->GetMemberSlots())
            if (member.guid.GetCounter() != Guild::HumanGuid() && !Guild::IsCast(member.guid.GetCounter())) return nullptr;
    return leader;
}

BossHold Observe(DcRunState const* state)
{
    if (!state) return {};
    return {state->paused, state->pausedDoor.IsEmpty() && !state->waitedBossGuid.IsEmpty()
        && state->pauseReason.starts_with("waiting at "), state->waitedBossGuid.GetRawValue()};
}

OverrideState Read(uint32 guid, std::string const& key)
{
    auto* def = FindDcSetting(key);
    return def ? OverrideState{DcSettings::HasOverride(Guid(guid), key.c_str()),
        DcSettings::GetEffectiveRaw(Guid(guid), *def)} : OverrideState{false, 0};
}

bool OwnedWait(uint32 guid, TrainingRun const& run)
{
    auto state = Read(guid, "WaitAtBoss");
    auto action = run.overrides.Undo("WaitAtBoss", state).action;
    return state.present && state.value != 0
        && (action == OverrideUndo::Remove || action == OverrideUndo::Restore);
}

bool RestoreKey(uint32 guid, TrainingRun& run, std::string const& key)
{
    auto plan = run.overrides.Undo(key, Read(guid, key));
    if (plan.action == OverrideUndo::Restore)
    {
        if (!DcSettings::SetOverride(Guid(guid), key, plan.value)) return false;
        auto actual = Read(guid, key);
        if (!actual.present || actual.value != plan.value) return false;
    }
    else if (plan.action == OverrideUndo::Remove)
    {
        // Never ClearRun or an empty key: other owners keep their overrides.
        DcSettings::ResetOverride(Guid(guid), key);
        if (Read(guid, key).present) return false;
    }
    run.overrides.Forget(key); // Unowned and externally replaced entries are deliberately left untouched.
    return true;
}

bool Resume(uint32 guid, TrainingRun& run, uint64 now, bool force)
{
    auto* leader = Guild::FindOnline(guid);
    if (!SameRun(leader, run)) return true; // Never act on the same leader's newer group/instance.
    auto* state = State(leader);
    auto current = Observe(state);
    if (!run.hold.ShouldResume(current, now, force)) return true;
    if (!state || !state->enabled) return true; // A disabled DC run has no live hold to release.
    if (now < run.resumeRetryAt) return false;
    run.resumeRetryAt = now + 2000; // Refused recovery must not spam an action/chat on every world tick.
    if (!DcLeaderSignal::IsDungeonClearLeader(leader)) return false;
    Player* issuer = Guild::FindOnline(run.scope.human);
    if (!issuer || !leader->GetGroup()->IsMember(issuer->GetGUID())) issuer = DcTestDriver::Get();
    if (!issuer) return false; // No driver login, DB query or forced state toggle from cleanup.
    auto* ai = GET_PLAYERBOT_AI(leader);
    bool acted = ai->DoSpecificAction("dc pause", Event("dc", "resume", issuer), true);
    state = State(leader);
    if (!acted || !state || state->paused) return false; // Followers can return true without resuming anything.
    run.hold.Resumed();
    LOG_INFO("module.leeroy", "mod-leeroy: resumed training boss hold for guid {}", guid);
    return true;
}

bool Clear(uint32 guid, TrainingRun& run)
{
    auto now = Now();
    auto* leader = Guild::FindOnline(guid);
    if (!run.clearing && SameRun(leader, run) && run.holdEnabled && OwnedWait(guid, run))
    {
        run.hold.Observe(Observe(State(leader)), now);
        run.resumePending = run.hold.ShouldResume(Observe(State(leader)), now, true);
    }
    run.clearing = true;
    bool success = true;
    if (run.resumePending)
    {
        if (Resume(guid, run, now, true)) run.resumePending = false;
        else success = false;
    }
    for (auto const& key : run.overrides.Keys()) success = RestoreKey(guid, run, key) && success;
    return success && run.overrides.Empty() && !run.resumePending;
}

TrainingRun* GetRun(Player* leader)
{
    auto guid = leader->GetGUID().GetCounter();
    auto found = runs.find(guid);
    if (found != runs.end())
    {
        if (SameRun(leader, found->second) && !found->second.clearing) return &found->second;
        if (!Clear(guid, found->second)) return nullptr;
        runs.erase(found);
    }
    if (runs.size() >= 128) return nullptr;
    TrainingRun run;
    run.scope = Scope(leader);
    auto* job = jobs.Find(guid);
    if (job && job->scope == run.scope)
    {
        run.token = job->token;
        run.jobOwned = true;
    }
    else run.token = jobs.NextToken();
    if (!run.token) return nullptr;
    return &runs.emplace(guid, std::move(run)).first->second;
}
}
#endif

std::uint64_t MarkJobRun(std::uint32_t guid)
{
#if LEEROY_HAS_DUNGEON_CLEAR
    if (!Guild::GuildEnabled() || !DcModule::IsEnabled()) return 0;
    auto* leader = OwnedLeader(guid);
    if (!leader) return 0;
    auto run = runs.find(guid);
    if (run != runs.end() && (!SameRun(leader, run->second) || run->second.clearing || run->second.jobOwned)) return 0;
    auto token = jobs.Register(guid, Scope(leader));
    if (token && run != runs.end())
    {
        run->second.token = token;
        run->second.jobOwned = true;
    }
    return token;
#else
    (void)guid;
    return 0;
#endif
}

bool ClearTrainingRun(std::uint32_t guid, std::uint64_t token)
{
#if LEEROY_HAS_DUNGEON_CLEAR
    if (Guild::BenchmarkRunning() && Guild::OfflineRaidOwns(guid) && !Guild::BenchmarkCanClearRun(guid, token))
        return false; // A matching GUID/group is insufficient after the original session was revoked.
    auto found = runs.find(guid);
    auto* job = jobs.Find(guid);
    if (!token || (job && job->token != token) || (found != runs.end() && found->second.token != token))
        return false; // Explicit stale ownership failure; no mutation of a newer receipt or override journal.
    if (found != runs.end())
    {
        if (!Clear(guid, found->second))
        {
            RequireTrainingCleanup();
            return false; // Keep BOTH receipts while restoration is still pending.
        }
        runs.erase(found);
    }
    return jobs.End(guid, token);
#else
    (void)guid;
    (void)token;
    return true;
#endif
}

LeeroyCommandResult HandleDcSetting(nlohmann::json const& message)
{
    std::string error;
    auto command = ParseDcSetting(message, error);
    if (!command) return {false, error};
#if LEEROY_HAS_DUNGEON_CLEAR
    bool undo = command->op == DcOp::Reset || command->op == DcOp::Clear;
    if (!Guild::GuildEnabled()) return {false, "guild_off"};
    if (!undo && !TrainingAllowed()) return {false, "training_off"};
    if (!DcModule::IsEnabled()) return {false, "dc_unavailable"};
    auto found = runs.find(command->leaderGuid);
    if (undo)
    {
        if (found == runs.end()) return {true, {}}; // No receipt means no ownership, even if an override exists.
        if (Guild::BenchmarkRunning() && Guild::OfflineRaidOwns(command->leaderGuid)
            && !Guild::BenchmarkCanClearRun(command->leaderGuid, found->second.token))
            return {false, "restore_ownership"};
        if (command->op == DcOp::Clear)
        {
            if (!Clear(command->leaderGuid, found->second))
            {
                RequireTrainingCleanup();
                return {false, "restore_failed"};
            }
            runs.erase(found); // Explicit settings cleanup does not end a still-live job's read-only receipt.
            return {true, {}};
        }
        auto& run = found->second;
        if (command->key == "WaitAtBoss" && run.holdEnabled && OwnedWait(command->leaderGuid, run))
        {
            auto* leader = Guild::FindOnline(command->leaderGuid);
            if (SameRun(leader, run)) run.hold.Observe(Observe(State(leader)), Now());
            if (!Resume(command->leaderGuid, run, Now(), true))
            {
                run.clearing = true;
                RequireTrainingCleanup();
                return {false, "resume_failed"};
            }
        }
        if (!RestoreKey(command->leaderGuid, run, command->key))
        {
            RequireTrainingCleanup();
            return {false, "restore_failed"};
        }
        if (command->key == "WaitAtBoss") run.holdEnabled = false;
        return {true, {}};
    }
    auto* leader = OwnedLeader(command->leaderGuid);
    if (!leader || !State(leader)) return {false, "not_leader"};
    auto* run = GetRun(leader);
    if (!run) return {false, "busy"};
    auto before = Read(command->leaderGuid, command->key);
    auto base = run->overrides.ScaleBase(command->key, before);
    if (!base) return {false, "override_changed"};
    double value = command->centi / 100.0;
    if (command->op == DcOp::Scale) value *= *base;
    if (!std::isfinite(value)) return {false, "bad_value"};
    // Prepare the journal allocation before the live mutation. The normalized value follows this registry's setter.
    auto* def = FindDcSetting(command->key);
    double normalized = std::clamp(value, def->minVal, def->maxVal);
    if (def->type != DcType::Float) normalized = std::round(normalized);
    if (command->key == "WaitAtBoss" && normalized == 0 && run->holdEnabled && OwnedWait(command->leaderGuid, *run))
    {
        run->hold.Observe(Observe(State(leader)), Now());
        if (!Resume(command->leaderGuid, *run, Now(), true)) return {false, "resume_failed"};
    }
    TrainingOverrides candidate = run->overrides;
    if (!candidate.NoteApplied(command->key, before, {true, normalized})) return {false, "override_changed"};
    if (!DcSettings::SetOverride(leader->GetGUID(), command->key, value, &error)) return {false, "refused:" + error};
    run->overrides = std::move(candidate); // Retain restoration evidence even if an unexpected readback fails.
    auto actual = Read(command->leaderGuid, command->key);
    if (!actual.present || actual.value != normalized) return {false, "apply_failed"};
    if (command->key == "WaitAtBoss")
    {
        bool const ownsNewHold = actual.value != 0 && *base == 0;
        if (!run->holdEnabled && ownsNewHold) run->hold.Enable(Observe(State(leader)));
        run->holdEnabled = ownsNewHold; // A pre-existing effective WaitAtBoss=1 belongs to its previous owner.
    }
    return {true, {}};
#else
    return {false, "dc_unavailable"};
#endif
}

void TickDcHolds(std::uint32_t diff)
{
#if LEEROY_HAS_DUNGEON_CLEAR
    if (diff < tickRemaining) { tickRemaining -= diff; return; }
    tickRemaining = 250;
    auto now = Now();
    for (auto it = runs.begin(); it != runs.end();)
    {
        auto guid = it->first;
        auto& run = it->second;
        if (Guild::BenchmarkRunning() && Guild::OfflineRaidOwns(guid) && !Guild::BenchmarkOwnsRun(guid))
        {
            ++it; // The exact-session job teardown owns recovery; generic cleanup must not adopt a replacement AI.
            continue;
        }
        try
        {
            auto* leader = Guild::FindOnline(guid);
            auto* state = SameRun(leader, run) ? State(leader) : nullptr;
            bool enabled = state && state->enabled;
            if (enabled) run.dcOffSince.reset();
            else if (!run.dcOffSince) run.dcOffSince = now;
            auto* job = jobs.Find(guid);
            bool ended = run.jobOwned && (!job || job->token != run.token);
            RunWatch watch{leader != nullptr, SameRun(leader, run), run.jobOwned, ended, enabled,
                run.dcOffSince.value_or(now)};
            bool clear = run.clearing || !TrainingAllowed() || ShouldClearRun(watch, now);
            if (!clear && run.holdEnabled)
            {
                if (!OwnedWait(guid, run)) run.holdEnabled = false;
                else
                {
                    run.hold.Observe(Observe(state), now);
                    if (run.hold.ShouldResume(Observe(state), now) && !Resume(guid, run, now, false))
                        clear = true; // Remove our future hold override; retain a failed current resume for retry.
                }
            }
            if (clear && Clear(guid, run))
            {
                LOG_INFO("module.leeroy", "mod-leeroy: cleared training overrides for guid {}", guid);
                it = runs.erase(it);
            }
            else
            {
                if (clear) RequireTrainingCleanup();
                ++it;
            }
        }
        catch (...)
        {
            run.clearing = true; // Retain receipts and retry; an engine exception cannot escape the world tick.
            RequireTrainingCleanup();
            ++it;
        }
    }
    for (auto guid : jobs.Leaders())
    {
        if (Guild::BenchmarkRunning() && Guild::OfflineRaidOwns(guid)) continue;
        if (auto* job = jobs.Find(guid); !runs.contains(guid) && !SameScope(Guild::FindOnline(guid), job->scope))
            jobs.End(guid, job->token); // Keep the receipt until old overrides have finished restoring.
    }
#else
    (void)diff;
#endif
}

bool ClearTrainingRuns()
{
#if LEEROY_HAS_DUNGEON_CLEAR
    uint32 cleared = 0;
    for (auto it = runs.begin(); it != runs.end();)
    {
        if (Guild::BenchmarkRunning() && Guild::OfflineRaidOwns(it->first) && !Guild::BenchmarkOwnsRun(it->first))
        {
            ++it;
            continue;
        }
        bool done = false;
        try { done = Clear(it->first, it->second); }
        catch (...) { it->second.clearing = true; }
        if (done) { it = runs.erase(it); ++cleared; }
        else ++it;
    }
    if (cleared || !runs.empty())
        LOG_INFO("module.leeroy", "mod-leeroy: training cleanup cleared {} runs; {} restorations retained",
            cleared, runs.size());
    return runs.empty();
#else
    return true;
#endif
}
}
