// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_QUEUE_FENCE_H
#define MOD_LEEROY_QUEUE_FENCE_H
// World thread, after commands/Realm steps: publish ownership for map/session
// hooks, then withdraw only queues whose public API is safe for one member.
void LeeroyRefreshQueueFence();
void AddSC_leeroy_queue_fence();
#endif
