// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#ifndef DOJO_TRAINING_BUILD
#error "DOJO_TRAINING_BUILD is not set: mod-dojo.cmake was skipped (the module folder must be named exactly mod-dojo)"
#endif

#if DOJO_TRAINING_BUILD

#include "DojoExecutor.h"
#include "DojoFixtures.h"
#include "DojoRuntime.h"

#include "ArenaScript.h"
#include "Chat.h"
#include "CommandScript.h"
#include "Player.h"
#include "PlayerScript.h"
#include "ServerScript.h"
#include "Spell.h"
#include "SpellInfo.h"
#include "WorldScript.h"

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
        };
        static ChatCommandTable dojo = {
            {"status", HandleStatus, SEC_CONSOLE, Console::Yes},
            {"scenario", HandleScenario, SEC_CONSOLE, Console::Yes},
            {"fixture", fixture},
            {"arena", arena},
            {"control", control},
            {"harness", harness},
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
        if (!Refuse(handler))
            Print(handler, Dojo::Director::Instance().Create());
        return true;
    }

    static bool HandleFixtureBuild(ChatHandler* handler)
    {
        if (!Refuse(handler))
            Print(handler, Dojo::Director::Instance().StartBuild());
        return true;
    }

    static bool HandleFixtureRestore(ChatHandler* handler)
    {
        if (!Refuse(handler))
            Print(handler, Dojo::Director::Instance().StartRestore());
        return true;
    }

    static bool HandleArenaOpen(ChatHandler* handler)
    {
        if (!Refuse(handler))
            Print(handler, Dojo::Director::Instance().StartOpen());
        return true;
    }

    static bool HandleArenaClose(ChatHandler* handler)
    {
        if (!Refuse(handler))
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
        std::optional<float> const last = words.size() == 1 ? Number(words[0]) : std::optional<float>(50.0f);
        if (!last || *last < 1)
            return Usage(handler, "dojo control trace [count]");
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
        Dojo::Director::Instance().Update(diff);
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
    }
};

void AddSC_dojo_commands()
{
    new DojoCommandScript();
    new DojoArenaScript();
    new DojoFixtureCastScript();
    new DojoPacketScript();
    new DojoTickScript();
}

#else

// The disabled variant registers no command, hook or tick.
void AddSC_dojo_commands()
{
}

#endif
