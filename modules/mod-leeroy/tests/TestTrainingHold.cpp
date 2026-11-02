// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyTrainingHold.h"
#include "LeeroyTrainingVocabulary.h"
#include "TestHarness.h"

using namespace Leeroy::Training;

LEEROY_TEST(Training_HoldDoesNotClaimAnExistingHumanPause)
{
    TrainingHold hold;
    BossHold boss{true, true, 41};
    hold.Enable(boss);
    hold.Observe(boss, 0);
    CHECK(!hold.ShouldResume(boss, 90000, true));
    hold.Observe({false, true, 41}, 90001);
    hold.Observe(boss, 91000);
    CHECK(!hold.ShouldResume(boss, 110999));
    CHECK(hold.ShouldResume(boss, 111000));
}

LEEROY_TEST(Training_HoldRequiresTheObservedBossAndActualResumeSuccess)
{
    TrainingHold hold;
    hold.Enable({});
    hold.Observe({true, true, 41}, 1000);
    CHECK(!hold.ShouldResume({true, true, 42}, 50000, true));
    CHECK(!hold.ShouldResume({true, false, 41}, 50000, true));
    CHECK(!hold.ShouldResume({false, true, 41}, 50000, true));
    CHECK(hold.ShouldResume({true, true, 41}, 21000));
    CHECK(hold.ShouldResume({true, true, 41}, 22000)); // A refused action must remain retryable.
    hold.Resumed();
    CHECK(!hold.ShouldResume({true, true, 41}, 23000, true));
    hold.Observe({true, true, 42}, 30000);
    CHECK(!hold.ShouldResume({true, true, 42}, 49999));
    CHECK(hold.ShouldResume({true, true, 42}, 50000));
}

LEEROY_TEST(Training_VocabularyEncodingIsCompleteOrExplicitlyTooLarge)
{
    Vocabulary data;
    data.id = "synthetic-request";
    data.cls = 6;
    data.probe = 1101;
    data.actions = {"frost strike", "obliterate"};
    data.spells = {"frost strike"};
    data.spellsChecked = true;
    auto encoded = EncodeVocabulary(data, 123);
    CHECK(encoded);
    auto parsed = nlohmann::json::parse(*encoded);
    CHECK_EQ(parsed.at("actions").size(), 2);
    CHECK_EQ(parsed.at("spells").size(), 1);
    CHECK(parsed.at("spells_checked").get<bool>());
    data.actions.push_back(std::string(Leeroy::kMaxLineBytes, 'x'));
    CHECK(!EncodeVocabulary(data, 123));
}
