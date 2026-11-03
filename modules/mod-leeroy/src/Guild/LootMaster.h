// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_LOOT_MASTER_H
#define MOD_LEEROY_LOOT_MASTER_H

#include "LeeroyCommands.h"
#include "LeeroyLootLedger.h"
#include "ObjectGuid.h"

class Creature;
class Player;
struct Loot;

namespace Leeroy::Guild
{
class LootSourceLedger
{
public:
    static LootSourceLedger& Instance();
    void Note(uint32 map, uint32 instanceId, uint32 entry, ObjectGuid source);
    void Note(uint32 map, uint32 instanceId, uint32 entry, ObjectGuid source, ObjectGuid group, uint64 generation);
    std::optional<ObjectGuid> Find(uint32 map, uint32 instanceId, uint32 entry) const;
    std::optional<LootSourceObservation> Observe(uint32 map, uint32 instanceId, uint32 entry) const;
    void Invalidate(uint32 map, uint32 instanceId, uint32 entry);
    void Expire();

private:
    LootSourceLedgerState _state;
};

// Map-thread hooks only copy source identity/ownership/generation into the mutex-guarded ledger.
void NoteCreatureLoot(Creature* creature);
void NoteGameObjectLoot(Player* player, ObjectGuid guid, Loot* loot);

// Commands and transfers run on the world thread after the map-update barrier.
LeeroyCommandResult HandleGiveLoot(nlohmann::json const& message);
void RegisterLootMasterHandler();
}

#endif
