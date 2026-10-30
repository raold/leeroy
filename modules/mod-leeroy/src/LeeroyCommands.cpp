// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyCommands.h"
#include "LeeroyCastModel.h"
#include "LeeroyModule.h"

#include "Log.h"

#include <cstdint>
#include <exception>
#include <unordered_map>
#include <utility>

namespace
{
// World thread only: registered from OnStartup, read by LeeroyExecuteCommand.
std::unordered_map<std::string, LeeroyCommandHandler>& Handlers()
{
    static std::unordered_map<std::string, LeeroyCommandHandler> handlers;
    return handlers;
}

std::string ExecuteExtension(Leeroy::Command const& cmd, std::int64_t now)
{
    auto const it = Handlers().find(cmd.extType);
    if (it == Handlers().end())
        return Leeroy::EncodeAck(now, cmd.id, false, std::string(Leeroy::Err::UnknownType) + cmd.extType);
    try
    {
        LeeroyCommandResult const result = it->second(cmd.raw);
        return Leeroy::EncodeAck(now, cmd.id, result.ok, result.error);
    }
    catch (std::exception const& e)
    {
        LOG_ERROR("module.leeroy", "mod-leeroy: {} handler threw on command {}: {}", cmd.extType, cmd.id, e.what());
    }
    catch (...)
    {
        LOG_ERROR("module.leeroy", "mod-leeroy: {} handler threw on command {}", cmd.extType, cmd.id);
    }
    return Leeroy::EncodeAck(now, cmd.id, false, Leeroy::Err::HandlerError);
}
}

std::string LeeroyExecuteCommand(Leeroy::Command const& cmd)
{
    LeeroyModule& module = LeeroyModule::Instance();
    std::int64_t const now = Leeroy::NowUnixMs();
    switch (cmd.type)
    {
        case Leeroy::CommandType::Ping:
            return Leeroy::EncodePong(now, cmd.id);
        case Leeroy::CommandType::GetCast:
            return Leeroy::EncodeCast(now, module.Cast().BuildMembers(module.GuildId()));
        case Leeroy::CommandType::Extension:
            return ExecuteExtension(cmd, now);
        case Leeroy::CommandType::Say:
        case Leeroy::CommandType::Emote:
        case Leeroy::CommandType::HumanSay:
        case Leeroy::CommandType::SetRank:
        case Leeroy::CommandType::SetMotd:
        case Leeroy::CommandType::AddFriend:
        case Leeroy::CommandType::Afk:
            break;
    }
    return Leeroy::EncodeAck(now, cmd.id, false, "not implemented");
}

bool LeeroyRegisterCommand(std::string const& type, LeeroyCommandHandler handler)
{
    if (type.empty() || !handler)
        return false;
    auto& handlers = Handlers();
    auto const [it, inserted] = handlers.emplace(type, std::move(handler));
    if (!inserted)
        return false;
    // Stored first so the type is never accepted without a handler. Refused here: a core type name, or a type
    // registered with the protocol directly.
    if (!Leeroy::RegisterExtensionCommand(type))
    {
        handlers.erase(it);
        return false;
    }
    return true;
}
