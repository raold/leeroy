// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyModule.h"

#include "Config.h"
#include "Log.h"

LeeroyModule& LeeroyModule::Instance()
{
    static LeeroyModule instance;
    return instance;
}

void LeeroyModule::LoadConfig(bool /*reload*/)
{
    LeeroyConfig cfg;
    cfg.enable = sConfigMgr->GetOption<bool>("Leeroy.Enable", true);
    cfg.guildId = sConfigMgr->GetOption<uint32>("Leeroy.GuildId", 21);
    cfg.humanGuid = sConfigMgr->GetOption<uint32>("Leeroy.HumanGuid", 1);
    cfg.bridgeHost = sConfigMgr->GetOption<std::string>("Leeroy.Bridge.Host", "127.0.0.1");

    uint32 port = sConfigMgr->GetOption<uint32>("Leeroy.Bridge.Port", 7878);
    if (port < 1 || port > 65535)
    {
        LOG_ERROR("module.leeroy", "mod-leeroy: Leeroy.Bridge.Port {} is out of range 1..65535, keeping 7878", port);
        port = 7878;
    }
    cfg.bridgePort = static_cast<uint16>(port);

    cfg.keepCast = sConfigMgr->GetOption<bool>("Leeroy.KeepCast", true);
    cfg.bringCastOnline = sConfigMgr->GetOption<bool>("Leeroy.BringCastOnline", true);
    cfg.excludeFromOllamaChat = sConfigMgr->GetOption<bool>("Leeroy.ExcludeFromOllamaChat", true);

    _config = cfg;
    _enabled = cfg.enable;
    _guildId = cfg.guildId;
    _humanGuid = cfg.humanGuid;
    _excludeFromOllama = cfg.excludeFromOllamaChat;
}
