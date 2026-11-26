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
#include "Group.h"
#include "InstanceScript.h"
#include "LastMovementValue.h"
#include "Map.h"
#include "MotionMaster.h"
#include "MoveSpline.h"
#include "ObjectAccessor.h"
#include "ObjectMgr.h"
#include "Player.h"
#include "PlayerbotAI.h"
#include "PlayerbotAIConfig.h"
#include "PlayerbotFactory.h"
#include "Playerbots.h"
#include "RandomPlayerbotMgr.h"
#include "ScriptMgr.h"
#include "Spell.h"
#include "SpellInfo.h"
#include "Timer.h"
#include "UnitScript.h"
#include "World.h"
#include "WorldScript.h"
#include "WorldSession.h"
#include "LeeroyModule.h"
#include "Guild/OfflineRaidJob.h"
#include "Util/DcBotProvisioning.h"
#include "Util/DcProvisionBudget.h"
#include "Ai/Dungeon/DungeonClear/Util/DcRun.h"
#include <algorithm>
#include <array>
#include <cmath>
#include <list>
#include <map>
#include <mutex>
#include <sstream>
#include "DojoNaxxPass.h"
#include "DojoTocIntro.h"
#include "DojoStall.h"

// Older production baselines can run this harness. A worm-support trial
// refuses unless the candidate includes that feature's policy.
#if __has_include("TocWormPolicy.h")
#define DOJO_HAS_TOC_WORM_SUPPORT 1
#else
#define DOJO_HAS_TOC_WORM_SUPPORT 0
#endif

using nlohmann::json;
using namespace Acore::ChatCommands;
namespace
{
struct Fixture { char const* name; uint8 cls; char const* spec; };
// Fixed synthetic identities; the command never accepts a caller-supplied GUID.
constexpr std::array<Fixture, 13> Fixtures{{
    {"Dojomaster", CLASS_WARRIOR, "arms pve"},
    {"Dojotank", CLASS_PALADIN, "prot pve"}, {"Dojoguard", CLASS_WARRIOR, "prot pve"},
    {"Dojoheala", CLASS_PALADIN, "holy pve"}, {"Dojohealb", CLASS_PRIEST, "holy pve"},
    {"Dojohealc", CLASS_SHAMAN, "resto pve"}, {"Dojodpsa", CLASS_MAGE, "fire pve"},
    {"Dojodpsb", CLASS_WARLOCK, "affli pve"}, {"Dojodpsc", CLASS_HUNTER, "mm pve"},
    {"Dojodpsd", CLASS_ROGUE, "combat pve"}, {"Dojodpse", CLASS_WARRIOR, "fury pve"},
    {"Dojobench", CLASS_PALADIN, "holy pve"}, {"Dojobencha", CLASS_MAGE, "fire pve"}}};
constexpr char const* GuildName = "Dojo Acceptance";

uint32 MainTank(Player* player)
{
    if (auto* group = player ? player->GetGroup() : nullptr)
        for (auto const& slot : group->GetMemberSlots())
            if (slot.flags & MEMBER_FLAG_MAINTANK) return slot.guid.GetCounter();
    return 0;
}

struct OfflineFixture
{
    struct SlimePool
    {
        uint32 firstMs = 0, lastMs = 0, casts = 0;
        float radius = 0.f, minimum = 1000.f, maximum = 0.f;
        std::array<bool, 10> inside{};
    };
    std::array<ObjectGuid, Fixtures.size()> actors{};
    std::string state = "idle", error, token;
    uint32 elapsed = 0, guildId = 0, samples = 0;
    std::size_t rolled = 0;
    uint32 map = 0;
    bool scriptedWipe = false, restartingSeen = false;
    uint32 restartsAtWipe = 0;
    uint32 wipeBoss = 0, mainTankAtWipe = 0;
    bool tankSwapForced = false;
    uint32 selfResSuppressed = 0;
    std::map<uint32, std::pair<uint64, uint64>> hits;
    struct SpellDamageSample
    {
        uint64 sequence = 0;
        uint32 atMs = 0, instance = 0, entry = 0, spell = 0, slot = 0, calculated = 0, healthBefore = 0;
    };
    std::array<SpellDamageSample, 32> spellDamage{};
    uint64 spellDamageSequence = 0;
    std::map<ObjectGuid, SlimePool> slimePools;
    uint32 clockMs = 0, slimeGap = 0, slimeInside = 0, slimeEscapes = 0, slimeOutsideMs = 0;
    uint32 slimeHits = 0, slimeLateHits = 0, slimeUnknownHits = 0;
    bool wormRequested = false, wormOverride = false, wormPrevious = false, wormRestored = true;
    uint32 wormSamples = 0;
    DojoNaxxPass naxxPass;
    DojoTocIntro tocIntro;
    DojoStall stall;

    std::array<Player*, 10> RaidActors() const
    {
        std::array<Player*, 10> out{};
        for (std::size_t i = 0; i < out.size(); ++i) out[i] = Actor(i + 1);
        return out;
    }

    std::string frontier = "none";
    std::array<bool, 13> frontierHeld{};
    std::array<std::array<std::array<bool, 3>, 2>, 13> frontierStrategies{};
    uint32 frontierMs = 0, frontierHeals = 0, frontierStartHealth = 0, frontierPeakHealth = 0;
    uint32 frontierSetupMask = 0;
    uint64 frontierHealing = 0;
    ObjectGuid frontierDragon;
    std::recursive_mutex mutex;

    bool ReleaseFrontier()
    {
        bool clear = true;
        for (std::size_t i = 1; i <= 10; ++i)
            if (frontierHeld[i])
            {
                auto* p = Actor(i);
                bool restored = true;
                if (p)
                    for (std::size_t state = 0; state < 2; ++state)
                    {
                        auto* ai = GET_PLAYERBOT_AI(p);
                        auto const engine = state == 0 ? BOT_STATE_COMBAT : BOT_STATE_NON_COMBAT;
                        std::array<char const*, 3> const names{"stay", "passive", "follow"};
                        for (std::size_t flag = 0; flag < names.size(); ++flag)
                        {
                            bool const want = frontierStrategies[i][state][flag];
                            ai->ChangeStrategy(std::string(want ? "+" : "-") + names[flag], engine);
                            restored = restored && ai->HasStrategy(names[flag], engine) == want;
                        }
                    }
                frontierHeld[i] = !restored;
                clear = clear && restored;
            }
        return clear;
    }

    void PrepareFrontier(json const& native, uint32 diff)
    {
        if (frontier == "none" || frontier == "ready" || !error.empty()) return;
        if (native.value("stage", "") != "running") return;
        frontierMs += diff;
        if (frontierMs > 10000) { error = "valithria_frontier_setup_timeout"; ReleaseFrontier(); return; }
        auto* leader = Actor(1);
        auto* instance = leader ? leader->GetInstanceScript() : nullptr;
        if (!leader || leader->GetMapId() != 631 || !instance) return;
        for (std::size_t i = 1; i <= 10; ++i)
        {
            auto* p = Actor(i);
            if (!p || !p->IsAlive() || p->IsBeingTeleported() || p->GetMap() != leader->GetMap()
                || !Leeroy::Guild::OfflineRaidOwns(actors[i].GetCounter()) || !sRandomPlayerbotMgr.IsPinned(actors[i].GetCounter())) return;
            if (p->IsInCombat()) { error = "valithria_frontier_actor_in_combat"; ReleaseFrontier(); return; }
        }
        auto* ai = GET_PLAYERBOT_AI(leader);
        auto* driver = ai->GetMaster();
        if (!driver) { error = "valithria_frontier_driver_missing"; ReleaseFrontier(); return; }
        if (frontier == "pending")
        {
            // The ordinary job has already proved a fresh instance and enabled
            // its real driver. This separately named mechanic fixture now
            // selects a later encounter; it is not a full-route result.
            if (native.value("stage_ms", 0u) > 2000 || instance->GetCompletedEncounterMask() != 0)
            { error = "valithria_frontier_not_fresh"; return; }
            for (uint32 boss = 0; boss <= 10; ++boss)
                if (instance->GetBossState(boss) == DONE || instance->GetBossState(boss) == IN_PROGRESS)
                { error = "valithria_frontier_encounter_already_started"; return; }
            ai->DoSpecificAction("dc off", Event("dc", "", driver), true);
            if (DcRun::Of(ai).enabled) { error = "valithria_frontier_driver_not_stopped"; return; }
            for (std::size_t i = 1; i <= 10; ++i)
            {
                auto* memberAI = GET_PLAYERBOT_AI(Actor(i));
                for (std::size_t state = 0; state < 2; ++state)
                {
                    auto const engine = state == 0 ? BOT_STATE_COMBAT : BOT_STATE_NON_COMBAT;
                    std::array<char const*, 3> const names{"stay", "passive", "follow"};
                    for (std::size_t flag = 0; flag < names.size(); ++flag)
                        frontierStrategies[i][state][flag] = memberAI->HasStrategy(names[flag], engine);
                    memberAI->ChangeStrategy("+stay,+passive,-follow", engine);
                }
                frontierHeld[i] = true;
                Actor(i)->StopMoving();
            }
            leader->GetMap()->LoadGrid(4203.5f, 2484.f);
            auto* dragon = leader->GetMap()->GetCreature(instance->GetGuidData(10));
            if (!dragon || dragon->GetEntry() != 36789 || !dragon->IsAlive())
            { error = "valithria_frontier_dragon_missing"; ReleaseFrontier(); return; }
            frontierDragon = dragon->GetGUID();
            frontierStartHealth = frontierPeakHealth = dragon->GetHealth();
            if (!frontierStartHealth || frontierStartHealth >= dragon->GetMaxHealth())
            { error = "valithria_frontier_dragon_not_injured"; ReleaseFrontier(); return; }
            for (uint32 boss = 0; boss < 10; ++boss) instance->SetBossState(boss, DONE);
            for (uint32 boss = 0; boss < 10; ++boss)
                if (instance->GetBossState(boss) != DONE)
                { error = "valithria_frontier_prerequisite_refused"; ReleaseFrontier(); return; }
            frontierSetupMask = instance->GetCompletedEncounterMask();
            if (frontierSetupMask != 0 || instance->GetBossState(10) == DONE)
            { error = "valithria_frontier_awarded_setup_credit"; ReleaseFrontier(); return; }
            for (std::size_t i = 1; i <= 10; ++i)
                Actor(i)->NearTeleportTo(4182.f + float(i % 3) * 2.f, 2480.f + float(i / 3) * 2.f, 364.956f, 0.f);
            frontier = "arriving"; frontierMs = 0;
        }
        else if (frontierMs >= 1000)
        {
            for (std::size_t i = 1; i <= 10; ++i)
                if (std::abs(Actor(i)->GetPositionZ() - 364.956f) > 8.f
                    || std::hypot(Actor(i)->GetPositionX() - 4182.f, Actor(i)->GetPositionY() - 2484.f) > 30.f) return;
            if (!ReleaseFrontier()) { error = "valithria_frontier_strategies_not_restored"; return; }
            ai->DoSpecificAction("dc on", Event("dc", "", driver), true);
            if (!DcRun::Of(ai).enabled) { error = "valithria_frontier_driver_not_resumed"; return; }
            frontier = "ready"; frontierMs = 0;
        }
    }

    void RestoreWormSupport()
    {
#if DOJO_HAS_TOC_WORM_SUPPORT
        if (wormOverride)
        {
            sPlayerbotAIConfig.TocWormSupport = wormPrevious;
            wormOverride = false;
            wormRestored = sPlayerbotAIConfig.TocWormSupport == wormPrevious;
        }
#endif
    }

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
        for (std::size_t i = 0; i < Fixtures.size(); ++i)
            if (DcBotProvisioning::ResolveSpecNo(Fixtures[i].cls, Fixtures[i].spec, Fixtures[i].spec, nullptr) < 0)
                return "missing premade spec for fixture slot " + std::to_string(i);
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
        else if (state == "running")
        {
            ++samples;
            clockMs += diff;
            auto const native = Leeroy::Guild::DojoOfflineSnapshot();
            naxxPass.Tick(RaidActors(), native, token, error, diff);
            tocIntro.Tick(RaidActors(), native, token, error, diff);
            stall.Tick(RaidActors(), native, error, diff);
            if (!native.value("active", false)) { RestoreWormSupport(); ReleaseFrontier(); }
            PrepareFrontier(native, diff);
            if (frontier == "ready")
            {
                frontierMs += diff;
                if (auto* leader = Actor(1))
                    if (auto* dragon = leader->GetMap()->GetCreature(frontierDragon))
                        frontierPeakHealth = std::max(frontierPeakHealth, dragon->GetHealth());
            }
#if DOJO_HAS_TOC_WORM_SUPPORT
            if (wormRequested && native.value("stage", "") == "running" && sPlayerbotAIConfig.TocWormSupport)
                ++wormSamples;
#endif
            if (native.value("active", false) && native.value("stage", "") != "tearing_down")
                for (auto const& body : native.value("bodies", json::array()))
                    if (!sRandomPlayerbotMgr.IsPinned(body.value("guid", uint32(0))))
                        error = "offline fixture lost its ordinary cast pin";
            if (scriptedWipe)
            {
                if (native.value("active", false) && native.value("stage", "") == "restarting") restartingSeen = true;
            }
            if (!slimePools.empty() && map == 649 && native.value("stage", "") == "running")
            {
                slimeGap = std::max(slimeGap, diff);
                auto* leader = Actor(1);
                bool present = leader != nullptr, anyPool = false, allOutside = true;
                for (auto& [guid, pool] : slimePools)
                {
                    auto* creature = leader ? leader->GetMap()->GetCreature(guid) : nullptr;
                    if (!creature || !creature->IsAlive() || clockMs - pool.lastMs > 1500) continue;
                    anyPool = true;
                    for (std::size_t i = 1; i <= 10; ++i)
                    {
                        auto* p = Actor(i);
                        if (!p || !p->IsAlive() || p->IsBeingTeleported() || p->GetMap() != creature->GetMap()
                            || !Leeroy::Guild::OfflineRaidOwns(actors[i].GetCounter()))
                        { present = false; continue; }
                        bool const inside = p->GetExactDist2d(creature) <= pool.radius + 0.5f;
                        if (inside) ++slimeInside;
                        if (pool.inside[i - 1] && !inside) ++slimeEscapes;
                        pool.inside[i - 1] = inside;
                        allOutside = allOutside && !inside;
                    }
                }
                slimeOutsideMs = anyPool && present && allOutside ? slimeOutsideMs + diff : 0;
            }
        }
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
        bool const fullRun = scenario == "toc-full" || scenario == "toc-full-worms" || scenario == "ulduar-full";
        bool const wormTrial = scenario == "toc-worms" || scenario == "toc-full-worms";
        bool const naxxTrial = scenario == "naxx-gothik-pass" || scenario == "naxx-gothik-pass-control";
        bool const tocIntroTrial = scenario == "toc-anub-intro";
        bool const stallTrial = scenario == "offline-native-stall";
        bool const valithriaTrial = scenario == "icc-valithria-frontier";
        if (wormTrial && !DOJO_HAS_TOC_WORM_SUPPORT) return "toc_worm_feature_missing_from_candidate";
        map = scenario == "toc" || scenario == "toc-full" || reserveTrial || wormTrial || tocIntroTrial || stallTrial ? 649
            : scenario == "ulduar" || scenario == "ulduar-full" ? 603 : scenario == "icc" || valithriaTrial ? 631 : naxxTrial ? 533 : 0;
        if (!map) return "unknown offline scenario";
        std::vector<std::size_t> requested{1, 2, 3, 4, 5, 6, 7, 8, 9};
        requested.push_back(reserveTrial ? 11 : 10);
        for (auto i : requested)
        {
            if (!Actor(i)) { Dojo::LoginRaidFixture(actors[i]); return "actors_pending"; }
            if (Actor(i)->GetGuildId() != guildId) return "actor guild mismatch";
            if (!sRandomPlayerbotMgr.IsPinned(actors[i].GetCounter())) return "fixture actor is not pinned";
        }
        Leeroy::Guild::OfflineRaidCmd cmd;
        if (stallTrial && !DojoStall::Configure(cmd)) return "native_stall_candidate_missing";
        cmd.mapId = map; cmd.size = 10; cmd.leaderGuid = actors[1].GetCounter();
        cmd.lootMasterGuid = cmd.leaderGuid; cmd.minMembers = 10;
        cmd.maxWipes = cmd.maxWipesPerBoss = 3; cmd.timeoutMin = fullRun ? 180 : 45;
        for (std::size_t i = 2; i <= (reserveTrial ? 9 : 10); ++i) cmd.members.push_back(actors[i].GetCounter());
        if (reserveTrial) cmd.reserves.push_back(actors[11].GetCounter());
        cmd.assistants.push_back(actors[2].GetCounter());
        auto result = Leeroy::Guild::StartRaidJob(id, cmd, Leeroy::Guild::RaidJobMode::Offline);
        if (!result.ok) return result.error;
        if (naxxTrial && !Leeroy::Guild::DojoConfigureNaxxPass(id, scenario == "naxx-gothik-pass-control"))
        {
            Leeroy::Guild::DojoStopOffline(id);
            return "naxx_pass_candidate_missing";
        }
        std::lock_guard<std::recursive_mutex> lock(mutex);
        token = id; state = "running"; error.clear(); samples = 0; hits.clear();
        spellDamage = {}; spellDamageSequence = 0;
        naxxPass = {};
        naxxPass.state = naxxTrial ? "pending" : "none";
        naxxPass.control = scenario == "naxx-gothik-pass-control";
        tocIntro = {};
        tocIntro.state = tocIntroTrial ? "pending" : "none";
        stall = {};
        stall.state = stallTrial ? "pending" : "none";
        scriptedWipe = restartingSeen = false; restartsAtWipe = wipeBoss = mainTankAtWipe = 0;
        tankSwapForced = false;
        selfResSuppressed = 0;
        slimePools.clear(); clockMs = slimeGap = slimeInside = slimeEscapes = slimeOutsideMs = 0;
        slimeHits = slimeLateHits = slimeUnknownHits = 0;
        wormRequested = wormTrial; wormSamples = 0; wormRestored = !wormTrial;
        frontier = valithriaTrial ? "pending" : "none"; frontierHeld = {}; frontierStrategies = {};
        frontierMs = frontierHeals = frontierStartHealth = frontierPeakHealth = frontierSetupMask = 0;
        frontierHealing = 0; frontierDragon.Clear();
#if DOJO_HAS_TOC_WORM_SUPPORT
        if (wormTrial)
        {
            wormPrevious = sPlayerbotAIConfig.TocWormSupport;
            sPlayerbotAIConfig.TocWormSupport = true;
            wormOverride = true;
        }
#endif
        return "started";
    }
    std::string Wipe(std::string const& id, bool swappedTank = false, bool forceTankSwap = false)
    {
        std::lock_guard<std::recursive_mutex> lock(mutex);
        if (!Armed() || state != "running" || id != token || scriptedWipe || map != 603) return "wipe_not_admitted";
        auto const native = Leeroy::Guild::DojoOfflineSnapshot();
        if (!native.value("active", false) || native.value("stage", "") != "running") return "wipe_not_running";
        auto* leader = Actor(1);
        auto* instance = leader ? leader->GetInstanceScript() : nullptr;
        uint32 const encounter = swappedTank ? 2 : 3; // Actual Razorscale or XT-002.
        auto* boss = instance ? instance->GetCreature(encounter) : nullptr;
        if (!instance || instance->GetBossState(encounter) != IN_PROGRESS || !boss || !boss->IsAlive() || !boss->IsInCombat())
            return "wipe_boss_not_engaged";
        uint32 tank = MainTank(leader);
        if (swappedTank && !forceTankSwap && (!tank || tank == actors[1].GetCounter())) return "wipe_tank_not_swapped";
        auto* group = leader->GetGroup();
        if (!group || !group->isRaidGroup()) return "wipe_group_not_ready";
        for (std::size_t i = 1; i <= 10; ++i)
        {
            auto* p = Actor(i);
            if (!p || !p->IsAlive() || p->IsBeingTeleported() || p->GetMap() != leader->GetMap() ||
                p->GetGroup() != group || !sRandomPlayerbotMgr.IsPinned(actors[i].GetCounter()) ||
                !Leeroy::Guild::OfflineRaidOwns(actors[i].GetCounter())) return "wipe_actor_not_ready";
        }
        if (forceTankSwap)
        {
            // Explicit role-change stimulus for a separate deterministic gate.
            // It must never be reported as the encounter AI's natural choice.
            group->SetGroupMemberFlag(actors[2], true, MEMBER_FLAG_MAINTANK);
            tank = MainTank(leader);
            tankSwapForced = true;
            if (tank != actors[2].GetCounter()) return "wipe_forced_tank_not_applied";
        }
        restartsAtWipe = native.value("restarts", 0u);
        wipeBoss = encounter; mainTankAtWipe = tank;
        scriptedWipe = true;
        // A test stimulus, never an earned combat outcome. All recovery after
        // these real deaths belongs to the ordinary offline job and core.
        for (std::size_t i = 1; i <= 10; ++i) Unit::Kill(boss, Actor(i));
        // The forced full-wipe stimulus includes declining pending Ankh and
        // Soulstone offers. Otherwise automatic self-rescues turn it into a
        // partial wipe before the ordinary full-wipe detector can observe it.
        // Spirit of Redemption expires naturally; no healing/revival is supplied.
        if (forceTankSwap)
            for (std::size_t i = 1; i <= 10; ++i)
                if (auto* p = Actor(i); p && p->GetUInt32Value(PLAYER_SELF_RES_SPELL))
                {
                    ++selfResSuppressed;
                    p->SetUInt32Value(PLAYER_SELF_RES_SPELL, 0);
                }
        return "wiped";
    }
    json Status()
    {
        std::lock_guard<std::recursive_mutex> lock(mutex);
        auto const native = Leeroy::Guild::DojoOfflineSnapshot();
        if (!native.value("active", false)) { RestoreWormSupport(); ReleaseFrontier(); }
        if (!native.value("active", false)) naxxPass.Release(RaidActors());
        if (!native.value("active", false)) tocIntro.Release(RaidActors());
        if (!native.value("active", false)) stall.Release(RaidActors());
        json bodies = json::array(), damage = json::array(), pools = json::array();
        json enemies = json::array(), spellSamples = json::array();
        for (std::size_t i = 1; i < actors.size(); ++i)
            if (auto* p = Actor(i))
            {
                json bosses = json::array(), auras = json::array();
                for (uint32 spell : {66823u, 67618u, 67619u, 67620u, 66830u, 66869u})
                    if (p->HasAura(spell)) auras.push_back(spell);
                if (auto* script = p->GetInstanceScript())
                    for (uint32 b = 0; b < script->GetEncounterCount(); ++b) bosses.push_back(uint32(script->GetBossState(b)));
                auto* casting = p->GetCurrentSpell(CURRENT_GENERIC_SPELL);
                auto* channel = p->GetCurrentSpell(CURRENT_CHANNELED_SPELL);
                auto* victim = p->GetVictim();
                auto* ai = GET_PLAYERBOT_AI(p);
                auto const& movement = ai->GetAiObjectContext()->GetValue<LastMovement&>("last movement")->Get();
                auto* selected = ai->GetAiObjectContext()->GetValue<Unit*>("current target")->Get();
                bodies.push_back({{"slot", i}, {"guid", actors[i].GetCounter()}, {"alive", p->IsAlive()},
                    {"map", p->GetMapId()}, {"instance", p->GetInstanceId()}, {"health", p->GetHealth()},
                    {"max_health", p->GetMaxHealth()}, {"mana", p->GetPower(POWER_MANA)},
                    {"max_mana", p->GetMaxPower(POWER_MANA)}, {"healer", PlayerbotAI::IsHeal(p)},
                    {"cast", casting ? casting->GetSpellInfo()->Id : 0u},
                    {"channel", channel ? channel->GetSpellInfo()->Id : 0u},
                    {"target", victim ? victim->GetGUID().GetCounter() : 0u},
                    {"target_entry", victim ? victim->GetEntry() : 0u},
                    {"ai_target_entry", selected ? selected->GetEntry() : 0u},
                    {"ai_state", uint32(ai->GetState())}, {"can_move", ai->CanMove()},
                    {"moving", p->isMoving()}, {"rooted", p->IsRooted()},
                    {"motion", uint32(p->GetMotionMaster()->GetCurrentMovementGeneratorType())},
                    {"spline_active", p->movespline && !p->movespline->Finalized()},
                    {"last_move", {{"age_ms", GetMSTimeDiffToNow(movement.msTime)},
                        {"priority", uint32(movement.priority)}, {"delay_ms", movement.lastdelayTime},
                        {"x", movement.lastMoveShort.GetPositionX()}, {"y", movement.lastMoveShort.GetPositionY()},
                        {"z", movement.lastMoveShort.GetPositionZ()}}},
                    {"x", p->GetPositionX()}, {"y", p->GetPositionY()}, {"z", p->GetPositionZ()},
                    {"combat", p->IsInCombat()}, {"bosses", bosses}, {"auras", auras},
                    {"speed_run", p->GetSpeed(MOVE_RUN)}, {"tank", PlayerbotAI::IsTank(p)},
                    {"grouped", p->GetGroup() != nullptr}, {"owned", Leeroy::Guild::OfflineRaidOwns(actors[i].GetCounter())},
                    {"pinned", sRandomPlayerbotMgr.IsPinned(actors[i].GetCounter())},
                    {"master", GET_PLAYERBOT_AI(p)->GetMaster() ? GET_PLAYERBOT_AI(p)->GetMaster()->GetGUID().GetCounter() : 0}});
            }
        for (auto const& [entry, value] : hits) damage.push_back({{"entry", entry}, {"hits", value.first}, {"requested", value.second}});
        // Read only, bounded diagnostics for the actual Beasts actors. In
        // particular, distinguish a surviving enraged worm from a cure stall.
        if (auto* leader = Actor(1); leader && map == 649 && leader->GetMapId() == map)
            for (uint32 entry : {34796u, 34799u, 35144u, 34797u, 35176u})
            {
                std::list<Creature*> found;
                leader->GetCreatureListWithEntryInGrid(found, entry, 200.f);
                for (auto* creature : found)
                    if (enemies.size() < 16)
                    {
                        auto* cast = creature->GetCurrentSpell(CURRENT_GENERIC_SPELL);
                        auto* channel = creature->GetCurrentSpell(CURRENT_CHANNELED_SPELL);
                        auto* victim = creature->GetVictim();
                        enemies.push_back({{"entry", entry}, {"guid", creature->GetGUID().GetCounter()},
                            {"instance", creature->GetInstanceId()}, {"alive", creature->IsAlive()},
                            {"health", creature->GetHealth()}, {"max_health", creature->GetMaxHealth()},
                            {"enraged", creature->HasAura(68335)},
                            {"cast", cast ? cast->GetSpellInfo()->Id : 0u},
                            {"channel", channel ? channel->GetSpellInfo()->Id : 0u},
                            {"target", victim ? victim->GetGUID().GetCounter() : 0u},
                            {"x", creature->GetPositionX()}, {"y", creature->GetPositionY()},
                            {"z", creature->GetPositionZ()}, {"facing", creature->GetOrientation()}});
                    }
            }
        for (auto const& sample : spellDamage)
            if (sample.sequence)
                spellSamples.push_back({{"sequence", sample.sequence}, {"at_ms", sample.atMs},
                    {"instance", sample.instance}, {"entry", sample.entry}, {"spell", sample.spell},
                    {"slot", sample.slot}, {"calculated", sample.calculated}, {"health_before", sample.healthBefore}});
        for (auto const& [guid, pool] : slimePools)
            pools.push_back({{"guid", guid.GetCounter()}, {"casts", pool.casts},
                {"age_ms", clockMs - pool.firstMs}, {"last_ms", clockMs - pool.lastMs},
                {"minimum_radius", pool.minimum}, {"maximum_radius", pool.maximum}});
        return {{"kind", "dojo-offline-observation"}, {"format", 1}, {"state", state}, {"error", error},
            {"token", token}, {"guild", guildId}, {"owner", actors[0].GetCounter()}, {"samples", samples},
            {"native", native}, {"bodies", bodies}, {"damage", damage},
            {"toc_enemies", enemies}, {"spell_damage_samples", spellSamples},
            {"naxx_pass", naxxPass.Snapshot()},
            {"toc_intro", tocIntro.Snapshot()},
            {"native_stall", stall.Snapshot()},
            {"toc_worm_support", {{"requested", wormRequested}, {"active_samples", wormSamples}, {"restored", wormRestored}}},
            {"valithria_frontier", {{"state", frontier}, {"measured_ms", frontierMs}, {"heals", frontierHeals},
                {"healing", frontierHealing}, {"start_health", frontierStartHealth}, {"peak_health", frontierPeakHealth},
                {"setup_completed_mask", frontierSetupMask},
                {"setup_strategies_restored", std::none_of(frontierHeld.begin(), frontierHeld.end(), [](bool held) { return held; })}}},
            {"scripted_wipe", scriptedWipe}, {"restarting_seen", restartingSeen}, {"restarts_at_wipe", restartsAtWipe},
            {"main_tank", MainTank(Actor(1))}, {"wipe_boss", wipeBoss}, {"main_tank_at_wipe", mainTankAtWipe},
            {"tank_swap_forced", tankSwapForced},
            {"self_res_suppressed", selfResSuppressed},
            {"slime", {{"pools", pools}, {"max_gap_ms", slimeGap}, {"inside_samples", slimeInside},
                {"escapes", slimeEscapes}, {"outside_ms", slimeOutsideMs}, {"hits", slimeHits},
                {"late_hits", slimeLateHits}, {"unknown_hits", slimeUnknownHits},
                {"mechanics_enabled", sConfigMgr->GetOption<bool>("Leeroy.Mechanics.Enable", false)}}}};
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
            {"stop", Stop, SEC_CONSOLE, Console::Yes}, {"wipe", Wipe, SEC_CONSOLE, Console::Yes},
            {"wipe-tank-swap", WipeTankSwap, SEC_CONSOLE, Console::Yes},
            {"wipe-force-tank-swap", WipeForceTankSwap, SEC_CONSOLE, Console::Yes}};
        static ChatCommandTable dojo{{"offline", commands}};
        return {{"dojo", dojo}};
    }
    static bool Prepare(ChatHandler* h) { h->SendSysMessage("dojo offline: " + fixture.Prepare()); return true; }
    static bool Wipe(ChatHandler* h, Tail args) { h->SendSysMessage("dojo offline: " + fixture.Wipe(std::string(args))); return true; }
    static bool WipeTankSwap(ChatHandler* h, Tail args)
    { h->SendSysMessage("dojo offline: " + fixture.Wipe(std::string(args), true)); return true; }
    static bool WipeForceTankSwap(ChatHandler* h, Tail args)
    { h->SendSysMessage("dojo offline: " + fixture.Wipe(std::string(args), true, true)); return true; }
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
    Damage() : UnitScript("DojoOfflineDamage", true,
        {UNITHOOK_ON_DAMAGE, UNITHOOK_ON_HEAL, UNITHOOK_MODIFY_SPELL_DAMAGE_TAKEN}) { }
    void OnHeal(Unit* healer, Unit* receiver, uint32& amount) override
    {
        std::lock_guard<std::recursive_mutex> lock(fixture.mutex);
        if (fixture.state != "running" || fixture.frontier != "ready" || !healer || !receiver || !amount
            || receiver->GetGUID() != fixture.frontierDragon
            || std::find(fixture.actors.begin() + 1, fixture.actors.begin() + 11, healer->GetGUID()) == fixture.actors.begin() + 11
            || !Leeroy::Guild::OfflineRaidOwns(healer->GetGUID().GetCounter())) return;
        ++fixture.frontierHeals; fixture.frontierHealing += amount;
    }
    void ModifySpellDamageTaken(Unit* victim, Unit* attacker, int32& calculated, SpellInfo const* info) override
    {
        if (!victim || !attacker || !info || calculated <= 0 || victim->GetMapId() != 649
            || attacker->GetMap() != victim->GetMap()) return;
        std::lock_guard<std::recursive_mutex> lock(fixture.mutex);
        if (fixture.state != "running" || fixture.map != 649) return;
        auto found = std::find(fixture.actors.begin() + 1, fixture.actors.begin() + 11, victim->GetGUID());
        if (found == fixture.actors.begin() + 11) return;
        // This hook precedes final damage application. These are diagnostic
        // calculations, not landed-hit or lethal-damage assertions.
        uint64 const sequence = ++fixture.spellDamageSequence;
        fixture.spellDamage[(sequence - 1) % fixture.spellDamage.size()] =
            {sequence, fixture.clockMs, victim->GetInstanceId(), attacker->GetEntry(), info->Id,
                uint32(found - fixture.actors.begin()), uint32(calculated), victim->GetHealth()};
    }
    void OnDamage(Unit* attacker, Unit* victim, uint32& amount) override
    {
        std::lock_guard<std::recursive_mutex> lock(fixture.mutex);
        if (fixture.state != "running" || !attacker || !victim || !amount) return;
        if (std::find(fixture.actors.begin() + 1, fixture.actors.end(), victim->GetGUID()) == fixture.actors.end()) return;
        auto& counter = fixture.hits[attacker->GetEntry()];
        ++counter.first; counter.second += amount;
        if (attacker->GetEntry() == 35176 && attacker->GetMapId() == 649)
        {
            ++fixture.slimeHits;
            auto const found = fixture.slimePools.find(attacker->GetGUID());
            if (found == fixture.slimePools.end()) ++fixture.slimeUnknownHits;
            else if (fixture.clockMs - found->second.firstMs > 2000) ++fixture.slimeLateHits;
        }
    }
};
class SlimeCasts : public AllSpellScript
{
public:
    SlimeCasts() : AllSpellScript("DojoOfflineSlimeCasts", {ALLSPELLHOOK_ON_PREPARE, ALLSPELLHOOK_ON_CAST}) { }
    void OnSpellPrepare(Spell* spell, Unit* caster, SpellInfo const* info) override
    { Record(spell, caster, info, false); }
    void OnSpellCast(Spell* spell, Unit* caster, SpellInfo const* info, bool /*skipCheck*/) override
    { Record(spell, caster, info, true); }
    void Record(Spell* spell, Unit* caster, SpellInfo const* info, bool completed)
    {
        std::lock_guard<std::recursive_mutex> lock(fixture.mutex);
        auto* leader = fixture.Actor(1);
        if (fixture.state != "running" || fixture.map != 649 || !leader || !spell || !caster || !info
            || caster->GetEntry() != 35176 || caster->GetMap() != leader->GetMap() || info->Id != 66881
            || !Leeroy::Guild::OfflineRaidOwns(leader->GetGUID().GetCounter())) return;
        float radius = 0.f;
        for (SpellEffectInfo const& effect : info->Effects)
            if (effect.IsTargetingArea())
                radius = std::max(radius, effect.CalcRadius(caster) * spell->GetSpellValue()->RadiusMod);
        if (!std::isfinite(radius) || radius <= 0.f) { fixture.error = "invalid_native_slime_radius"; return; }
        auto [where, inserted] = fixture.slimePools.try_emplace(caster->GetGUID());
        auto& pool = where->second;
        if (inserted) pool.firstMs = fixture.clockMs;
        // Immediate damage happens before AllSpellScript::OnSpellCast. Prepare
        // establishes identity; only the successful cast counts as stimulus.
        if (completed)
        {
            ++pool.casts; pool.lastMs = fixture.clockMs; pool.radius = radius;
            pool.minimum = std::min(pool.minimum, radius); pool.maximum = std::max(pool.maximum, radius);
        }
    }
};
}
void AddSC_dojo_offline() { new Commands(); new Ticks(); new Damage(); new SlimeCasts(); }
#else
void AddSC_dojo_offline() { }
#endif
#else
void AddSC_dojo_offline() { }
#endif
