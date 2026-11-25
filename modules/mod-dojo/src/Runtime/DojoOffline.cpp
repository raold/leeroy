// This file is part of leeroy, released under the GNU GPL v2. See LICENSE.
#include "DojoOffline.h"
#if DOJO_TRAINING_BUILD
#if defined(DOJO_OFFLINE_ACCEPTANCE) && DOJO_OFFLINE_ACCEPTANCE
#include "DojoRaid.h"
#include "DojoRuntime.h"
#include "AccountMgr.h"
#include "CharacterCache.h"
#include "Chat.h"
#include "CommandScript.h"
#include "Config.h"
#include "Creature.h"
#include "Guild.h"
#include "GuildMgr.h"
#include "InstanceScript.h"
#include "Map.h"
#include "MotionMaster.h"
#include "ObjectAccessor.h"
#include "ObjectMgr.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "PlayerbotFactory.h"
#include "Playerbots.h"
#include "UnitScript.h"
#include "World.h"
#include "WorldScript.h"
#include "WorldSession.h"
#include "LeeroyModule.h"
#include "Guild/OfflineRaidJob.h"
#include "Util/DcBotProvisioning.h"
#include "Util/DcProvisionBudget.h"
#include <algorithm>
#include <array>
#include <map>
#include <mutex>
#include <sstream>

using nlohmann::json;
using namespace Acore::ChatCommands;
namespace
{
struct Fixture { char const* name; uint8 cls; char const* spec; };
// Fixed synthetic identities; the command never accepts a caller-supplied GUID.
constexpr std::array<Fixture, 13> Fixtures{{
    {"Dojomaster", CLASS_WARRIOR, "arms"},
    {"Dojotank", CLASS_PALADIN, "prot"}, {"Dojoguard", CLASS_WARRIOR, "prot"},
    {"Dojoheala", CLASS_PALADIN, "holy"}, {"Dojohealb", CLASS_PRIEST, "holy"},
    {"Dojohealc", CLASS_SHAMAN, "resto"}, {"Dojodpsa", CLASS_MAGE, "fire"},
    {"Dojodpsb", CLASS_WARLOCK, "affliction"}, {"Dojodpsc", CLASS_HUNTER, "marks"},
    {"Dojodpsd", CLASS_ROGUE, "combat"}, {"Dojodpse", CLASS_WARRIOR, "fury"},
    {"Dojobench", CLASS_PALADIN, "holy"}, {"Dojobencha", CLASS_MAGE, "fire"}}};
constexpr char const* GuildName = "Dojo Acceptance";

struct OfflineFixture
{
    std::array<ObjectGuid, Fixtures.size()> actors{};
    std::string state = "idle", error, token;
    uint32 elapsed = 0, guildId = 0, samples = 0;
    std::size_t rolled = 0;
    uint32 map = 0;
    std::map<uint32, std::pair<uint64, uint64>> hits;
    std::recursive_mutex mutex;

    bool Armed() const { return Dojo::State().plan.state == Dojo::RuntimeState::Armed && !Dojo::State().manifest; }
    Player* Actor(std::size_t i) const
    {
        auto* p = ObjectAccessor::FindConnectedPlayer(actors[i]);
        return p && p->IsInWorld() && GET_PLAYERBOT_AI(p) ? p : nullptr;
    }
    std::string Identify(bool create)
    {
        for (std::size_t i = 0; i < actors.size(); ++i)
        {
            uint32 const account = AccountMgr::GetId(i ? "RNDBOTDOJO" : "DOJORAID");
            if (!account) return "fixture accounts DOJORAID and RNDBOTDOJO are required";
            ObjectGuid guid = sCharacterCache->GetCharacterGuidByName(Fixtures[i].name);
            if (!guid && create)
            {
                auto* session = new WorldSession(account, std::string(), 0, nullptr, SEC_PLAYER,
                    EXPANSION_WRATH_OF_THE_LICH_KING, time_t(0), sWorld->GetDefaultDbcLocale(), 0, false, false, 0);
                uint8 const race = Fixtures[i].cls == CLASS_SHAMAN ? RACE_DRAENEI :
                    Fixtures[i].cls == CLASS_HUNTER ? RACE_DWARF : RACE_HUMAN;
                CharacterCreateInfo info(Fixtures[i].name, race, Fixtures[i].cls, GENDER_MALE, 0, 0, 0, 0, 0);
                auto* p = new Player(session);
                p->GetMotionMaster()->Initialize();
                if (p->Create(sObjectMgr->GetGenerator<HighGuid::Player>().Generate(), &info))
                {
                    p->setCinematic(2); p->SetAtLoginFlag(AT_LOGIN_NONE); p->SaveToDB(true, false);
                    guid = p->GetGUID();
                    sCharacterCache->AddCharacterCacheEntry(guid, account, p->GetName(), p->getGender(),
                        p->getRace(), p->getClass(), p->GetLevel());
                }
                p->CleanupsBeforeDelete(); delete p; delete session;
            }
            if (!guid || sCharacterCache->GetCharacterAccountIdByGuid(guid) != account)
                return "fixture missing or belongs to another account";
            actors[i] = guid;
        }
        return "";
    }
    std::string Prepare()
    {
        if (!Armed() || LeeroyModule::Instance().Enabled()) return "prepare requires an armed realm with Leeroy disabled";
        if (state != "idle" && state != "prepared") return "fixture is busy";
        auto why = Identify(true);
        if (!why.empty()) return why;
        for (auto guid : actors) if (ObjectAccessor::FindConnectedPlayer(guid)) return "fixture already online";
        error.clear(); elapsed = 0; rolled = 0; state = "preparing";
        for (auto guid : actors) Dojo::LoginRaidFixture(guid);
        return "started";
    }
    void Update(uint32 diff)
    {
        std::lock_guard<std::recursive_mutex> lock(mutex);
        if (state == "preparing")
        {
            elapsed += diff;
            if (elapsed > 300000) { error = "fixture preparation timeout"; state = "failed"; return; }
            if (rolled < actors.size())
            {
                auto* p = Actor(rolled);
                if (!p || p->IsBeingTeleported()) return;
                if (p->getClass() != Fixtures[rolled].cls) { error = "fixture class mismatch"; state = "failed"; return; }
                if (p->GetGroup()) { error = "fixture has an existing group"; state = "failed"; return; }
                std::string picked;
                int const spec = DcBotProvisioning::ResolveSpecNo(Fixtures[rolled].cls,
                    Fixtures[rolled].spec, Fixtures[rolled].spec, &picked);
                if (spec < 0) { error = "missing premade spec for fixture slot " + std::to_string(rolled); state = "failed"; return; }
                if (!DcProvisionBudget::Take()) return;
                DcBotProvisioning::Roll(p, 80, ITEM_QUALITY_EPIC,
                    PlayerbotFactory::CalcMixedGearScore(245, ITEM_QUALITY_EPIC), spec);
                p->CombatStop(true);
                p->TeleportTo(0, -8833.0f + float(rolled), 626.f, 94.f, 0.f);
                p->SaveToDB(false, false);
                ++rolled;
                return;
            }
            for (std::size_t i = 0; i < actors.size(); ++i)
                if (!Actor(i) || Actor(i)->IsBeingTeleported()) return;
            auto* guild = sGuildMgr->GetGuildByName(GuildName);
            if (!guild)
            {
                guild = new Guild();
                if (!guild->Create(Actor(0), GuildName)) { delete guild; error = "fixture guild creation failed"; state = "failed"; return; }
                sGuildMgr->AddGuild(guild);
            }
            if (guild->GetLeaderGUID() != actors[0]) { error = "fixture guild belongs to another owner"; state = "failed"; return; }
            guildId = guild->GetId();
            for (std::size_t i = 1; i < actors.size(); ++i)
            {
                auto* p = Actor(i);
                if (p->GetGuildId() && p->GetGuildId() != guildId) { error = "fixture belongs to another guild"; state = "failed"; return; }
                if (!p->GetGuildId() && !guild->AddMember(actors[i])) { error = "fixture guild admission failed"; state = "failed"; return; }
                p->SaveToDB(false, false);
            }
            for (auto guid : actors) Dojo::LogoutRaidFixture(guid);
            state = "logging_out"; elapsed = 0;
        }
        else if (state == "logging_out")
        {
            elapsed += diff;
            for (auto guid : actors)
                if (ObjectAccessor::FindConnectedPlayer(guid))
                { if (elapsed > 60000) { error = "fixture logout timeout"; state = "failed"; } return; }
            state = "prepared";
        }
        else if (state == "running") ++samples;
    }
    std::string Start(std::string const& scenario, std::string const& id)
    {
        if (!Armed() || !LeeroyModule::Instance().Enabled()) return "offline fixture needs an enabled isolated guild";
        if (state != "idle" && state != "prepared") return "fixture is busy";
        if (id.empty() || id.size() > 64 || id.find_first_not_of("abcdefghijklmnopqrstuvwxyz0123456789-") != std::string::npos)
            return "invalid run token";
        auto why = Identify(false);
        if (!why.empty()) return why;
        auto* guild = sGuildMgr->GetGuildByName(GuildName);
        if (!guild || guild->GetLeaderGUID() != actors[0] || LeeroyModule::Instance().GuildId() != guild->GetId()
            || LeeroyModule::Instance().HumanGuid() != actors[0].GetCounter() || ObjectAccessor::FindConnectedPlayer(actors[0]))
            return "isolated guild/owner identity mismatch";
        guildId = guild->GetId();
        bool const reserveTrial = scenario == "toc-reserves";
        map = scenario == "toc" || reserveTrial ? 649 : scenario == "ulduar" ? 603 : scenario == "icc" ? 631 : 0;
        if (!map) return "unknown offline scenario";
        std::vector<std::size_t> requested{1, 2, 3, 4, 5, 6, 7, 8, 9};
        requested.push_back(reserveTrial ? 11 : 10);
        for (auto i : requested)
        {
            if (!Actor(i)) { Dojo::LoginRaidFixture(actors[i]); return "actors_pending"; }
            if (Actor(i)->GetGuildId() != guildId) return "actor guild mismatch";
        }
        Leeroy::Guild::OfflineRaidCmd cmd;
        cmd.mapId = map; cmd.size = 10; cmd.leaderGuid = actors[1].GetCounter();
        cmd.lootMasterGuid = cmd.leaderGuid; cmd.minMembers = 10;
        cmd.maxWipes = cmd.maxWipesPerBoss = 3; cmd.timeoutMin = 45;
        for (std::size_t i = 2; i <= (reserveTrial ? 9 : 10); ++i) cmd.members.push_back(actors[i].GetCounter());
        if (reserveTrial) cmd.reserves.push_back(actors[11].GetCounter());
        cmd.assistants.push_back(actors[2].GetCounter());
        auto result = Leeroy::Guild::StartRaidJob(id, cmd, Leeroy::Guild::RaidJobMode::Offline);
        if (!result.ok) return result.error;
        std::lock_guard<std::recursive_mutex> lock(mutex);
        token = id; state = "running"; error.clear(); samples = 0; hits.clear();
        return "started";
    }
    json Status()
    {
        std::lock_guard<std::recursive_mutex> lock(mutex);
        json bodies = json::array(), damage = json::array();
        for (std::size_t i = 1; i < actors.size(); ++i)
            if (auto* p = Actor(i))
            {
                json bosses = json::array(), auras = json::array();
                for (uint32 spell : {66823u, 67618u, 67619u, 67620u, 66830u, 66869u})
                    if (p->HasAura(spell)) auras.push_back(spell);
                if (auto* script = p->GetInstanceScript())
                    for (uint32 b = 0; b < script->GetEncounterCount(); ++b) bosses.push_back(uint32(script->GetBossState(b)));
                bodies.push_back({{"slot", i}, {"guid", actors[i].GetCounter()}, {"alive", p->IsAlive()},
                    {"map", p->GetMapId()}, {"instance", p->GetInstanceId()}, {"health", p->GetHealth()},
                    {"x", p->GetPositionX()}, {"y", p->GetPositionY()}, {"z", p->GetPositionZ()},
                    {"combat", p->IsInCombat()}, {"bosses", bosses}, {"auras", auras},
                    {"speed_run", p->GetSpeed(MOVE_RUN)}, {"tank", PlayerbotAI::IsTank(p)},
                    {"grouped", p->GetGroup() != nullptr}, {"owned", Leeroy::Guild::OfflineRaidOwns(actors[i].GetCounter())},
                    {"master", GET_PLAYERBOT_AI(p)->GetMaster() ? GET_PLAYERBOT_AI(p)->GetMaster()->GetGUID().GetCounter() : 0}});
            }
        for (auto const& [entry, value] : hits) damage.push_back({{"entry", entry}, {"hits", value.first}, {"requested", value.second}});
        return {{"kind", "dojo-offline-observation"}, {"format", 1}, {"state", state}, {"error", error},
            {"token", token}, {"guild", guildId}, {"owner", actors[0].GetCounter()}, {"samples", samples},
            {"native", Leeroy::Guild::DojoOfflineSnapshot()}, {"bodies", bodies}, {"damage", damage}};
    }
};
OfflineFixture fixture;
class Commands : public CommandScript
{
public:
    Commands() : CommandScript("DojoOfflineCommands") { }
    ChatCommandTable GetCommands() const override
    {
        static ChatCommandTable commands{{"prepare", Prepare, SEC_CONSOLE, Console::Yes},
            {"start", Start, SEC_CONSOLE, Console::Yes}, {"status", Status, SEC_CONSOLE, Console::Yes},
            {"stop", Stop, SEC_CONSOLE, Console::Yes}};
        static ChatCommandTable dojo{{"offline", commands}};
        return {{"dojo", dojo}};
    }
    static bool Prepare(ChatHandler* h) { h->SendSysMessage("dojo offline: " + fixture.Prepare()); return true; }
    static bool Start(ChatHandler* h, Tail args)
    {
        std::istringstream in{std::string(args)};
        std::string scenario, token, extra;
        in >> scenario >> token >> extra;
        h->SendSysMessage("dojo offline: " + (extra.empty() ? fixture.Start(scenario, token) : "invalid arguments"));
        return true;
    }
    static bool Status(ChatHandler* h) { h->SendSysMessage("dojo-offline " + fixture.Status().dump()); return true; }
    static bool Stop(ChatHandler* h)
    { h->SendSysMessage(Leeroy::Guild::DojoStopOffline(fixture.token) ? "dojo offline: stopping" : "dojo offline: no owned run"); return true; }
};
class Ticks : public WorldScript
{
public:
    Ticks() : WorldScript("DojoOfflineTicks", {WORLDHOOK_ON_UPDATE}) { }
    void OnUpdate(uint32 diff) override { fixture.Update(diff); }
};
class Damage : public UnitScript
{
public:
    Damage() : UnitScript("DojoOfflineDamage", true, {UNITHOOK_ON_DAMAGE}) { }
    void OnDamage(Unit* attacker, Unit* victim, uint32& amount) override
    {
        std::lock_guard<std::recursive_mutex> lock(fixture.mutex);
        if (fixture.state != "running" || !attacker || !victim || !amount) return;
        if (std::find(fixture.actors.begin() + 1, fixture.actors.end(), victim->GetGUID()) == fixture.actors.end()) return;
        auto& counter = fixture.hits[attacker->GetEntry()];
        ++counter.first; counter.second += amount;
    }
};
}
void AddSC_dojo_offline() { new Commands(); new Ticks(); new Damage(); }
#else
void AddSC_dojo_offline() { }
#endif
#else
void AddSC_dojo_offline() { }
#endif
