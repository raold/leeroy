// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_GUILD_STRINGS_H
#define MOD_LEEROY_GUILD_STRINGS_H

#include <cstdint>

namespace Leeroy::Guild
{
// Leeroy.Strings.Enable: the owner's strings in game (string_do, pure policy LeeroyStrings.h). World thread.
void ReloadStrings();
// OnStartup: registers string_do.
void RegisterStringsHandler();
// Once a second: lifts come_to_me's stay when it ends, follow's when the group does, and stands a sitting member up
// when her seat ends. A body another owner holds is never touched; its hold waits until it is free again.
void TickStrings(std::uint32_t diffMs);
// At her logout: her holds and seat end with that life.
void ForgetStrings(std::uint32_t guid);
// World thread: a live come_to_me stay holds her for its 10 minutes (StringHoldFences). NativeOwnershipReason
// answers busy:string for it, so the Realm (its moves, the bond's inn visits), training and offline raids refuse her.
// False inside a StringWaiver.
bool StringStayHeld(std::uint32_t guid);
// His own asks pass that fence: his strings, and his add command filling or bringing his group. World thread.
class StringWaiver
{
public:
    StringWaiver();
    ~StringWaiver();
    StringWaiver(StringWaiver const&) = delete;
    StringWaiver& operator=(StringWaiver const&) = delete;
};
}

#endif
