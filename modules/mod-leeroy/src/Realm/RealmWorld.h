// This file is part of leeroy, released under the GNU GPL v2.
#ifndef LEEROY_REALM_WORLD_H
#define LEEROY_REALM_WORLD_H
#include <cstdint>
namespace Leeroy::Realm
{
void RegisterWorldRosterHandler();
bool WorldOn();
bool WorldSpeaker(std::uint32_t guid, std::uint32_t guild);
}
#endif
