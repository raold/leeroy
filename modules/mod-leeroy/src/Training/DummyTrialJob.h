// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_DUMMY_TRIAL_JOB_H
#define MOD_LEEROY_DUMMY_TRIAL_JOB_H
#include "LeeroyCommands.h"
#include <cstdint>
#include <string>

namespace Leeroy::Training
{
LeeroyCommandResult HandleDummyTrial(nlohmann::json const&);
void TickDummyTrial(std::uint32_t diff);
bool DummyTrialRunning();
bool DummyTrialOwns(std::uint32_t guid); // World thread; includes admitted requests and unresolved recovery.
void EndDummyTrial(std::string const& reason);
void DummyPlayerLogout(std::uint32_t guid);
void ShutdownDummyTrial();
}
#endif
