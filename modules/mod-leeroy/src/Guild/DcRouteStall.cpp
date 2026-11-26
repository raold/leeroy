// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "DcRouteStall.h"

#if !defined(LEEROY_HAS_DUNGEON_CLEAR)
#error "LEEROY_HAS_DUNGEON_CLEAR comes from mod-leeroy.cmake; re-run cmake"
#endif

#if LEEROY_HAS_DUNGEON_CLEAR
#include "AiObjectContext.h"
#include "PlayerbotAI.h"
#include "Playerbots.h"
#include "Ai/Dungeon/DungeonClear/DcApproachState.h"
#include "Ai/Dungeon/DungeonClear/DcValueKeys.h"

#include <string>
#endif

namespace Leeroy::Guild
{
bool DcRouteStalled([[maybe_unused]] Player* leader)
{
#if LEEROY_HAS_DUNGEON_CLEAR
    PlayerbotAI* ai = leader ? GET_PLAYERBOT_AI(leader) : nullptr;
    if (!ai) return false;
    auto* context = ai->GetAiObjectContext();
    auto* reason = context->GetValue<std::string&>(DcKey::StallReason);
    auto* approach = context->GetValue<DcApproachState&>(DcKey::ApproachState);
    return reason && approach && !reason->Get().empty() && !approach->Get().doorOwnsStallReason;
#else
    return false;
#endif
}
}
