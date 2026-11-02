// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "RaidAssembly.h"
#include "ScriptMgr.h"

class LeeroyGuildWorldScript : public WorldScript
{
public:
    LeeroyGuildWorldScript() : WorldScript("LeeroyGuildWorldScript",
        { WORLDHOOK_ON_STARTUP, WORLDHOOK_ON_UPDATE }) { }

    void OnStartup() override { Leeroy::Guild::RegisterRaidAssemblyHandlers(); }
    void OnUpdate(uint32 /*diff*/) override { Leeroy::Guild::TickSummons(); }
};

void AddSC_leeroy_guild_life()
{
    new LeeroyGuildWorldScript();
}
