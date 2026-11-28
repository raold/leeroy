// This file is part of leeroy, released under the GNU GPL v2. See LICENSE.
#include "DojoQueue.h"
#if DOJO_TRAINING_BUILD && defined(DOJO_OFFLINE_ACCEPTANCE) && DOJO_OFFLINE_ACCEPTANCE
#include "DojoRaid.h"
#include "DojoRuntime.h"
#include "AccountMgr.h"
#include "BattleGroundJoinAction.h"
#include "BattlegroundMgr.h"
#include "CharacterCache.h"
#include "Chat.h"
#include "CommandScript.h"
#include "Event.h"
#include "Group.h"
#include "GroupMgr.h"
#include "ObjectAccessor.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "PlayerbotAIConfig.h"
#include "Playerbots.h"
#include "RandomPlayerbotMgr.h"
#include "WorldScript.h"
#include "LeeroyIntegrations.h"
#include "Guild/OfflineRaidJob.h"
#include <algorithm>
#include <array>
#include <mutex>
#include <unordered_set>

using nlohmann::json;
using namespace Acore::ChatCommands;
namespace
{
// Existing offline-acceptance actors only; never accept a character GUID.
constexpr std::array<char const*, 2> Names{"Dojotank", "Dojodpsa"};
struct QueueTrial
{
    std::array<ObjectGuid, 2> actors{};
    std::recursive_mutex mutex;
    std::string token, state = "idle", failure;
    uint32 elapsed = 0, phaseMs = 0, cadence = 0, attempts = 0, samples = 0;
    uint32 queuedSamples = 0, maxGap = 0;
    bool active = false, cleanup = false, controlQueued = false, controlLeft = false;
    bool oldJoin = false, pinsChanged = false;
    ObjectGuid expectedGroup;
    json cases = json::array();
    json failureEvidence = json::object();

    Player* Actor(std::size_t i) const
    {
        auto* p = ObjectAccessor::FindConnectedPlayer(actors[i]);
        return p && p->IsInWorld() && !p->IsDuringRemoveFromWorld() && GET_PLAYERBOT_AI(p) ? p : nullptr;
    }
    void Phase(std::string const& next)
    {
        state = next; phaseMs = cadence = attempts = samples = queuedSamples = 0;
        expectedGroup = Actor(0) && Actor(0)->GetGroup() ? Actor(0)->GetGroup()->GetGUID() : ObjectGuid::Empty;
    }
    void Fail(char const* code)
    {
        if (failure.empty())
        {
            failure = code;
            failureEvidence = {{"phase", state}, {"phase_ms", phaseMs}, {"attempts", attempts},
                {"samples", samples}, {"queued_samples", queuedSamples}};
        }
        Phase("cleanup");
    }
    std::string Start(std::string const& id)
    {
        std::lock_guard<std::recursive_mutex> lock(mutex);
        if (active || state == "cleanup_failed") return "fixture_busy";
        if (Dojo::State().plan.state != Dojo::RuntimeState::Armed || Dojo::State().manifest ||
            Leeroy::Guild::DojoOfflineSnapshot().value("active", false)) return "fixture_not_isolated";
        if (id.empty() || id.size() > 64 || id.find_first_not_of("abcdefghijklmnopqrstuvwxyz0123456789-") != std::string::npos)
            return "invalid_token";
        uint32 const account = AccountMgr::GetId("RNDBOTDOJO");
        if (!account) return "prepare_offline_fixture_first";
        for (std::size_t i = 0; i < actors.size(); ++i)
        {
            auto guid = sCharacterCache->GetCharacterGuidByName(Names[i]);
            if (!guid || sCharacterCache->GetCharacterAccountIdByGuid(guid) != account ||
                ObjectAccessor::FindConnectedPlayer(guid) || sRandomPlayerbotMgr.IsPinned(guid.GetCounter()))
                return "fixture_not_clean";
            actors[i] = guid;
        }
        token = id; failure.clear(); cases = json::array(); elapsed = maxGap = 0;
        failureEvidence = json::object();
        cleanup = controlQueued = controlLeft = pinsChanged = false;
        oldJoin = sPlayerbotAIConfig.RandomBotJoinBG;
        sPlayerbotAIConfig.RandomBotJoinBG = true;
        active = true;
        for (auto guid : actors) Dojo::LoginRaidFixture(guid);
        Phase("login");
        return "started";
    }
    bool Party(bool raid)
    {
        auto* bot = Actor(0); auto* peer = Actor(1);
        if (!bot || !peer || bot->GetGroup() || peer->GetGroup()) return false;
        auto* group = new Group();
        if (!group->Create(bot)) { delete group; return false; }
        sGroupMgr->AddGroup(group);
        if (raid) group->ConvertToRaid();
        if (!group->AddMember(peer)) { group->Disband(); return false; }
        return true;
    }
    void Pins(std::initializer_list<ObjectGuid::LowType> guids)
    {
        pinsChanged = true;
        if (sRandomPlayerbotMgr.SetPinnedBots(std::unordered_set<ObjectGuid::LowType>(guids), true, false) != guids.size())
            Fail("pin_admission_failed");
    }
    void Attempt(Player* bot)
    {
        auto* ai = GET_PLAYERBOT_AI(bot);
        ai->GetAiObjectContext()->GetValue<uint32>("bg type")->Set(BATTLEGROUND_QUEUE_WS);
        // Exercise the actual action after trigger selection. The verdict uses
        // core queue membership, not this action's return value.
        BGJoinAction action(ai);
        action.Execute(Event());
        ++attempts;
    }
    void Leave(Player* bot)
    {
        if (!bot->InBattlegroundQueue() && !bot->InBattleground()) return;
        auto* ai = GET_PLAYERBOT_AI(bot);
        ai->GetAiObjectContext()->GetValue<uint32>("bg type")->Set(BATTLEGROUND_QUEUE_WS);
        BGLeaveAction action(ai);
        action.Execute(Event());
    }
    void Update(uint32 diff)
    {
        std::lock_guard<std::recursive_mutex> lock(mutex);
        if (!active) return;
        elapsed += diff; phaseMs += diff; cadence += diff;
        if (elapsed > 240000 && state != "cleanup" && state != "logout") Fail("queue_trial_deadline");
        auto* bot = Actor(0); auto* peer = Actor(1);
        if (state == "login")
        {
            if (!bot || !peer) return;
            for (auto* p : {bot, peer})
            {
                if (p->GetGroup() || !p->IsAlive() || p->GetLevel() != 80 || p->InBattlegroundQueue())
                { Fail("queue_fixture_unclean"); return; }
                GET_PLAYERBOT_AI(p)->SetMaster(nullptr);
                GET_PLAYERBOT_AI(p)->ChangeStrategy("+stay", BOT_STATE_NON_COMBAT);
                GET_PLAYERBOT_AI(p)->ChangeStrategy("-bg", BOT_STATE_NON_COMBAT);
                if (!p->TeleportTo(0, -8949.95f, -132.493f, 83.5312f, 0.f))
                { Fail("queue_fixture_teleport"); return; }
            }
            Phase("arrive");
        }
        else if (state == "arrive")
        {
            if (bot && peer && !bot->IsBeingTeleported() && !peer->IsBeingTeleported() &&
                bot->GetMapId() == 0 && peer->GetMapId() == 0) Phase("positive");
        }
        else if (state == "positive")
        {
            if (!bot || !peer || !bot->IsAlive() || bot->IsInCombat()) { Fail("positive_fixture_lost"); return; }
            if (bot->InBattlegroundQueueForBattlegroundQueueType(BATTLEGROUND_QUEUE_WS))
            { controlQueued = true; Phase("leave_positive"); return; }
            if (cadence >= 1000) { cadence = 0; Attempt(bot); }
            if (phaseMs >= 20000) Fail("positive_queue_not_observed");
        }
        else if (state == "leave_positive")
        {
            if (!bot || !peer) { Fail("positive_fixture_lost"); return; }
            if (bot->InBattleground()) { Fail("unexpected_battleground_start"); return; }
            if (cadence >= 500) { cadence = 0; Leave(bot); }
            if (!bot->InBattlegroundQueue())
            {
                controlLeft = true;
                GET_PLAYERBOT_AI(bot)->ChangeStrategy("+stay,-bg", BOT_STATE_NON_COMBAT);
                if (!Party(true)) { Fail("raid_creation_failed"); return; }
                Phase("raid");
            }
            else if (phaseMs >= 20000) Fail("positive_queue_not_cleared");
        }
        else if (state == "raid" || state == "pinned_solo" || state == "pinned_party" || state == "dungeon")
        {
            if (!bot || !peer || !bot->IsAlive() || !peer->IsAlive() || bot->IsBeingTeleported())
            { Fail("protected_fixture_lost"); return; }
            auto const group = bot->GetGroup() ? bot->GetGroup()->GetGUID() : ObjectGuid::Empty;
            if (group != expectedGroup) { Fail("protected_group_changed"); return; }
            if ((state == "raid" && (!bot->GetGroup() || !bot->GetGroup()->isRaidGroup() ||
                    peer->GetGroup() != bot->GetGroup() || bot->GetGroup()->GetMembersCount() != 2)) ||
                (state == "pinned_solo" && !sRandomPlayerbotMgr.IsPinned(actors[0].GetCounter())) ||
                (state == "pinned_party" && (!bot->GetGroup() || peer->GetGroup() != bot->GetGroup() ||
                    !sRandomPlayerbotMgr.IsPinned(actors[1].GetCounter()))) ||
                (state == "dungeon" && (bot->GetMapId() != 36 || !bot->GetInstanceId() || bot->GetGroup())))
            { Fail("protection_context_lost"); return; }
            ++samples; maxGap = std::max(maxGap, diff);
            if (bot->InBattlegroundQueue() || peer->InBattlegroundQueue()) ++queuedSamples;
            if (queuedSamples || bot->InBattleground() || peer->InBattleground()) { Fail("protected_body_queued"); return; }
            // Allow the final queued opcode a full second to settle before
            // judging the context or changing groups/pins.
            if (cadence >= 500 && phaseMs < 9000) { cadence = 0; Attempt(bot); }
            if (phaseMs < 10000) return;
            cases.push_back({{"case", state}, {"attempts", attempts}, {"samples", samples},
                {"queued_samples", queuedSamples}, {"map", bot->GetMapId()}, {"instance", bot->GetInstanceId()},
                {"group", group.GetCounter()}, {"pinned_self", sRandomPlayerbotMgr.IsPinned(actors[0].GetCounter())},
                {"pinned_peer", sRandomPlayerbotMgr.IsPinned(actors[1].GetCounter())}});
            if (state == "raid")
            {
                bot->GetGroup()->Disband(); Pins({actors[0].GetCounter()});
                if (failure.empty()) Phase("pinned_solo");
            }
            else if (state == "pinned_solo")
            {
                Pins({actors[1].GetCounter()});
                if (!failure.empty()) return;
                if (!Party(false)) { Fail("party_creation_failed"); return; }
                Phase("pinned_party");
            }
            else if (state == "pinned_party")
            {
                bot->GetGroup()->Disband(); Pins({});
                if (!failure.empty()) return;
                if (!bot->TeleportTo(36, -16.f, -383.f, 61.f, 0.f)) { Fail("dungeon_entry_refused"); return; }
                Phase("dungeon_arrive");
            }
            else Phase("cleanup");
        }
        else if (state == "dungeon_arrive")
        {
            if (bot && !bot->IsBeingTeleported() && bot->GetMapId() == 36 && bot->GetInstanceId()) Phase("dungeon");
            else if (phaseMs >= 30000) Fail("dungeon_entry_missing");
        }
        else if (state == "cleanup")
        {
            sPlayerbotAIConfig.RandomBotJoinBG = oldJoin;
            if (pinsChanged) { LeeroyApplyPins(); pinsChanged = false; }
            bool waiting = false;
            for (auto* p : {bot, peer}) if (p)
            {
                if (p->InBattlegroundQueue() || p->InBattleground()) { Leave(p); waiting = true; continue; }
                p->CombatStop(true);
                if (p->GetGroup()) p->GetGroup()->Disband();
                if (!p->IsAlive()) { p->ResurrectPlayer(1.f); p->SpawnCorpseBones(); }
                if (!p->IsBeingTeleported() && p->GetMapId() != 0)
                    p->TeleportTo(0, -8949.95f, -132.493f, 83.5312f, 0.f);
                if (p->IsBeingTeleported() || p->GetMapId() != 0) waiting = true;
                else Dojo::LogoutRaidFixture(p->GetGUID());
            }
            if (!waiting) Phase("logout");
            else if (phaseMs >= 60000) { active = false; state = "cleanup_failed"; }
        }
        else if (state == "logout")
        {
            if (!ObjectAccessor::FindConnectedPlayer(actors[0]) && !ObjectAccessor::FindConnectedPlayer(actors[1]))
            { cleanup = true; active = false; state = "done"; }
            else if (phaseMs >= 60000) { active = false; state = "cleanup_failed"; }
        }
    }
    json Status()
    {
        std::lock_guard<std::recursive_mutex> lock(mutex);
        return {{"kind", "dojo-queue-observation"}, {"format", 1}, {"token", token}, {"state", state},
            {"failure", failure}, {"elapsed_ms", elapsed}, {"max_gap_ms", maxGap}, {"cases", cases},
            {"failure_evidence", failureEvidence}, {"attempts", attempts}, {"samples", samples},
            {"control_queued", controlQueued}, {"control_left", controlLeft}, {"cleanup", cleanup}};
    }
};
QueueTrial trial;
class Commands : public CommandScript
{
public:
    Commands() : CommandScript("DojoQueueCommands") { }
    ChatCommandTable GetCommands() const override
    {
        static ChatCommandTable commands{{"start", Start, SEC_CONSOLE, Console::Yes},
            {"status", Status, SEC_CONSOLE, Console::Yes}, {"abort", Abort, SEC_CONSOLE, Console::Yes}};
        static ChatCommandTable dojo{{"queue", commands}};
        return {{"dojo", dojo}};
    }
    static bool Start(ChatHandler* h, Tail args) { h->SendSysMessage("dojo queue: " + trial.Start(std::string(args))); return true; }
    static bool Status(ChatHandler* h) { h->SendSysMessage("dojo-queue " + trial.Status().dump()); return true; }
    static bool Abort(ChatHandler* h)
    {
        std::lock_guard<std::recursive_mutex> lock(trial.mutex);
        if (trial.active) trial.Fail("aborted");
        h->SendSysMessage("dojo queue: cleanup requested"); return true;
    }
};
class Ticks : public WorldScript
{
public:
    Ticks() : WorldScript("DojoQueueTicks", {WORLDHOOK_ON_UPDATE}) { }
    void OnUpdate(uint32 diff) override { trial.Update(diff); }
};
}
void AddSC_dojo_queue() { new Commands(); new Ticks(); }
#else
void AddSC_dojo_queue() { }
#endif
