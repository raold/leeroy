// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_LEEROY_LEEROY_MODULE_H
#define MOD_LEEROY_LEEROY_MODULE_H

#include "Define.h"

#include <atomic>
#include <string>

struct LeeroyConfig
{
    bool enable = true;
    uint32 guildId = 21;
    uint32 humanGuid = 1;
    std::string bridgeHost = "127.0.0.1";
    uint16 bridgePort = 7878;
    bool keepCast = true;
    bool bringCastOnline = true;
    bool excludeFromOllamaChat = true;
};

class LeeroyModule
{
public:
    static LeeroyModule& Instance();

    void LoadConfig(bool reload);
    LeeroyConfig const& Config() const { return _config; }

    bool Enabled() const { return _enabled.load(); }
    uint32 GuildId() const { return _guildId.load(); }
    uint32 HumanGuid() const { return _humanGuid.load(); }
    bool ExcludeFromOllama() const { return _excludeFromOllama.load(); }

private:
    LeeroyConfig _config;
    std::atomic<bool> _enabled{true};
    std::atomic<uint32> _guildId{21};
    std::atomic<uint32> _humanGuid{1};
    std::atomic<bool> _excludeFromOllama{true};
};

#endif
