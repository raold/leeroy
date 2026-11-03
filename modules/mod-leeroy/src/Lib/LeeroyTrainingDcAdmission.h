// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_TRAINING_DC_ADMISSION_H
#define MOD_LEEROY_TRAINING_DC_ADMISSION_H
#include "LeeroyTrainingRuns.h"

namespace Leeroy::Training
{
// New DC control/job ownership requires the selected leader and every current
// group slot to be free. Supply the authoritative holder query, without an
// enable, online, cast or activity-domain waiver. Existing exact cleanup does
// not capture a new manifest and must remain independent of this admission.
template<class Owns>
std::optional<RunManifest> CaptureUnownedDcRun(std::uint32_t leader,
    std::vector<std::uint32_t> members, Owns const& owns)
{
    if (!leader || members.empty() || members.size() > kMaxRunMembers || owns(leader)) return std::nullopt;
    for (auto guid : members)
        if (owns(guid)) return std::nullopt;
    return RunManifest::Capture(leader, std::move(members));
}
}
#endif
