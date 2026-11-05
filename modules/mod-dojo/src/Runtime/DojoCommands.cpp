// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#ifndef DOJO_TRAINING_BUILD
#error "DOJO_TRAINING_BUILD is not set: mod-dojo.cmake was skipped (the module folder must be named exactly mod-dojo)"
#endif

#if DOJO_TRAINING_BUILD

#include "DojoEndpoint.h"
#include "DojoExecutor.h"
#include "DojoFixtures.h"
#include "DojoReset.h"
#include "DojoRuntime.h"

#include "AllSpellScript.h"
#include "ArenaScript.h"
#include "Chat.h"
#include "CommandScript.h"
#include "Player.h"
#include "PlayerScript.h"
#include "ServerScript.h"
#include "Spell.h"
#include "SpellInfo.h"
#include "UnitScript.h"
#include "WorldScript.h"

#include <algorithm>
#include <optional>
#include <sstream>
#include <vector>

using namespace Acore::ChatCommands;

namespace
{
// Console only (RA on the dojo's loopback port or the console itself): a training realm takes no player commands.
void Print(ChatHandler* handler, std::string const& text)
{
    std::istringstream lines(text);
    for (std::string line; std::getline(lines, line);)
        handler->SendSysMessage(line);
}

bool Refuse(ChatHandler* handler)
{
    if (Dojo::State().plan.state == Dojo::RuntimeState::Armed)
        return false;
    Print(handler, "dojo: " + std::string(Dojo::StateName(Dojo::State().plan.state)) + " (" + Dojo::State().plan.reason + ")");
    return true;
}

// Control and harness commands also need the scenario ready, which includes its action catalog.
bool RefuseControl(ChatHandler* handler)
{
    if (Refuse(handler))
        return true;
    if (Dojo::State().scenarioReady && Dojo::Control::Instance().Ready())
        return false;
    Print(handler, "dojo: the scenario is not ready (see dojo scenario)");
    return true;
}

std::vector<std::string> Words(std::string_view text)
{
    std::vector<std::string> words;
    std::istringstream in{std::string(text)};
    for (std::string word; in >> word;)
        words.push_back(word);
    return words;
}

// The actor slots a console argument names: 0, 1 or all.
std::vector<std::size_t> Slots(std::string const& word)
{
    if (word == "all")
        return {0, 1};
    if (word == "0" || word == "1")
        return {std::size_t(word[0] - '0')};
    return {};
}

// A plain decimal count, exact beyond float's 24 bits (trace sequence numbers).
std::optional<std::uint64_t> Count(std::string const& word)
{
    if (word.empty() || word.size() > 18 || !std::all_of(word.begin(), word.end(), [](char c) { return c >= '0' && c <= '9'; }))
        return std::nullopt;
    return std::stoull(word);
}

std::optional<float> Number(std::string const& word)
{
    try
    {
        std::size_t used = 0;
        float const value = std::stof(word, &used);
        if (used == word.size())
            return value;
    }
    catch (...)
    {
    }
    return std::nullopt;
}

bool Usage(ChatHandler* handler, char const* usage)
{
    Print(handler, std::string("usage: ") + usage);
    return true;
}

// The director's own jobs wait while an episode reset is in flight: that reset is using the director.
bool RefuseJob(ChatHandler* handler)
{
    if (Refuse(handler))
        return true;
    if (!Dojo::Episode::Instance().Busy())
        return false;
    Print(handler, "dojo: refused, an episode reset is in flight (dojo episode status)");
    return true;
}

std::string Rest(std::vector<std::string> const& words)
{
    std::string out;
    for (std::string const& w : words)
        out += (out.empty() ? "" : " ") + w;
    return out;
}
}

class DojoCommandScript : public CommandScript
{
public:
    DojoCommandScript() : CommandScript("DojoCommandScript") { }

    ChatCommandTable GetCommands() const override
    {
        static ChatCommandTable fixture = {
            {"create", HandleFixtureCreate, SEC_CONSOLE, Console::Yes},
            {"build", HandleFixtureBuild, SEC_CONSOLE, Console::Yes},
            {"restore", HandleFixtureRestore, SEC_CONSOLE, Console::Yes},
        };
        static ChatCommandTable arena = {
            {"open", HandleArenaOpen, SEC_CONSOLE, Console::Yes},
            {"close", HandleArenaClose, SEC_CONSOLE, Console::Yes},
        };
        static ChatCommandTable control = {
            {"status", HandleControlStatus, SEC_CONSOLE, Console::Yes},
            {"own", HandleControlOwn, SEC_CONSOLE, Console::Yes},
            {"release", HandleControlRelease, SEC_CONSOLE, Console::Yes},
            {"act", HandleControlAct, SEC_CONSOLE, Console::Yes},
            {"masks", HandleControlMasks, SEC_CONSOLE, Console::Yes},
            {"maskcheck", HandleControlMaskCheck, SEC_CONSOLE, Console::Yes},
            {"trace", HandleControlTrace, SEC_CONSOLE, Console::Yes},
            {"actions", HandleControlActions, SEC_CONSOLE, Console::Yes},
        };
        // Acceptance set-up only: every use is traced with the producer "harness".
        static ChatCommandTable harness = {
            {"place", HandleHarnessPlace, SEC_CONSOLE, Console::Yes},
            {"mana", HandleHarnessMana, SEC_CONSOLE, Console::Yes},
            {"restore", HandleHarnessRestore, SEC_CONSOLE, Console::Yes},
            {"los", HandleHarnessLos, SEC_CONSOLE, Console::Yes},
            {"kill", HandleHarnessKill, SEC_CONSOLE, Console::Yes},
            {"cooldowns", HandleHarnessCooldowns, SEC_CONSOLE, Console::Yes},
            {"disconnect", HandleHarnessDisconnect, SEC_CONSOLE, Console::Yes},
        };
        static ChatCommandTable episode = {
            {"reset", HandleEpisodeReset, SEC_CONSOLE, Console::Yes},
            {"abort", HandleEpisodeAbort, SEC_CONSOLE, Console::Yes},
            {"status", HandleEpisodeStatus, SEC_CONSOLE, Console::Yes},
            {"evidence", HandleEpisodeEvidence, SEC_CONSOLE, Console::Yes},
            {"observe", HandleEpisodeObserve, SEC_CONSOLE, Console::Yes},
            {"decide", HandleEpisodeDecide, SEC_CONSOLE, Console::Yes},
            {"schema", HandleEpisodeSchema, SEC_CONSOLE, Console::Yes},
            {"debug", HandleEpisodeDebug, SEC_CONSOLE, Console::Yes},
            {"fault", HandleEpisodeFault, SEC_CONSOLE, Console::Yes},
            {"stock", HandleEpisodeStock, SEC_CONSOLE, Console::Yes},
            {"score", HandleEpisodeScore, SEC_CONSOLE, Console::Yes},
        };
        static ChatCommandTable endpoint = {
            {"status", HandleEndpointStatus, SEC_CONSOLE, Console::Yes},
            {"boundaries", HandleEndpointBoundaries, SEC_CONSOLE, Console::Yes},
        };
        static ChatCommandTable dojo = {
            {"status", HandleStatus, SEC_CONSOLE, Console::Yes},
            {"scenario", HandleScenario, SEC_CONSOLE, Console::Yes},
            {"fixture", fixture},
            {"arena", arena},
            {"control", control},
            {"harness", harness},
            {"episode", episode},
            {"endpoint", endpoint},
        };
        static ChatCommandTable root = {{"dojo", dojo}};
        return root;
    }

    static bool HandleStatus(ChatHandler* handler)
    {
        Print(handler, Dojo::Director::Instance().Status());
        return true;
    }

    static bool HandleScenario(ChatHandler* handler)
    {
        Print(handler, Dojo::FormatChecks(Dojo::State().checks));
        return true;
    }

    static bool HandleFixtureCreate(ChatHandler* handler)
    {
        if (!RefuseJob(handler))
            Print(handler, Dojo::Director::Instance().Create());
        return true;
    }

    static bool HandleFixtureBuild(ChatHandler* handler)
    {
        if (!RefuseJob(handler))
            Print(handler, Dojo::Director::Instance().StartBuild());
        return true;
    }

    static bool HandleFixtureRestore(ChatHandler* handler)
    {
        if (!RefuseJob(handler))
            Print(handler, Dojo::Director::Instance().StartRestore());
        return true;
    }

    static bool HandleArenaOpen(ChatHandler* handler)
    {
        if (!RefuseJob(handler))
            Print(handler, Dojo::Director::Instance().StartOpen());
        return true;
    }

    static bool HandleArenaClose(ChatHandler* handler)
    {
        if (!RefuseJob(handler))
            Print(handler, Dojo::Director::Instance().StartClose());
        return true;
    }

    static bool HandleControlStatus(ChatHandler* handler)
    {
        if (!RefuseControl(handler))
            Print(handler, Dojo::Control::Instance().Status());
        return true;
    }

    static bool HandleControlOwn(ChatHandler* handler, Tail args)
    {
        if (RefuseControl(handler))
            return true;
        std::vector<std::string> const words = Words(args);
        std::vector<std::size_t> const slots = words.size() == 1 ? Slots(words[0]) : std::vector<std::size_t>{};
        if (slots.empty())
            return Usage(handler, "dojo control own <0|1|all>");
        for (std::size_t slot : slots)
            Print(handler, Dojo::Control::Instance().Own(slot));
        return true;
    }

    static bool HandleControlRelease(ChatHandler* handler, Tail args)
    {
        if (RefuseControl(handler))
            return true;
        std::vector<std::string> const words = Words(args);
        std::vector<std::size_t> const slots = words.size() == 1 ? Slots(words[0]) : std::vector<std::size_t>{};
        if (slots.empty())
            return Usage(handler, "dojo control release <0|1|all>");
        for (std::size_t slot : slots)
            Print(handler, Dojo::Control::Instance().Release(slot));
        return true;
    }

    static bool HandleControlAct(ChatHandler* handler, Tail args)
    {
        if (RefuseControl(handler))
            return true;
        std::vector<std::string> words = Words(args);
        bool const force = !words.empty() && words.back() == "force";
        if (force)
            words.pop_back();
        std::vector<std::size_t> const slots = words.size() >= 2 ? Slots(words[0]) : std::vector<std::size_t>{};
        if (slots.size() != 1)
            return Usage(handler, "dojo control act <0|1> <action> [<action>...] [force]");
        Print(handler, Dojo::Control::Instance().Submit(slots[0], {words.begin() + 1, words.end()}, force));
        return true;
    }

    static bool HandleControlMasks(ChatHandler* handler, Tail args)
    {
        if (RefuseControl(handler))
            return true;
        std::vector<std::string> const words = Words(args);
        std::vector<std::size_t> const slots = !words.empty() ? Slots(words[0]) : std::vector<std::size_t>{};
        if (slots.size() != 1 || words.size() > 2 || (words.size() == 2 && words[1] != "all"))
            return Usage(handler, "dojo control masks <0|1> [all]");
        Print(handler, Dojo::Control::Instance().Masks(slots[0], words.size() == 2));
        return true;
    }

    static bool HandleControlMaskCheck(ChatHandler* handler, Tail args)
    {
        if (RefuseControl(handler))
            return true;
        std::vector<std::string> const words = Words(args);
        std::vector<std::size_t> const slots = words.size() == 2 ? Slots(words[0]) : std::vector<std::size_t>{};
        std::optional<float> const repeats = words.size() == 2 ? Number(words[1]) : std::nullopt;
        if (slots.size() != 1 || !repeats || *repeats < 1)
            return Usage(handler, "dojo control maskcheck <0|1> <repeats>");
        Print(handler, Dojo::Control::Instance().MaskCheck(slots[0], std::uint32_t(*repeats)));
        return true;
    }

    static bool HandleControlTrace(ChatHandler* handler, Tail args)
    {
        if (RefuseControl(handler))
            return true;
        std::vector<std::string> const words = Words(args);
        if (!words.empty() && words[0] == "since")
        {
            std::optional<std::uint64_t> const seq = words.size() >= 2 ? Count(words[1]) : std::nullopt;
            std::optional<std::uint64_t> const max = words.size() == 3 ? Count(words[2]) : std::optional<std::uint64_t>(4096);
            if (!seq || !max || *max < 1 || words.size() > 3)
                return Usage(handler, "dojo control trace since <seq> [max]");
            Print(handler, Dojo::Control::Instance().TraceSince(*seq, std::size_t(*max)));
            return true;
        }
        std::optional<float> const last = words.size() == 1 ? Number(words[0]) : std::optional<float>(50.0f);
        if (!last || *last < 1)
            return Usage(handler, "dojo control trace [count] | since <seq> [max]");
        Print(handler, Dojo::Control::Instance().Trace(std::size_t(*last)));
        return true;
    }

    static bool HandleControlActions(ChatHandler* handler)
    {
        if (!RefuseControl(handler))
            Print(handler, Dojo::Control::Instance().CatalogText());
        return true;
    }

    static bool HandleHarnessPlace(ChatHandler* handler, Tail args)
    {
        if (RefuseControl(handler))
            return true;
        std::vector<std::string> const words = Words(args);
        std::vector<std::size_t> const slots = words.size() == 5 ? Slots(words[0]) : std::vector<std::size_t>{};
        std::vector<float> values;
        for (std::size_t i = 1; i < words.size(); ++i)
            if (std::optional<float> v = Number(words[i]))
                values.push_back(*v);
        if (slots.size() != 1 || values.size() != 4)
            return Usage(handler, "dojo harness place <0|1> <x> <y> <z> <o>");
        Print(handler, Dojo::Control::Instance().Place(slots[0], values[0], values[1], values[2], values[3]));
        return true;
    }

    static bool HandleHarnessMana(ChatHandler* handler, Tail args)
    {
        if (RefuseControl(handler))
            return true;
        std::vector<std::string> const words = Words(args);
        std::vector<std::size_t> const slots = words.size() == 2 ? Slots(words[0]) : std::vector<std::size_t>{};
        std::optional<float> const mana = words.size() == 2 ? Number(words[1]) : std::nullopt;
        if (slots.size() != 1 || !mana || *mana < 0)
            return Usage(handler, "dojo harness mana <0|1> <mana>");
        Print(handler, Dojo::Control::Instance().SetMana(slots[0], std::uint32_t(*mana)));
        return true;
    }

    static bool HandleHarnessRestore(ChatHandler* handler, Tail args)
    {
        if (RefuseControl(handler))
            return true;
        std::vector<std::string> const words = Words(args);
        std::vector<std::size_t> const slots = words.size() == 1 ? Slots(words[0]) : std::vector<std::size_t>{};
        if (slots.empty())
            return Usage(handler, "dojo harness restore <0|1|all>");
        for (std::size_t slot : slots)
            Print(handler, Dojo::Control::Instance().Restore(slot));
        return true;
    }

    static bool HandleHarnessKill(ChatHandler* handler, Tail args)
    {
        if (RefuseControl(handler))
            return true;
        std::vector<std::string> const words = Words(args);
        std::vector<std::size_t> const slots = words.size() == 1 ? Slots(words[0]) : std::vector<std::size_t>{};
        if (slots.empty())
            return Usage(handler, "dojo harness kill <0|1|all>");
        for (std::size_t slot : slots)
            Print(handler, Dojo::Control::Instance().Kill(slot));
        return true;
    }

    static bool HandleHarnessCooldowns(ChatHandler* handler, Tail args)
    {
        if (RefuseControl(handler))
            return true;
        std::vector<std::string> const words = Words(args);
        std::vector<std::size_t> const slots = words.size() == 1 ? Slots(words[0]) : std::vector<std::size_t>{};
        if (slots.size() != 1)
            return Usage(handler, "dojo harness cooldowns <0|1>");
        Print(handler, Dojo::Control::Instance().ClearCooldowns(slots[0]));
        return true;
    }

    static bool HandleHarnessDisconnect(ChatHandler* handler, Tail args)
    {
        if (RefuseControl(handler))
            return true;
        std::vector<std::string> const words = Words(args);
        std::vector<std::size_t> const slots = words.size() == 1 ? Slots(words[0]) : std::vector<std::size_t>{};
        if (slots.size() != 1)
            return Usage(handler, "dojo harness disconnect <0|1>");
        Print(handler, Dojo::Director::Instance().Disconnect(slots[0]));
        return true;
    }

    static bool HandleEpisodeReset(ChatHandler* handler, Tail args)
    {
        if (!RefuseControl(handler))
            Print(handler, Dojo::Episode::Instance().Reset(Rest(Words(args))));
        return true;
    }

    static bool HandleEpisodeAbort(ChatHandler* handler, Tail args)
    {
        if (!RefuseControl(handler))
            Print(handler, Dojo::Episode::Instance().Abort(Rest(Words(args))));
        return true;
    }

    static bool HandleEpisodeStatus(ChatHandler* handler)
    {
        if (!RefuseControl(handler))
            Print(handler, Dojo::Episode::Instance().Status());
        return true;
    }

    static bool HandleEpisodeEvidence(ChatHandler* handler)
    {
        if (!RefuseControl(handler))
            Print(handler, Dojo::Episode::Instance().Evidence());
        return true;
    }

    static bool HandleEpisodeObserve(ChatHandler* handler, Tail args)
    {
        if (RefuseControl(handler))
            return true;
        std::vector<std::string> const words = Words(args);
        if (words.size() > 1 || (words.size() == 1 && words[0] != "full"))
            return Usage(handler, "dojo episode observe [full]");
        Print(handler, Dojo::Episode::Instance().Observe(words.size() == 1));
        return true;
    }

    static bool HandleEpisodeDecide(ChatHandler* handler, Tail args)
    {
        if (RefuseControl(handler))
            return true;
        std::vector<std::string> words = Words(args);
        bool const force = !words.empty() && words.back() == "force";
        if (force)
            words.pop_back();
        if (words.size() != 2)
            return Usage(handler, "dojo episode decide <action for actor 0> <action for actor 1> [force]");
        Print(handler, Dojo::Episode::Instance().Decide(words, force));
        return true;
    }

    static bool HandleEpisodeSchema(ChatHandler* handler)
    {
        if (!RefuseControl(handler))
            Print(handler, Dojo::Episode::Instance().Schema());
        return true;
    }

    static bool HandleEpisodeDebug(ChatHandler* handler, Tail args)
    {
        if (RefuseControl(handler))
            return true;
        std::vector<std::string> const words = Words(args);
        std::optional<float> const last = words.size() == 1 ? Number(words[0]) : std::optional<float>(1.0f);
        if (!last || *last < 1 || *last > 600)
            return Usage(handler, "dojo episode debug [records, at most 600]");
        Print(handler, Dojo::Episode::Instance().Debug(std::size_t(*last)));
        return true;
    }

    static bool HandleEpisodeFault(ChatHandler* handler, Tail args)
    {
        if (RefuseControl(handler))
            return true;
        std::vector<std::string> const words = Words(args);
        if (words.size() != 1)
            return Usage(handler, "dojo episode fault <leave|arena|none>");
        Print(handler, Dojo::Episode::Instance().Fault(words[0]));
        return true;
    }

    static bool HandleEpisodeStock(ChatHandler* handler)
    {
        if (!RefuseControl(handler))
            Print(handler, Dojo::Episode::Instance().Stock());
        return true;
    }

    static bool HandleEpisodeScore(ChatHandler* handler)
    {
        if (!RefuseControl(handler))
            Print(handler, Dojo::Episode::Instance().Score());
        return true;
    }

    static bool HandleEndpointStatus(ChatHandler* handler)
    {
        if (!Refuse(handler))
            Print(handler, Dojo::Endpoint::Instance().Status());
        return true;
    }

    static bool HandleEndpointBoundaries(ChatHandler* handler, Tail args)
    {
        if (Refuse(handler))
            return true;
        std::vector<std::string> const words = Words(args);
        std::optional<std::uint64_t> const from = !words.empty() ? Count(words[0]) : std::optional<std::uint64_t>(0);
        std::optional<std::uint64_t> const max = words.size() == 2 ? Count(words[1]) : std::optional<std::uint64_t>(100);
        if (!from || !max || *max < 1 || words.size() > 2)
            return Usage(handler, "dojo endpoint boundaries [from decision] [max]");
        Print(handler, Dojo::Endpoint::Instance().Boundaries(*from, std::size_t(*max)));
        return true;
    }

    static bool HandleHarnessLos(ChatHandler* handler, Tail args)
    {
        if (RefuseControl(handler))
            return true;
        std::vector<float> values;
        for (std::string const& word : Words(args))
            if (std::optional<float> v = Number(word))
                values.push_back(*v);
        if (values.size() != 6)
            return Usage(handler, "dojo harness los <x1> <y1> <z1> <x2> <y2> <z2>");
        Print(handler, Dojo::Control::Instance().LineOfSight(values[0], values[1], values[2], values[3], values[4], values[5]));
        return true;
    }
};

// The director's arena keeps its stock win conditions unchecked for as long as it exists, so leaving, logging out or
// dying there ends no match and updates no achievement criteria. Every other arena is untouched.
class DojoArenaScript : public ArenaScript
{
public:
    DojoArenaScript() : ArenaScript("DojoArenaScript", {ARENAHOOK_ON_BEFORE_CHECK_WIN_CONDITION}) { }

    bool OnBeforeArenaCheckWinConditions(Battleground* const bg) override
    {
        return !Dojo::Director::Instance().SuppressesWinCheck(bg);
    }
};

// Attributes every fixture cast to its producer, and keeps fixtures from earning achievement progress.
class DojoFixtureCastScript : public PlayerScript
{
public:
    DojoFixtureCastScript() : PlayerScript("DojoFixtureCastScript", {PLAYERHOOK_ON_SPELL_CAST,
        PLAYERHOOK_ON_BEFORE_CRITERIA_PROGRESS, PLAYERHOOK_ON_BEFORE_ACHI_COMPLETE}) { }

    void OnPlayerSpellCast(Player* player, Spell* spell, bool /*skipCheck*/) override
    {
        bool const executor = Dojo::Control::Instance().OnSpellCast(player, spell);
        Dojo::Director::Instance().OnFixtureCast(player, spell->GetSpellInfo()->Id, spell->IsTriggered(), executor);
    }

    bool OnPlayerBeforeCriteriaProgress(Player* player, AchievementCriteriaEntry const* /*criteria*/) override
    {
        return !Dojo::Control::Instance().SuppressesProgress(player);
    }

    bool OnPlayerBeforeAchievementComplete(Player* player, AchievementEntry const* /*achievement*/) override
    {
        return !Dojo::Control::Instance().SuppressesProgress(player);
    }
};

// Damage as the core's scoreboard counts it (after absorbs), for the episode's events and its reconciliation tallies.
class DojoDamageScript : public UnitScript
{
public:
    DojoDamageScript() : UnitScript("DojoDamageScript", true, {UNITHOOK_ON_DAMAGE}) { }

    void OnDamage(Unit* attacker, Unit* victim, uint32& damage) override
    {
        Dojo::Episode::Instance().OnDamage(attacker, victim, damage);
    }
};

// A cast that completed (the end of Spell::_cast, after its last checks), for the episode's reconciliation tallies.
class DojoCastScript : public AllSpellScript
{
public:
    DojoCastScript() : AllSpellScript("DojoCastScript", {ALLSPELLHOOK_ON_CAST}) { }

    void OnSpellCast(Spell* spell, Unit* caster, SpellInfo const* spellInfo, bool /*skipCheck*/) override
    {
        Dojo::Episode::Instance().OnCast(caster, spellInfo, spell->IsTriggered());
    }
};

// What the server tells a fixture's session is what a client would read: cast results, starts and completions.
class DojoPacketScript : public ServerScript
{
public:
    DojoPacketScript() : ServerScript("DojoPacketScript", {SERVERHOOK_ON_PACKET_SENT}) { }

    void OnPacketSent(WorldSession* session, WorldPacket const& packet) override
    {
        Dojo::Control::Instance().OnPacketSent(session, packet);
    }
};

class DojoTickScript : public WorldScript
{
public:
    DojoTickScript() : WorldScript("DojoTickScript", {WORLDHOOK_ON_UPDATE,
        WORLDHOOK_ON_SHUTDOWN_INITIATE, WORLDHOOK_ON_SHUTDOWN_CANCEL, WORLDHOOK_ON_SHUTDOWN}) { }

    void OnUpdate(uint32 diff) override
    {
        // Ownership and due commands, the director's job, then (after both) the actors' senses and the episode, and
        // last the endpoint, which answers from what this tick made.
        Dojo::Director::Instance().Update(diff);
        Dojo::Episode::Instance().Update(diff);
        Dojo::Endpoint::Instance().Update();
    }

    void OnShutdownInitiate(ShutdownExitCode /*code*/, ShutdownMask /*mask*/) override
    {
        Dojo::Control::Instance().OnShutdown("initiated");
    }

    void OnShutdownCancel() override
    {
        Dojo::Control::Instance().OnShutdownCancel();
    }

    void OnShutdown() override
    {
        Dojo::Control::Instance().OnShutdown("final");
        Dojo::Endpoint::Instance().Shutdown();
    }
};

void AddSC_dojo_commands()
{
    new DojoCommandScript();
    new DojoArenaScript();
    new DojoFixtureCastScript();
    new DojoPacketScript();
    new DojoDamageScript();
    new DojoCastScript();
    new DojoTickScript();
}

#else

// The disabled variant registers no command, hook or tick.
void AddSC_dojo_commands()
{
}

#endif
