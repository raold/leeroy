// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_TRAINING_COMMANDS_H
#define MOD_LEEROY_TRAINING_COMMANDS_H
#include "LeeroyCommands.h"

namespace Leeroy::Training
{
LeeroyCommandResult HandleTelemetry(nlohmann::json const&);
LeeroyCommandResult HandleGetStats(nlohmann::json const&);
void TrainingOffCleanup();
}
#endif
