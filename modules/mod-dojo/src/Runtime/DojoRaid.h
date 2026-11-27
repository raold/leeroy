// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_DOJO_RAID_H
#define MOD_DOJO_RAID_H
void AddSC_dojo_raid();
class ObjectGuid;
namespace Dojo
{
// One admission boundary for all scripted raid fixture logins. Only dedicated
// synthetic accounts are accepted, even if a scenario passes the wrong GUID.
bool LoginRaidFixture(ObjectGuid guid);
bool LogoutRaidFixture(ObjectGuid guid);
}
#endif
