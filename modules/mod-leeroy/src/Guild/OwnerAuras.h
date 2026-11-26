// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_OWNER_AURAS_JOB_H
#define MOD_LEEROY_OWNER_AURAS_JOB_H
#include <cstdint>
namespace Leeroy::Guild
{
// Leeroy.OwnerAuras: the owner's character keeps the listed auras; Leeroy.OwnerPetAuras: his pet and his
// guardians keep theirs (pure policy: LeeroyOwnerAuras).
void ReloadOwnerAuras();
void TickOwnerAuras(std::uint32_t diffMs);
void AddOwnerAuraScripts();
}
#endif
