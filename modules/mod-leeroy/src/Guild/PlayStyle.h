// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_PLAY_STYLE_ADAPTER_H
#define MOD_LEEROY_PLAY_STYLE_ADAPTER_H
#include <cstdint>
#include <string_view>
class Player;
namespace Leeroy::PlayStyle
{
std::string_view Apply(Player*, std::string_view);
bool Restore(Player*, std::string_view name = "*");
bool Owns(std::uint32_t, std::string_view name = "*");
void Forget(std::uint32_t);
void Tick();
void Reload();
std::uint64_t Epoch();
}
#endif
