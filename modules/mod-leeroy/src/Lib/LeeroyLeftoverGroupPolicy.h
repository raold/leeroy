// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_LEFTOVER_GROUP_POLICY_H
#define MOD_LEEROY_LEFTOVER_GROUP_POLICY_H

namespace Leeroy::Guild
{
// What the leftover-raid sweep needs to know about one group member.
struct LeftoverMember
{
    bool cast = false;       // a guild cast member
    bool human = false;      // the owner
    bool reserved = false;   // reserved by a DC test run
    bool driven = false;     // an active DcRun
    bool inCombat = false;
    bool inInstance = false; // on a dungeon or raid map
};

enum class LeftoverVerdict { Disband, KeepNotOwned, KeepCombat, KeepInstance };

inline char const* LeftoverVerdictCode(LeftoverVerdict verdict)
{
    switch (verdict)
    {
        case LeftoverVerdict::KeepCombat: return "combat";
        case LeftoverVerdict::KeepInstance: return "instance";
        default: return "owned";
    }
}

// A group is a restart leftover only when every member is cast, nobody drives or reserves it, and nobody is
// fighting or standing in an instance: a leader handover mid-boss must not disband the raid.
template <class Range>
LeftoverVerdict DecideLeftoverGroup(Range const& members)
{
    bool owned = true, combat = false, instance = false;
    for (auto const& m : members)
    {
        if (m.human || !m.cast || m.reserved || m.driven) owned = false;
        if (m.inCombat) combat = true;
        if (m.inInstance) instance = true;
    }
    if (!owned) return LeftoverVerdict::KeepNotOwned;
    if (combat) return LeftoverVerdict::KeepCombat;
    if (instance) return LeftoverVerdict::KeepInstance;
    return LeftoverVerdict::Disband;
}
}
#endif
