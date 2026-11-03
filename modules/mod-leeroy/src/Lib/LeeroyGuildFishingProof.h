// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_GUILD_FISHING_PROOF_H
#define MOD_LEEROY_GUILD_FISHING_PROOF_H
#include <cstdint>
#include <string_view>
namespace Leeroy::Guild
{
// Immutable dispatch identity plus exact native receipts. A pole, matching spell
// number or elapsed time is never evidence that this attempt cast or ended.
struct FishingProof
{
    std::uint64_t serial = 0, bobber = 0;
    std::uintptr_t spell = 0;
    bool prepared = false, cast = false, canceled = false, removed = false, poisoned = false, sealed = false;
    bool Prepare(std::uint64_t dispatch, std::uintptr_t pointer)
    {
        if (sealed || prepared || !dispatch || !pointer) { poisoned = true; return false; }
        serial = dispatch; spell = pointer; prepared = true; return true;
    }
    bool AddBobber(std::uint64_t dispatch, std::uintptr_t pointer, std::uint64_t guid)
    {
        if (!prepared || sealed || dispatch != serial || pointer != spell || !guid || bobber)
        { poisoned = true; return false; }
        bobber = guid; return true;
    }
    void Cast(std::uintptr_t pointer)
    { if (!prepared || pointer != spell || cast) poisoned = true; else cast = true; }
    void Cancel(std::uintptr_t pointer)
    { if (!prepared || pointer != spell) poisoned = true; else canceled = true; }
    void Remove(std::uint64_t guid)
    { if (!bobber || guid != bobber) poisoned = true; else removed = true; }
    bool Started() const { return prepared && cast && bobber && !poisoned; }
    bool CanHandBack(bool exactWeapons, bool channelAbsent, bool bobberAbsent, bool lootClosed) const
    {
        return sealed && !poisoned && exactWeapons && channelAbsent && bobberAbsent && lootClosed
            && (!prepared || (bobber ? removed : canceled));
    }
};
inline bool FishingActionName(std::string_view name)
{
    return name == "go fishing" || name == "move near water" || name == "use fishing bobber"
        || name == "equip fishing pole" || name == "see spell" || name == "end master fishing";
}
}
#endif
