// This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

#include "DojoSettings.h"
#include "TestHarness.h"

#include <initializer_list>
#include <ostream>
#include <string>
#include <utility>

using namespace Dojo;

namespace Dojo
{
std::ostream& operator<<(std::ostream& out, RuntimeState state)
{
    return out << StateName(state);
}
}

namespace
{
Settings Training(std::string instance = "dojo-01")
{
    Settings s;
    s.enable = true;
    s.marker = std::string(kTrainingMarker);
    s.instance = std::move(instance);
    return s;
}
}

DOJO_TEST(Settings_DefaultsAreOff)
{
    Settings const s;
    CHECK(!s.enable);
    CHECK_EQ(PlanRuntime(s, false).state, RuntimeState::Off);
    CHECK_EQ(PlanRuntime(s, true).state, RuntimeState::Off);
}

DOJO_TEST(Settings_EnableZeroIsOffWhateverElseIsSet)
{
    Settings s = Training();
    s.enable = false;
    for (bool training : {false, true})
    {
        RuntimePlan const plan = PlanRuntime(s, training);
        CHECK_EQ(plan.state, RuntimeState::Off);
        CHECK_EQ(plan.reason, std::string("Dojo.Enable = 0"));
    }
}

DOJO_TEST(Settings_DisabledBuildRefusesEnable)
{
    RuntimePlan const plan = PlanRuntime(Training(), false);
    CHECK_EQ(plan.state, RuntimeState::Refused);
    CHECK(plan.reason.find("AC_ENABLE_DOJO") != std::string::npos);
}

DOJO_TEST(Settings_TrainingBuildNeedsTheMarker)
{
    for (char const* marker : {"", "leeroy-dojo", "LEEROY-DOJO-TRAINING", "leeroy-dojo-training "})
    {
        Settings s = Training();
        s.marker = marker;
        RuntimePlan const plan = PlanRuntime(s, true);
        CHECK_EQ(plan.state, RuntimeState::Refused);
        CHECK(plan.reason.find("Dojo.Marker") != std::string::npos);
    }
}

DOJO_TEST(Settings_TrainingBuildNeedsAnInstanceName)
{
    for (std::string const& bad : {std::string(), std::string("dojo 01"), std::string("../x"), std::string("d\xc3\xa9"),
             std::string(65, 'a')})
    {
        RuntimePlan const plan = PlanRuntime(Training(bad), true);
        CHECK_EQ(plan.state, RuntimeState::Refused);
        CHECK(plan.reason.find("Dojo.Instance") != std::string::npos);
        CHECK(bad.empty() || plan.reason.find(bad) == std::string::npos);   // the bad value is never echoed
    }
}

DOJO_TEST(Settings_TrainingBuildWithItsConfigurationIsArmed)
{
    RuntimePlan const plan = PlanRuntime(Training("dojo-01"), true);
    CHECK_EQ(plan.state, RuntimeState::Armed);
    CHECK_EQ(plan.reason, std::string("training instance dojo-01"));
    CHECK(PlanRuntime(Training(std::string(64, 'a')), true).state == RuntimeState::Armed);
    CHECK(PlanRuntime(Training("A.b_c:d-9"), true).state == RuntimeState::Armed);
}

DOJO_TEST(Settings_InstanceNamesFollowThePreflightPattern)
{
    CHECK(IsInstanceName("dojo-01"));
    CHECK(IsInstanceName("t1"));
    CHECK(!IsInstanceName(""));
    CHECK(!IsInstanceName("a/b"));
    CHECK(!IsInstanceName("a b"));
    CHECK(!IsInstanceName(std::string(65, 'x')));
    CHECK(!IsInstanceName(std::string("a\0b", 3)));
}

DOJO_TEST(Settings_StatusLineSaysNothingStarts)
{
    struct Case
    {
        Settings settings;
        bool training;
        char const* expected;
    };
    Settings off;
    Case const cases[] = {
        {off, false, "mod-dojo: off (Dojo.Enable = 0), disabled build; no endpoint, no bot control"},
        {off, true, "mod-dojo: off (Dojo.Enable = 0), training build; no endpoint, no bot control"},
        {Training(), true, "mod-dojo: armed (training instance dojo-01), training build; no endpoint, no bot control"},
    };
    for (Case const& c : cases)
        CHECK_EQ(StatusLine(PlanRuntime(c.settings, c.training), c.training), std::string(c.expected));
    std::string const refused = StatusLine(PlanRuntime(Training(), false), false);
    CHECK(refused.rfind("mod-dojo: refused (", 0) == 0);
    CHECK(refused.find("no endpoint, no bot control") != std::string::npos);
}

DOJO_TEST(Settings_StateNames)
{
    CHECK_EQ(StateName(RuntimeState::Off), std::string_view("off"));
    CHECK_EQ(StateName(RuntimeState::Refused), std::string_view("refused"));
    CHECK_EQ(StateName(RuntimeState::Armed), std::string_view("armed"));
}
