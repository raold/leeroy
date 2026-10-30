// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#ifndef MOD_LEEROY_LEEROY_CAST_H
#define MOD_LEEROY_LEEROY_CAST_H

#include "Define.h"
#include "LeeroyCastModel.h"

#include <atomic>
#include <shared_mutex>
#include <unordered_set>
#include <vector>

// The cast: the guild's members at level 80 or above, never the human. The GUID set is written on the world
// thread and read from any thread (map threads, mod-ollama-chat's submitting thread).
class LeeroyCastRegistry
{
public:
    bool Refresh(uint32 guildId, uint32 humanGuid);                      // world thread; true when the set changed
    bool IsCast(uint32 lowGuid) const;                                    // any thread
    std::unordered_set<uint32> Guids() const;                             // any thread, copy
    std::vector<Leeroy::CastMember> BuildMembers(uint32 guildId) const;   // world thread
    void MarkDirty();                                                     // any thread
    bool ConsumeDirty();                                                  // world thread
    void RequestPush();                                                   // world thread: push `cast` next update
    bool ConsumePush();                                                   // world thread

private:
    mutable std::shared_mutex _mutex;
    std::unordered_set<uint32> _guids;
    std::atomic<bool> _dirty{false};
    std::atomic<bool> _push{false};
    bool _guildMissing = false;   // world thread: "guild not found" is logged once per outage
};

void AddSC_leeroy_cast();   // guild and level hooks that MarkDirty

#endif
