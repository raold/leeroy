// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_LEEROY_LEEROY_COMMANDS_H
#define MOD_LEEROY_LEEROY_COMMANDS_H

#include "LeeroyProtocol.h"

#include <functional>
#include <string>
#include <nlohmann/json.hpp>

// World thread; returns the reply line (a pong, a cast or an ack).
std::string LeeroyExecuteCommand(Leeroy::Command const& cmd);

// Extension commands (P1d registers its nine here). The bridge has already checked v, id and type; the handler gets
// the whole message, checks its own fields, runs on the world thread and returns the ack's result.
struct LeeroyCommandResult
{
    bool ok = false;
    std::string error;
};

using LeeroyCommandHandler = std::function<LeeroyCommandResult(nlohmann::json const& msg)>;

// World thread (OnStartup). Stores the handler, then calls Leeroy::RegisterExtensionCommand so the bridge thread
// starts accepting the type. false for a core type, a duplicate, an empty type or an empty handler.
bool LeeroyRegisterCommand(std::string const& type, LeeroyCommandHandler handler);

#endif
