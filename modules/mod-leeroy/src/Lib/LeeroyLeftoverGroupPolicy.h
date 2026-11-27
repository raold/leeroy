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
    bool inEncounter = false; // its instance has a boss encounter in progress
};

enum class LeftoverVerdict { Disband, KeepNotOwned, KeepCombat, KeepEncounter };

inline char const* LeftoverVerdictCode(LeftoverVerdict verdict)
{
    switch (verdict)
    {
        case LeftoverVerdict::KeepCombat: return "combat";
        case LeftoverVerdict::KeepEncounter: return "encounter";
        default: return "owned";
    }
}

// A group is a restart leftover only when every member is cast, nobody drives or reserves it, and nobody is
// fighting or in a boss encounter: a leader handover mid-boss must not disband the raid.
template <class Range>
LeftoverVerdict DecideLeftoverGroup(Range const& members)
{
    bool owned = true, combat = false, encounter = false;
    for (auto const& m : members)
    {
        if (m.human || !m.cast || m.reserved || m.driven) owned = false;
        if (m.inCombat) combat = true;
        if (m.inEncounter) encounter = true;
    }
    if (!owned) return LeftoverVerdict::KeepNotOwned;
    if (combat) return LeftoverVerdict::KeepCombat;
    if (encounter) return LeftoverVerdict::KeepEncounter;
    return LeftoverVerdict::Disband;
}
}
#endif
