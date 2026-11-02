// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_LEEROY_LEEROY_INTEGRATIONS_H
#define MOD_LEEROY_LEEROY_INTEGRATIONS_H

#include "Define.h"

// World thread. Pins the cast in playerbots (an empty set with both flags off when the module is disabled);
// returns how many characters SetPinnedBots accepted.
uint32 LeeroyApplyPins();

// OnStartup. Each returns false when the module was built without that integration.
bool LeeroyInstallOllamaFilter();   // mod-ollama-chat stops voicing the cast
bool LeeroyInstallDcObserver();     // mod-dungeon-clear status reaches the mind as dc_status

// World thread. The observer sees neither edge of a run: OnUpdate polls mod-dungeon-clear's list of running tanks once
// a second and publishes each start and end as a dc_status with no payload; the reset after a greeting re-reports
// everyone running. No-ops when the module was built without that integration.
void LeeroyPollDcTanks();
void LeeroyResetDcTanks();

#endif
