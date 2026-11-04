// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "RealmModule.h"
#include "RealmPorts.h"
#include "RealmPvp.h"
#include "RealmArenaObserver.h"
#include "RealmCraft.h"
#include "RealmLfgReceipts.h"
#include "RealmGuild.h"
#include "RealmBank.h"
#include "RealmMail.h"
#include "RealmCalendar.h"
#include "Guild/GuildFishing.h"
#include "ScriptMgr.h"

// These later slices are registered only when their real implementation is
// assembled into the module; unavailable commands never receive success stubs.
#if __has_include("RealmSpeech.h")
#include "RealmSpeech.h"
#define LEEROY_REALM_SPEECH 1
#else
#define LEEROY_REALM_SPEECH 0
#endif
#if __has_include("RealmBodies.h")
#include "RealmBodies.h"
#define LEEROY_REALM_BODIES 1
#else
#define LEEROY_REALM_BODIES 0
#endif

namespace Leeroy::Realm
{
void RegisterRealmHandlers()
{
    RegisterRealmContinuationHandlers();
    RegisterRealmPvpHandlers();
    RegisterRealmArenaObserverHandlers();
    RegisterRealmCraftHandlers();
    RegisterRealmGuildHandlers();
    RegisterRealmBankHandlers();
    RegisterRealmMailHandlers();
    RegisterRealmCalendarHandlers();
    RegisterCommand("realm_actors", [](nlohmann::json const& message)
    {
        std::string error;
        auto command = ParseActors(message, error);
        if (!command) return LeeroyCommandResult{false, error};
        bool const ok = RealmModule::Instance().SetActors(*command, error);
        return LeeroyCommandResult{ok, error};
    });
#if LEEROY_REALM_SPEECH
    RegisterRealmSpeechHandlers();
#endif
#if LEEROY_REALM_BODIES
    RegisterRealmBodyHandlers();
#endif
}

class RealmLifecycleScript : public PlayerScript
{
public:
    RealmLifecycleScript() : PlayerScript("LeeroyRealmLifecycleScript",
        {PLAYERHOOK_ON_LOGIN, PLAYERHOOK_ON_LOGOUT}) { }
    void OnPlayerLogin(Player* player) override
    {
        if (player) RealmModule::Instance().NotifyBodyChanged(player->GetGUID().GetCounter());
    }
    void OnPlayerLogout(Player* player) override
    {
        if (player) RealmModule::Instance().NotifyBodyChanged(player->GetGUID().GetCounter());
    }
};
}

void AddSC_leeroy_realm()
{
    Leeroy::Realm::AddCastGuardScripts();
    new Leeroy::Realm::RealmLifecycleScript();
    Leeroy::Realm::AddRealmPvpScripts();
    Leeroy::Realm::AddRealmArenaObserverScripts();
    Leeroy::Realm::AddRealmCraftScripts();
    Leeroy::Realm::AddRealmLfgReceiptScripts();
    Leeroy::Realm::AddRealmBankScripts();
    Leeroy::Realm::AddRealmMailScripts();
    Leeroy::Realm::AddRealmCalendarScripts();
    Leeroy::Realm::AddRealmGuildScripts();
    Leeroy::Guild::AddGuildFishingScripts();
#if LEEROY_REALM_SPEECH
    Leeroy::Realm::AddRealmSpeechScripts();
#endif
#if LEEROY_REALM_BODIES
    Leeroy::Realm::AddRealmBodyScripts();
#endif
}
