// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_OWNER_FIGHT_H
#define MOD_LEEROY_OWNER_FIGHT_H

namespace Leeroy
{
// Read the current group references at the mutation boundary. A queued snapshot cannot witness a later pull.
template <class PlayerType>
bool OwnerGroupInCombat(PlayerType* human)
{
    if (!human) return false;
    if (human->IsInCombat()) return true;
    if (auto* group = human->GetGroup())
        for (auto* ref = group->GetFirstMember(); ref; ref = ref->next())
            if (auto* member = ref->GetSource(); member && member->GetMap() == human->GetMap() && member->IsInCombat())
                return true;
    return false;
}
}
#endif
