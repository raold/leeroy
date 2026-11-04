// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#ifndef DOJO_TRAINING_BUILD
#error "DOJO_TRAINING_BUILD is not set: mod-dojo.cmake was skipped (the module folder must be named exactly mod-dojo)"
#endif

#if DOJO_TRAINING_BUILD

#include "DojoFixtures.h"
#include "DojoRuntime.h"

#include "ArenaScript.h"
#include "Chat.h"
#include "CommandScript.h"
#include "Player.h"
#include "PlayerScript.h"
#include "Spell.h"
#include "SpellInfo.h"
#include "WorldScript.h"

#include <sstream>

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
        };
        static ChatCommandTable arena = {
            {"open", HandleArenaOpen, SEC_CONSOLE, Console::Yes},
            {"close", HandleArenaClose, SEC_CONSOLE, Console::Yes},
        };
        static ChatCommandTable dojo = {
            {"status", HandleStatus, SEC_CONSOLE, Console::Yes},
            {"scenario", HandleScenario, SEC_CONSOLE, Console::Yes},
            {"fixture", fixture},
            {"arena", arena},
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
};

// While the director tears its own arena down, its win conditions stay unchecked, so the first actor to leave during
// preparation ends no match and updates no achievement criteria. Every other arena is untouched.
class DojoArenaScript : public ArenaScript
{
public:
    DojoArenaScript() : ArenaScript("DojoArenaScript", {ARENAHOOK_ON_BEFORE_CHECK_WIN_CONDITION}) { }

    bool OnBeforeArenaCheckWinConditions(Battleground* const bg) override
    {
        return !Dojo::Director::Instance().SuppressesWinCheck(bg);
    }
};

class DojoFixtureCastScript : public PlayerScript
{
public:
    DojoFixtureCastScript() : PlayerScript("DojoFixtureCastScript", {PLAYERHOOK_ON_SPELL_CAST}) { }

    void OnPlayerSpellCast(Player* player, Spell* spell, bool /*skipCheck*/) override
    {
        Dojo::Director::Instance().OnFixtureCast(player, spell->GetSpellInfo()->Id, spell->IsTriggered());
    }
};

class DojoTickScript : public WorldScript
{
public:
    DojoTickScript() : WorldScript("DojoTickScript", {WORLDHOOK_ON_UPDATE}) { }

    void OnUpdate(uint32 diff) override
    {
        Dojo::Director::Instance().Update(diff);
    }
};

void AddSC_dojo_commands()
{
    new DojoCommandScript();
    new DojoArenaScript();
    new DojoFixtureCastScript();
    new DojoTickScript();
}

#else

// The disabled variant registers no command, hook or tick.
void AddSC_dojo_commands()
{
}

#endif
