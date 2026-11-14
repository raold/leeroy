// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "GuildAdd.h"
#include "RaidAssembly.h"
#include "LootMaster.h"
#include "GearUpJob.h"
#include "TopUpJob.h"
#include "DeathLoopJob.h"
#include "GroupRezJob.h"
#include "HazardJob.h"
#include "BotCommands.h"
#include "OfflineRaidJob.h"
#include "OwnerSay.h"
#include "RaidLead.h"
#include "PlayStyle.h"
#include "ScriptMgr.h"
#include "Player.h"
#include "Map.h"

class LeeroyGuildWorldScript : public WorldScript
{
public:
    LeeroyGuildWorldScript() : WorldScript("LeeroyGuildWorldScript",
        { WORLDHOOK_ON_STARTUP, WORLDHOOK_ON_UPDATE, WORLDHOOK_ON_AFTER_CONFIG_LOAD }) { }

    void OnAfterConfigLoad(bool /*reload*/) override
    {
        Leeroy::PlayStyle::Reload();
        Leeroy::Guild::ReloadDeathLoop();
        Leeroy::Guild::ReloadGroupRez();
        Leeroy::Guild::ReloadHazards();
        Leeroy::Guild::ReloadGuildAdd();
    }

    void OnStartup() override
    {
        Leeroy::Guild::RegisterRaidAssemblyHandlers();
        Leeroy::Guild::RegisterLootMasterHandler();
        Leeroy::Guild::RegisterGearUpHandler();
        Leeroy::Guild::RegisterSocialCommands();
        Leeroy::Guild::RegisterBotCommandHandlers();
        Leeroy::Guild::RegisterOfflineRaidHandler();
        Leeroy::Guild::RegisterOwnerSayHandler();
        Leeroy::Guild::RegisterGuildAddRosterHandler();
        Leeroy::Guild::RegisterRaidLeadHandler();
        Leeroy::Guild::InstallHazardFilter();
    }

    void OnUpdate(uint32 diff) override
    {
        Leeroy::Guild::TickSummons();
        Leeroy::Guild::TickGearUp();
        Leeroy::Guild::TickBotCommands();
        Leeroy::Guild::TickRaidLead();
        Leeroy::Guild::TickTopUp(diff);
        Leeroy::Guild::TickDeathLoop(diff);
        Leeroy::Guild::TickGroupRez(diff);
        Leeroy::Guild::TickHazards(diff);
        Leeroy::Guild::TickOfflineRaid(diff);
        if (diff >= _styleCleanup)
        {
            _styleCleanup = 1000;
            Leeroy::PlayStyle::Tick();
        }
        else _styleCleanup -= diff;
        if (diff >= _untilExpire)
        {
            Leeroy::Guild::LootSourceLedger::Instance().Expire();
            Leeroy::Guild::SweepLeftoverGroups();
            _untilExpire = 60000;
        }
        else
            _untilExpire -= diff;
    }

private:
    uint32 _untilExpire = 60000;
    uint32 _styleCleanup = 0;
};

class LeeroyGuildPlayerScript : public PlayerScript
{
public:
    LeeroyGuildPlayerScript() : PlayerScript("LeeroyGuildPlayerScript",
        { PLAYERHOOK_ON_CREATURE_KILL_CREDIT, PLAYERHOOK_ON_BEFORE_SEND_LOOT, PLAYERHOOK_ON_LOGOUT,
          PLAYERHOOK_ON_MAP_CHANGED }) { }

    void OnPlayerLogout(Player* player) override { Leeroy::PlayStyle::Forget(player->GetGUID().GetCounter()); }
    void OnPlayerMapChanged(Player* player) override
    {
        if (player->GetMap() && player->GetMap()->IsDungeon()) Leeroy::PlayStyle::Restore(player);
    }

    void OnPlayerCreatureKillCredit(Player* /*player*/, Creature* killed) override
    {
        Leeroy::Guild::NoteCreatureLoot(killed);
    }

    void OnPlayerBeforeSendLoot(Player* player, ObjectGuid guid, Loot* loot) override
    {
        Leeroy::Guild::NoteGameObjectLoot(player, guid, loot);
    }
};

void AddSC_leeroy_guild_life()
{
    new LeeroyGuildWorldScript();
    new LeeroyGuildPlayerScript();
    Leeroy::Guild::AddGuildAddScripts();
}
