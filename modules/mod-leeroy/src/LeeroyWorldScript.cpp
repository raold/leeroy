// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyModule.h"

#include "Log.h"
#include "ScriptMgr.h"

class LeeroyWorldScript : public WorldScript
{
public:
    LeeroyWorldScript() : WorldScript("LeeroyWorldScript", { WORLDHOOK_ON_AFTER_CONFIG_LOAD, WORLDHOOK_ON_STARTUP })
    {
    }

    void OnAfterConfigLoad(bool reload) override
    {
        LeeroyModule::Instance().LoadConfig(reload);
    }

    void OnStartup() override
    {
        LOG_INFO("module.leeroy", "mod-leeroy: loaded (Leeroy.Enable={})", LeeroyModule::Instance().Enabled());
    }
};

void AddSC_leeroy_world()
{
    new LeeroyWorldScript();
}
