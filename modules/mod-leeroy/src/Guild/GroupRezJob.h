// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_GROUP_REZ_JOB_H
#define MOD_LEEROY_GROUP_REZ_JOB_H
#include <cstdint>
namespace Leeroy::Guild
{
void ReloadGroupRez();
void TickGroupRez(std::uint32_t diffMs);
}
#endif
