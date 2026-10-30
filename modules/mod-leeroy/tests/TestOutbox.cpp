// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyOutbox.h"
#include "TestHarness.h"

#include <string>
#include <vector>

using namespace Leeroy;

LEEROY_TEST(Outbox_ControlBeforeEvents)
{
    Outbox ob;
    ob.PushEvent("e1", false);
    ob.PushControl("c1");
    std::vector<std::string> out;
    ob.PopBatch(out, 10, true);
    CHECK(out == std::vector<std::string>({"c1", "e1"}));
}

LEEROY_TEST(Outbox_EventsHeldUntilEnabled)
{
    Outbox ob;
    ob.PushEvent("e1", false);
    ob.PushControl("c1");
    std::vector<std::string> out;
    ob.PopBatch(out, 10, false);
    CHECK(out == std::vector<std::string>({"c1"}));
    CHECK_EQ(ob.EventCount(), 1u);
}

LEEROY_TEST(Outbox_DropsOldestDroppableFirst)
{
    Outbox ob(3);
    ob.PushEvent("boss1", false);
    ob.PushEvent("chat1", true);
    ob.PushEvent("death1", false);
    CHECK_EQ(ob.PushEvent("zone2", true), 1u);
    std::vector<std::string> out;
    ob.PopBatch(out, 10, true);
    CHECK(out == std::vector<std::string>({"boss1", "death1", "zone2"}));
    CHECK_EQ(ob.TotalDropped(), 1u);
}

LEEROY_TEST(Outbox_DropsOldestWhenNoneDroppable)
{
    Outbox ob(2);
    ob.PushEvent("a", false);
    ob.PushEvent("b", false);
    ob.PushEvent("c", false);
    std::vector<std::string> out;
    ob.PopBatch(out, 10, true);
    CHECK(out == std::vector<std::string>({"b", "c"}));
}

LEEROY_TEST(Outbox_PopBatchMaxAndClearControl)
{
    Outbox ob;
    for (int i = 0; i < 5; ++i)
        ob.PushEvent(std::to_string(i), false);
    ob.PushControl("x");
    ob.ClearControl();
    std::vector<std::string> out;
    CHECK_EQ(ob.PopBatch(out, 2, true), 2u);
    CHECK(out == std::vector<std::string>({"0", "1"}));
    CHECK_EQ(ob.EventCount(), 3u);
}
