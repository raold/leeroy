// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_GUILD_H
#define MOD_LEEROY_REALM_GUILD_H
#include <cstddef>
class Guild;
namespace Leeroy::Realm
{
// Copies only the installed core's protected rank count; never exposes ranks
// or infers rank existence from its possibly-zero rights mask.
std::size_t RealmGuildRankCount(::Guild const& guild);
void RegisterRealmGuildHandlers();
}
#endif
