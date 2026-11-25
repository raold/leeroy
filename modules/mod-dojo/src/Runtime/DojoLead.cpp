// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.
#include "DojoLead.h"
#if DOJO_TRAINING_BUILD
#if defined(DOJO_OFFLINE_ACCEPTANCE) && DOJO_OFFLINE_ACCEPTANCE
#include "DojoHuman.h"
#include "DojoRaid.h"
#include "DojoRuntime.h"
#include "AccountMgr.h"
#include "CharacterCache.h"
#include "Chat.h"
#include "CommandScript.h"
#include "Group.h"
#include "GroupMgr.h"
#include "Guild.h"
#include "GuildMgr.h"
#include "ObjectAccessor.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "Playerbots.h"
#include "WorldScript.h"
#include "LeeroyCommands.h"
#include "LeeroyModule.h"
#include <algorithm>
#include <array>

using nlohmann::json;
using namespace Acore::ChatCommands;
namespace
{
constexpr std::array<char const*, 2> Names{"Dojotank", "Dojodpsa"};
Position const StartPoint(5707.31f, 612.214f, 646.791f, 0.f); // Actual Eventide fountain ground.
struct Trial
{
    std::array<ObjectGuid, 2> actors{};
    ObjectGuid group;
    std::string token, state = "idle", failure;
    uint32 elapsed = 0, phaseMs = 0, samples = 0, followed = 0, waitMs = 0, maxGap = 0;
    float travelled = 0.f, followerTravelled = 0.f, resumed = 0.f;
    Position pausedAt;
    std::array<Position, 2> previous;
    bool active = false, issued = false, waited = false, restored = false, cleanup = false, ready = false, refusedOff = false;

    Player* Actor(std::size_t i) const
    {
        auto* p = ObjectAccessor::FindConnectedPlayer(actors[i]);
        return p && p->IsInWorld() && GET_PLAYERBOT_AI(p) ? p : nullptr;
    }
    void Phase(std::string const& next) { state = next; phaseMs = 0; }
    void Fail(std::string const& why) { failure = why; Phase("cleanup"); }
    std::string Command(bool end)
    {
        json message{{"v", 1}, {"type", "lead_do"}, {"id", token + (end ? "-end" : "-start")},
            {"action", end ? "end" : "start"}, {"guid", actors[0].GetCounter()}};
        if (!end) { message["activity"] = "travel"; message["spot"] = "dal_bank"; }
        auto command = Leeroy::DecodeCommand(message.dump());
        if (!command.command) return "lead_fixture_command_invalid";
        auto reply = json::parse(LeeroyExecuteCommand(*command.command), nullptr, false);
        if (!reply.is_object()) return "lead_fixture_ack_missing";
        return reply.value("ok", false) ? std::string{} : reply.value("error", "lead_fixture_ack_invalid");
    }
    std::string Start(std::string const& id)
    {
        if (active || state == "cleanup_failed") return "fixture_busy";
        if (Dojo::State().plan.state != Dojo::RuntimeState::Armed || Dojo::State().manifest)
            return "fixture_not_armed";
        if (id.empty() || id.size() > 64 || id.find_first_not_of("abcdefghijklmnopqrstuvwxyz0123456789-") != std::string::npos)
            return "invalid_token";
        auto* guild = sGuildMgr->GetGuildByName("Dojo Acceptance");
        ObjectGuid const human = sCharacterCache->GetCharacterGuidByName("Dojomaster");
        auto& module = LeeroyModule::Instance();
        if (!guild || !human || guild->GetLeaderGUID() != human || !module.Enabled() ||
            module.GuildId() != guild->GetId() || module.HumanGuid() != human.GetCounter())
            return "fixture_guild_identity_mismatch";
        uint32 const account = AccountMgr::GetId("RNDBOTDOJO");
        for (std::size_t i = 0; i < actors.size(); ++i)
        {
            actors[i] = sCharacterCache->GetCharacterGuidByName(Names[i]);
            if (!account || !actors[i] || sCharacterCache->GetCharacterAccountIdByGuid(actors[i]) != account)
                return "fixture_account_mismatch";
            if (auto* p = ObjectAccessor::FindConnectedPlayer(actors[i]); p && p->GetGroup()) return "fixture_group_busy";
        }
        auto why = Dojo::BeginHumanFixture(id);
        if (!why.empty()) return why;
        token = id; failure.clear(); group.Clear();
        elapsed = samples = followed = waitMs = maxGap = 0;
        travelled = followerTravelled = resumed = 0.f;
        issued = waited = restored = cleanup = ready = refusedOff = false; active = true;
        for (auto guid : actors) Dojo::LoginRaidFixture(guid);
        Phase("login");
        return "started";
    }
    void Update(uint32 diff)
    {
        if (!active) return;
        elapsed += diff; phaseMs += diff;
        if (elapsed > 180000 && state != "cleanup" && state != "logout") Fail("lead_fixture_deadline");
        auto* human = Dojo::HumanFixture(token);
        auto* leader = Actor(0);
        auto* follower = Actor(1);
        if (state == "login")
        {
            auto why = Dojo::HumanFixtureError(token);
            if (!why.empty()) { Fail(why); return; }
            if (!human || !leader || !follower) return;
            for (auto* p : {human, leader, follower})
                if (p->GetGroup() || p->GetLevel() != 80 || !p->IsAlive()) { Fail("unclean_lead_fixture"); return; }
            auto* party = new Group();
            if (!party->Create(human)) { delete party; Fail("fixture_group_creation"); return; }
            sGroupMgr->AddGroup(party);
            group = party->GetGUID();
            for (auto* p : {leader, follower})
            {
                if (!party->AddMember(p)) { Fail("fixture_group_join"); return; }
                auto* ai = GET_PLAYERBOT_AI(p);
                ai->SetMaster(human);
                ai->ChangeStrategy("-stay,-passive,+follow", BOT_STATE_NON_COMBAT);
            }
            for (auto* p : {human, leader, follower})
                if (!p->TeleportTo(571, StartPoint.GetPositionX(), StartPoint.GetPositionY(), StartPoint.GetPositionZ(), 0.f))
                { Fail("fixture_teleport_refused"); return; }
            Phase("arrive");
        }
        else if (state == "arrive")
        {
            if (!human || !leader || !follower || human->IsBeingTeleported() || leader->IsBeingTeleported() ||
                follower->IsBeingTeleported()) return;
            if (human->GetMapId() != 571 || leader->GetMap() != human->GetMap() || follower->GetMap() != human->GetMap())
            { Fail("fixture_destination_mismatch"); return; }
            for (auto* p : {leader, follower}) GET_PLAYERBOT_AI(p)->SetMaster(human);
            ready = true;
            auto why = Command(false);
            if (!why.empty())
            {
                refusedOff = why == "off";
                restored = GET_PLAYERBOT_AI(leader)->GetMaster() == human && GET_PLAYERBOT_AI(follower)->GetMaster() == human;
                Fail(why); return;
            }
            issued = true;
            previous = {leader->GetPosition(), follower->GetPosition()};
            Phase("observe");
        }
        else if (state == "observe")
        {
            if (!human || !leader || !follower || !leader->IsAlive() || !follower->IsAlive() ||
                GET_PLAYERBOT_AI(human) || human->GetMapId() != 571 || leader->GetMap() != human->GetMap() ||
                follower->GetMap() != human->GetMap() || leader->IsBeingTeleported() || follower->IsBeingTeleported())
            { Fail("lead_fixture_body_changed"); return; }
            ++samples;
            maxGap = std::max(maxGap, diff);
            for (std::size_t i = 0; i < actors.size(); ++i)
            {
                auto* p = Actor(i);
                float const step = p->GetExactDist2d(previous[i]);
                if (step > 1.f + p->GetSpeed(MOVE_RUN) * float(diff) * 0.0015f)
                { Fail("measured_actor_jumped"); return; }
                previous[i] = p->GetPosition();
            }
            if (GET_PLAYERBOT_AI(leader)->GetMaster() != leader || GET_PLAYERBOT_AI(follower)->GetMaster() != leader)
            { Fail("lead_master_relationship_lost"); return; }
            travelled = std::max(travelled, leader->GetExactDist2d(StartPoint));
            followerTravelled = std::max(followerTravelled, follower->GetExactDist2d(StartPoint));
            if (followerTravelled >= 10.f && follower->GetExactDist2d(leader) <= 15.f) ++followed;
            if (!waited)
            {
                float const distance = leader->GetExactDist2d(human);
                if (distance > 75.f) { Fail("lead_did_not_wait_for_human"); return; }
                if (distance > 40.f && !leader->isMoving() && travelled >= 20.f)
                {
                    waitMs += diff;
                    if (waitMs >= 3000)
                    {
                        waited = true; pausedAt = leader->GetPosition();
                        // The scripted human catches up. The measured leader and
                        // follower must move themselves through ordinary AI.
                        human->NearTeleportTo(leader->GetPositionX(), leader->GetPositionY() + 1.f,
                            leader->GetPositionZ(), 0.f);
                    }
                }
                else waitMs = 0;
            }
            else
            {
                resumed = std::max(resumed, leader->GetExactDist2d(pausedAt));
                if (resumed >= 10.f && followed >= 10)
                {
                    auto why = Command(true);
                    issued = false;
                    if (!why.empty()) { Fail(why); return; }
                    restored = GET_PLAYERBOT_AI(leader)->GetMaster() == human && GET_PLAYERBOT_AI(follower)->GetMaster() == human;
                    if (!restored) { Fail("lead_did_not_restore_masters"); return; }
                    Phase("cleanup");
                }
            }
        }
        else if (state == "cleanup")
        {
            if (issued) { Command(true); issued = false; }
            for (auto* p : {leader, follower}) if (p) GET_PLAYERBOT_AI(p)->SetMaster(nullptr);
            for (auto* p : {human, leader, follower})
                if (p && p->GetGroup() && p->GetGroup()->GetGUID() == group) { p->GetGroup()->Disband(); break; }
            for (auto guid : actors) Dojo::LogoutRaidFixture(guid);
            if (!Dojo::EndHumanFixture(token)) { failure += "; human_cleanup_failed"; }
            Phase("logout");
        }
        else if (state == "logout")
        {
            bool online = false;
            for (auto guid : actors) online = online || ObjectAccessor::FindConnectedPlayer(guid) != nullptr;
            if (!online) { cleanup = true; active = false; Phase("done"); }
            else if (phaseMs >= 60000) { failure += "; bot_cleanup_failed"; active = false; Phase("cleanup_failed"); }
        }
    }
    json Status() const
    {
        return {{"kind", "dojo-lead-observation"}, {"format", 1}, {"token", token}, {"state", state},
            {"failure", failure}, {"samples", samples}, {"elapsed_ms", elapsed}, {"leader_distance", travelled},
            {"max_gap_ms", maxGap},
            {"follower_distance", followerTravelled}, {"follow_samples", followed}, {"wait_ms", waitMs},
            {"waited", waited}, {"resumed_distance", resumed}, {"restored", restored}, {"cleanup", cleanup},
            {"ready", ready}, {"refused_off", refusedOff}};
    }
};
Trial trial;
class Commands : public CommandScript
{
public:
    Commands() : CommandScript("DojoLeadCommands") { }
    ChatCommandTable GetCommands() const override
    {
        static ChatCommandTable commands{{"start", Start, SEC_CONSOLE, Console::Yes},
            {"status", Status, SEC_CONSOLE, Console::Yes}, {"abort", Abort, SEC_CONSOLE, Console::Yes}};
        static ChatCommandTable dojo{{"lead", commands}};
        return {{"dojo", dojo}};
    }
    static bool Start(ChatHandler* h, Tail args) { h->SendSysMessage("dojo lead: " + trial.Start(std::string(args))); return true; }
    static bool Status(ChatHandler* h) { h->SendSysMessage("dojo-lead " + trial.Status().dump()); return true; }
    static bool Abort(ChatHandler* h) { if (trial.active) trial.Fail("aborted"); h->SendSysMessage("dojo lead: cleanup requested"); return true; }
};
class Ticks : public WorldScript
{
public:
    Ticks() : WorldScript("DojoLeadTicks", {WORLDHOOK_ON_UPDATE}) { }
    void OnUpdate(uint32 diff) override { trial.Update(diff); }
};
}
void AddSC_dojo_lead() { new Commands(); new Ticks(); }
#else
void AddSC_dojo_lead() { }
#endif
#else
void AddSC_dojo_lead() { }
#endif
