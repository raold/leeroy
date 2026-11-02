// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "RaidAssembly.h"
#include "LootMaster.h"
#include "OfflineRaidJob.h"
#include "ScriptMgr.h"

class LeeroyGuildWorldScript : public WorldScript
{
public:
    LeeroyGuildWorldScript() : WorldScript("LeeroyGuildWorldScript",
        { WORLDHOOK_ON_STARTUP, WORLDHOOK_ON_UPDATE }) { }

    void OnStartup() override
    {
        Leeroy::Guild::RegisterRaidAssemblyHandlers();
        Leeroy::Guild::RegisterLootMasterHandler();
        Leeroy::Guild::RegisterOfflineRaidHandler();
    }

    void OnUpdate(uint32 diff) override
    {
        Leeroy::Guild::TickSummons();
        Leeroy::Guild::TickOfflineRaid(diff);
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
};

class LeeroyGuildPlayerScript : public PlayerScript
{
public:
    LeeroyGuildPlayerScript() : PlayerScript("LeeroyGuildPlayerScript",
        { PLAYERHOOK_ON_CREATURE_KILL_CREDIT, PLAYERHOOK_ON_BEFORE_SEND_LOOT }) { }

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
}
