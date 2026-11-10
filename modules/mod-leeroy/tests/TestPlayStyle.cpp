// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyPlayStyle.h"
#include "LeeroyGuildCommands.h"
#include "TestHarness.h"
using namespace Leeroy::PlayStyle;

LEEROY_TEST(PlayStyle_StrictWireBounds)
{
    for (auto text : {"0:0", "0:1", "2:0", "3:1", "6:1"})
        CHECK(Parse(text) && Leeroy::Guild::BotDoParamAllowed("playstyle", text));
    for (auto text : {"1:0", "7:0", "-2:1", "02:1", "6:2", "6:1 ", "potions", "6:1,4:0"})
        CHECK(!Parse(text) && !Leeroy::Guild::BotDoParamAllowed("playstyle", text));
    CHECK(Leeroy::Guild::BotDoParamAllowed("restore", "playstyle"));
}
LEEROY_TEST(PlayStyle_OnlyAddsUseAndDamageHoldWithinBounds)
{
    for (unsigned seconds = 0; seconds < 256; ++seconds)
        for (bool active : {false, true})
            for (bool potions : {false, true})
                for (unsigned delay = 2; delay <= 6; ++delay)
                {
                    Values before{active, static_cast<std::uint8_t>(seconds), potions};
                    auto after = Plan(before, {static_cast<std::uint8_t>(delay), true}, true);
                    CHECK(after.potions);
                    if (active && seconds > 6) CHECK(after.wait == active && after.seconds == seconds);
                    else CHECK(after.wait && after.seconds >= 2 && after.seconds <= 6
                        && (!active || after.seconds >= seconds));
                    auto healer = Plan(before, {static_cast<std::uint8_t>(delay), false}, false);
                    CHECK(healer == before);
                }
}
LEEROY_TEST(PlayStyle_IndependentAdmissionFences)
{
    Facts good{true,true,true,true,false,false,false,false,false};
    CHECK(Refusal(good).empty());
    for (auto field : {&Facts::enabled,&Facts::owned,&Facts::alive,&Facts::quiet})
    { auto bad = good; bad.*field = false; CHECK(!Refusal(bad).empty()); }
    for (auto field : {&Facts::instance,&Facts::raid,&Facts::foreign,&Facts::pvp,&Facts::busy})
    { auto bad = good; bad.*field = true; CHECK(!Refusal(bad).empty()); }
}
LEEROY_TEST(PlayStyle_JournalRestoresExactBaselineAndKeepsCallerChanges)
{
    Values baseline{false,10,false};
    Values style = Plan(baseline,{4,true},true);
    Journal journal;
    journal.Note(baseline,style);
    CHECK(journal.Restore(style,"*") == baseline);
    CHECK(journal.Owns()); // Planning is not verification; a failed native write keeps the journal.
    Values caller{true,6,false};
    auto restore = journal.Restore(caller,"*");
    CHECK(restore.seconds == 6 && restore.wait && !restore.potions);
    journal.Restored("wait for attack");
    CHECK(!journal.Owns("wait for attack") && journal.Owns("wait for attack time"));
    journal.Restored("*");
    CHECK(!journal.Owns());
}
LEEROY_TEST(PlayStyle_ExplicitEqualReplacementMustUnwindBeforeNewOwnerWrites)
{
    Values baseline{false,10,false};
    auto style = Plan(baseline,{4,true},true);
    Journal journal;
    journal.Note(baseline,style);
    // Adapter's bot_do/Training boundary: unwind, verify, forget, then caller's same desired state.
    auto live = journal.Restore(style,"*");
    CHECK(live == baseline);
    journal.Restored("*");
    live = style;
    CHECK(journal.Restore(live,"*") == style);
}
LEEROY_TEST(PlayStyle_CleanupSeesPvpDuringCombatAndExactBodyLife)
{
    Facts f{true,true,true,false,false,false,false,false,false};
    CHECK(!CleanupDue(f,false,false));
    f.pvp = true;
    CHECK(CleanupDue(f,false,false));
    f.pvp = false;
    CHECK(CleanupDue(f,true,false));
    CHECK(CleanupDue(f,false,true));
    CHECK(SameLife({1,2,3},{1,2,3}));
    CHECK(!SameLife({1,2,3},{4,2,3}));
    CHECK(!SameLife({1,2,3},{1,4,3}));
    CHECK(!SameLife({1,2,3},{1,2,4}));
    CHECK(!SameLife({0,2,3},{0,2,3}));
}
