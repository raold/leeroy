// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyCast.h"
#include "LeeroyCastModel.h"
#include "LeeroyCommands.h"
#include "LeeroyEvents.h"
#include "LeeroyIntegrations.h"
#include "LeeroyModule.h"
#include "LeeroyProtocol.h"

#include "Log.h"
#include "ObjectAccessor.h"
#include "Player.h"
#include "ScriptMgr.h"

#include <cstddef>
#include <cstdint>
#include <utility>
#include <vector>

namespace
{
constexpr uint32 kRefreshIntervalMs = 60000;
constexpr uint32 kBridgeRetryMs = 30000;
constexpr uint32 kAfkPollMs = 1000;
constexpr std::size_t kMaxCommandsPerUpdate = 64;

char const* OnOff(bool on)
{
    return on ? "on" : "off";
}
}

// Lifecycle and the world-thread pump. Every hook here runs on the world thread.
class LeeroyWorldScript : public WorldScript
{
public:
    LeeroyWorldScript() : WorldScript("LeeroyWorldScript",
        { WORLDHOOK_ON_AFTER_CONFIG_LOAD, WORLDHOOK_ON_STARTUP, WORLDHOOK_ON_UPDATE, WORLDHOOK_ON_SHUTDOWN })
    {
    }

    void OnAfterConfigLoad(bool reload) override
    {
        LeeroyModule& module = LeeroyModule::Instance();
        LeeroyConfig const old = module.Config();
        module.LoadConfig(reload);
        if (!reload)
            return;

        // The cast and the pins follow in OnUpdate. A changed flag re-applies the pins even when the set is the same.
        LeeroyConfig const& cfg = module.Config();
        module.Cast().MarkDirty();
        if (cfg.enable != old.enable || cfg.keepCast != old.keepCast || cfg.bringCastOnline != old.bringCastOnline)
            _repin = true;
        if (module.Bridge().Running() && (cfg.bridgeHost != old.bridgeHost || cfg.bridgePort != old.bridgePort))
            LOG_WARN("module.leeroy", "mod-leeroy: the bridge address changed to {}:{}, which needs a restart; "
                "still listening on {}:{}", cfg.bridgeHost, cfg.bridgePort, old.bridgeHost, old.bridgePort);
    }

    void OnStartup() override
    {
        LeeroyModule& module = LeeroyModule::Instance();
        if (!module.Enabled())
        {
            LeeroyApplyPins();
            LOG_INFO("module.leeroy", "mod-leeroy: disabled (Leeroy.Enable = 0), pins cleared, nothing started");
            return;
        }

        LeeroyCastRegistry& cast = module.Cast();
        cast.Refresh(module.GuildId(), module.HumanGuid());
        uint32 const pinned = LeeroyApplyPins();
        Services const services = StartServices(module);
        LeeroyConfig const& cfg = module.Config();
        LOG_INFO("module.leeroy", "mod-leeroy: cast {} members, pinned {} (keep={}, online={}), ollama filter {}, "
            "dc observer {}, bridge {}:{} {}", cast.Guids().size(), pinned, uint32(cfg.keepCast),
            uint32(cfg.bringCastOnline), OnOff(services.ollamaFilter), OnOff(services.dcObserver), cfg.bridgeHost,
            cfg.bridgePort, services.listening ? "listening" : "not listening");
    }

    void OnUpdate(uint32 diff) override
    {
        LeeroyModule& module = LeeroyModule::Instance();
        LeeroyCastRegistry& cast = module.Cast();
        if (!module.Enabled())
        {
            // A reload turned the module off: let the cast go, and drop the mind rather than queue its commands
            // for a late replay. The retry below restarts the bridge once a reload turns the module back on.
            if (std::exchange(_repin, false))
            {
                LeeroyApplyPins();
                module.Bridge().Stop();
                LOG_INFO("module.leeroy", "mod-leeroy: disabled by a config reload, pins cleared, bridge stopped");
            }
            return;
        }

        Leeroy::Bridge& bridge = module.Bridge();
        LeeroyConfig const& cfg = module.Config();
        if (!_servicesStarted)   // off at startup, turned on by a reload
        {
            Services const services = StartServices(module);
            LOG_INFO("module.leeroy", "mod-leeroy: enabled by a config reload: ollama filter {}, dc observer {}, "
                "bridge {}:{} {}", OnOff(services.ollamaFilter), OnOff(services.dcObserver), cfg.bridgeHost,
                cfg.bridgePort, services.listening ? "listening" : "not listening");
        }

        if (bridge.Running())
            _bridgeRetryTimer = 0;
        else if ((_bridgeRetryTimer += diff) >= kBridgeRetryMs)
        {
            _bridgeRetryTimer = 0;
            bridge.Start(cfg.bridgeHost, cfg.bridgePort);
        }

        bool changed = false;
        _refreshTimer += diff;
        if (cast.ConsumeDirty() || _refreshTimer >= kRefreshIntervalMs)
        {
            _refreshTimer = 0;
            changed = cast.Refresh(module.GuildId(), module.HumanGuid());
        }
        if (changed || _repin)
        {
            _repin = false;
            uint32 const pinned = LeeroyApplyPins();
            LOG_INFO("module.leeroy", "mod-leeroy: cast {} members, pinned {} (keep={}, online={})",
                cast.Guids().size(), pinned, uint32(cfg.keepCast), uint32(cfg.bringCastOnline));
            if (changed)
                cast.RequestPush();
        }

        // Greet, then pop, then reply: the bridge hands out commands only after the greeting and drops a reply
        // whose mind has gone.
        std::uint64_t const serial = bridge.ConnectionSerial();
        if (bridge.Connected() && serial != _greetedSerial)
        {
            std::int64_t const now = Leeroy::NowUnixMs();
            uint32 const human = module.HumanGuid();
            Player* humanPlayer = ObjectAccessor::FindConnectedPlayer(ObjectGuid::Create<HighGuid::Player>(human));
            bool const humanOnline = humanPlayer && humanPlayer->IsInWorld();
            bridge.Greet(serial, Leeroy::EncodeHello(now, module.GuildId(), humanOnline ? human : 0),
                Leeroy::EncodeCast(now, cast.BuildMembers(module.GuildId())));
            _greetedSerial = serial;
            LeeroyResetAfk();   // the new mind hears about everyone already AFK on the next poll
        }

        _commands.clear();
        bridge.PopCommands(_commands, kMaxCommandsPerUpdate);
        for (Leeroy::Command const& cmd : _commands)
            bridge.Reply(LeeroyExecuteCommand(cmd));
        _commands.clear();

        if ((_afkPollTimer += diff) >= kAfkPollMs)
        {
            _afkPollTimer = 0;
            LeeroyPollAfk();
        }

        // Pushed on a GUID-set change or a successful set_rank. With no greeted mind the push is dropped: the next
        // greeting carries a fresh cast.
        if (cast.ConsumePush() && bridge.Connected() && bridge.ConnectionSerial() == _greetedSerial)
            module.Publish(Leeroy::EncodeCast(Leeroy::NowUnixMs(), cast.BuildMembers(module.GuildId())), false);
    }

    void OnShutdown() override
    {
        LeeroyModule& module = LeeroyModule::Instance();
        module.Bridge().Stop();
        module.CloseLog();
    }

private:
    struct Services
    {
        bool ollamaFilter = false;
        bool dcObserver = false;
        bool listening = false;
    };

    // Once per process: from OnStartup, or from the first update after a reload turns the module on.
    Services StartServices(LeeroyModule& module)
    {
        _servicesStarted = true;
        Services services;
        services.ollamaFilter = LeeroyInstallOllamaFilter();
        services.dcObserver = LeeroyInstallDcObserver();
        LeeroyConfig const& cfg = module.Config();
        services.listening = module.Bridge().Start(cfg.bridgeHost, cfg.bridgePort);
        _bridgeRetryTimer = 0;
        return services;
    }

    bool _servicesStarted = false;
    bool _repin = false;
    uint32 _refreshTimer = 0;
    uint32 _bridgeRetryTimer = 0;
    uint32 _afkPollTimer = 0;
    std::uint64_t _greetedSerial = 0;
    std::vector<Leeroy::Command> _commands;
};

void AddSC_leeroy_world()
{
    new LeeroyWorldScript();
}
