// This file is part of leeroy, released under the GNU GPL v2. See LICENSE.
#include "TestHarness.h"
#include <cstdint>
#include <vector>

namespace OfflineTankTest
{
using ObjectGuid = std::uint32_t;
constexpr std::uint8_t MEMBER_FLAG_MAINTANK = 2;
struct Group
{
    struct MemberSlot { ObjectGuid guid; std::uint8_t flags = 0; };
    std::vector<MemberSlot> slots{{15, 0}, {16, 0}, {17, 0}};
    bool raid = true;
    unsigned writes = 0;
    bool isRaidGroup() const { return raid; }
    auto const& GetMemberSlots() const { return slots; }
    void SetGroupMemberFlag(ObjectGuid guid, bool apply, std::uint8_t flag)
    {
        if (!raid) return;
        bool found = false;
        for (auto const& slot : slots) found = found || slot.guid == guid;
        if (!found) return;
        // Match Group.cpp: uniqueness is cleared even for apply=false.
        for (auto& slot : slots) slot.flags &= ~flag;
        for (auto& slot : slots) if (slot.guid == guid && apply) slot.flags |= flag;
        ++writes;
    }
};
#include "OfflineRaidTank.inc"
}

LEEROY_TEST(OfflineRaidTank_AssignmentSurvivesOtherRosterMembers)
{
    using namespace OfflineTankTest;
    Group group;
    DesignateRaidTank(&group, 15);
    CHECK_EQ(group.slots[0].flags, MEMBER_FLAG_MAINTANK);
    CHECK_EQ(group.slots[1].flags, 0u);
    CHECK_EQ(group.slots[2].flags, 0u);
    CHECK_EQ(group.writes, 1u);
    DesignateRaidTank(&group, 15);
    CHECK_EQ(group.writes, 1u);
}

LEEROY_TEST(OfflineRaidTank_RestoresDriverAfterEncounterTankSwap)
{
    using namespace OfflineTankTest;
    Group group;
    group.SetGroupMemberFlag(16, true, MEMBER_FLAG_MAINTANK);
    DesignateRaidTank(&group, 15);
    CHECK_EQ(group.slots[0].flags, MEMBER_FLAG_MAINTANK);
    CHECK_EQ(group.slots[1].flags, 0u);
    CHECK_EQ(group.writes, 2u);
}

LEEROY_TEST(OfflineRaidTank_PartyAndMissingLeaderDoNotAcquireRaidFlags)
{
    using namespace OfflineTankTest;
    Group group;
    group.raid = false;
    DesignateRaidTank(&group, 15);
    CHECK_EQ(group.writes, 0u);
    group.raid = true;
    DesignateRaidTank(&group, 99);
    CHECK_EQ(group.writes, 0u);
    DesignateRaidTank(nullptr, 15);
}
