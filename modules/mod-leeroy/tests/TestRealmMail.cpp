// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmMail.h"
#include "TestHarness.h"
#include <limits>

using namespace Leeroy::Realm;
namespace
{
MailSendCmd Command()
{
    MailSendCmd command; command.fromGuid = 11001; command.subject = "Delivery"; command.body = "A gift.";
    command.money = 90; command.items = {{41001, 3, "bags"}, {41002, 2, "new"}}; return command;
}
MailPlan Plan()
{
    std::string error;
    auto plan = PlanMail(Command(), {{41002,"gift",20}}, {{41001,20,0},{41002,1,0}},
        {{901,41001,2,true},{902,41001,4,true}}, error);
    CHECK(plan.has_value()); CHECK(error.empty()); return plan.value_or(MailPlan{});
}
std::vector<MailSavedItem> Attachments() { return {{901,41001,11002,2},{903,41001,11002,1},{904,41002,11002,1},{905,41002,11002,1}}; }
MailStage Stage()
{
    MailStage stage;
    stage.headers = {{7001,11001,11002,90,2000,"Delivery","A gift.",true,true,true,true,true,true}};
    stage.links = {{7001,901,11002},{7001,903,11002},{7001,904,11002},{7001,905,11002}};
    stage.items = Attachments(); stage.items.push_back({902,41001,11001,3});
    stage.inventoryDeletes = {901}; stage.inventorySaves = {902}; stage.gold = {{11001,910}};
    return stage;
}
bool Proved(MailStage const& stage) { return ProveMailStage(stage, Command(), Plan(), Attachments(), 11002, 910, 2000); }
}
LEEROY_TEST(RealmMail_ExactSourceAllocationConservesFragmentedStacks)
{
    auto plan = Plan(); CHECK_EQ(plan.attachments.size(), 4u); CHECK_EQ(plan.sources.size(), 2u);
    CHECK((plan.sources[0] == MailSource{901,41001,2,0})); CHECK((plan.sources[1] == MailSource{902,41001,4,3}));
    CHECK((plan.attachments[0] == MailAttachment{41001,2,901}));
    CHECK((plan.attachments[1] == MailAttachment{41001,1,902}));
}
LEEROY_TEST(RealmMail_DuplicateRowsCannotSpendOneSourceTwiceOrBypassCreationMax)
{
    std::string error; auto command = Command(); command.items = {{41001,2,"bags"},{41001,2,"bags"}};
    CHECK(!PlanMail(command, {}, {{41001,20,0}}, {{901,41001,3,true}}, error)); CHECK_EQ(error,"no_item");
    command.items = {{41001,2,"new"},{41001,2,"new"}};
    CHECK(!PlanMail(command, {{41001,"gift",3}}, {{41001,20,0}}, {}, error)); CHECK_EQ(error,"not_whitelisted");
    CHECK(!PlanMail(command, {{41001,"gift",20}}, {{41001,20,3}}, {}, error)); CHECK_EQ(error,"not_whitelisted");
}
LEEROY_TEST(RealmMail_NewUsesOnlyGiftHolidayPrizeAndBagsCannotCreate)
{
    std::string error; auto command = Command(); command.items = {{41001,1,"new"}};
    CHECK(!PlanMail(command, {{41001,"bank",1000}}, {{41001,20,0}}, {}, error)); CHECK_EQ(error,"not_whitelisted");
    for (auto use : {"gift","holiday","prize"})
        CHECK(PlanMail(command, {{41001,use,1}}, {{41001,20,0}}, {}, error));
    command.items[0].from = "bags";
    CHECK(!PlanMail(command, {{41001,"gift",20}}, {{41001,20,0}}, {{901,41001,1,false}}, error));
    CHECK_EQ(error,"no_item");
    CHECK(PlanMail(command, {}, {{41001,20,0}}, {{901,41001,1,true}}, error));
}
LEEROY_TEST(RealmMail_NativeStackAndAttachmentCapsNeverTruncateQuantities)
{
    std::string error; auto command = Command(); command.items = {{41001,12,"new"}};
    auto plan = PlanMail(command, {{41001,"prize",20}}, {{41001,1,0}}, {}, error);
    CHECK(plan && plan->attachments.size() == kMailAttachments);
    command.items[0].count = 13;
    CHECK(!PlanMail(command, {{41001,"prize",20}}, {{41001,1,0}}, {}, error)); CHECK_EQ(error,"no_item");
    command.items[0].count = 20;
    plan = PlanMail(command, {{41001,"gift",20}}, {{41001,6,0}}, {}, error);
    CHECK(plan && plan->attachments.size() == 4); CHECK_EQ(plan->attachments.back().count,2u);
}
LEEROY_TEST(RealmMail_MoneyDebitChecksCoreCapSignedRangeAndExactResult)
{
    constexpr std::uint32_t cap = 2147483647;
    CHECK(MailDebitAllowed(cap,1000000,cap)); CHECK(MailDebitProved(100,10,90,cap));
    CHECK(!MailDebitAllowed(100,101,cap)); CHECK(!MailDebitAllowed(cap+1,0,cap));
    CHECK(!MailDebitAllowed(cap,1000001,cap)); CHECK(!MailDebitProved(100,9,90,cap));
    CHECK(!MailDebitAllowed(std::numeric_limits<std::uint32_t>::max(),0,cap));
}
LEEROY_TEST(RealmMail_ExactNativeStageBindsMailIdHeaderLinksSourcesAndGold)
{
    CHECK(Proved(Stage()));
    auto stage = Stage(); stage.headers[0].receiver = 11003; CHECK(!Proved(stage));
    stage = Stage(); stage.headers[0].money++; CHECK(!Proved(stage));
    stage = Stage(); stage.headers[0].subject = "Changed"; CHECK(!Proved(stage));
    stage = Stage(); stage.headers[0].deliverAt++; CHECK(!Proved(stage));
    stage = Stage(); stage.headers[0].stationeryDefault = false; CHECK(!Proved(stage));
    stage = Stage(); stage.headers[0].hasAttachments = false; CHECK(!Proved(stage));
    stage = Stage(); stage.links[1].mail++; CHECK(!Proved(stage));
    stage = Stage(); stage.links[1].item = 901; CHECK(!Proved(stage));
    stage = Stage(); stage.items[0].count--; CHECK(!Proved(stage));
    stage = Stage(); stage.items.back().owner = 11002; CHECK(!Proved(stage));
    stage = Stage(); stage.inventoryDeletes[0] = 990; CHECK(!Proved(stage));
    stage = Stage(); stage.inventorySaves.clear(); CHECK(!Proved(stage));
    stage = Stage(); stage.gold[0].second++; CHECK(!Proved(stage));
    stage = Stage(); stage.unsupported = true; CHECK(!Proved(stage));
}
LEEROY_TEST(RealmMail_VetoMissingAttachmentAndUnrelatedEqualDeltasAreNotReceipts)
{
    auto stage = Stage(); stage.headers.clear(); CHECK(!Proved(stage));
    stage = Stage(); stage.links.pop_back(); CHECK(!Proved(stage));
    stage = Stage(); stage.items[0].guid = 999; stage.links[0].item = 999; CHECK(!Proved(stage));
    stage = Stage(); stage.items.back().count++; stage.items[0].count--; CHECK(!Proved(stage));
    auto attachments = Attachments(); attachments[0].guid = 999;
    stage = Stage(); stage.items[0].guid = 999; stage.links[0].item = 999;
    CHECK(!ProveMailStage(stage,Command(),Plan(),attachments,11002,910,2000));
}
LEEROY_TEST(RealmMail_EmptyAndMoneyOnlyMailRequireExactNativeHeader)
{
    auto command = Command(); command.items.clear(); command.money = 0;
    MailStage stage; stage.headers = {{7002,11001,11002,0,2000,"Delivery","A gift.",true,true,true,true,true,false}};
    CHECK(ProveMailStage(stage,command,{}, {},11002,1000,2000));
    command.money = 90; stage.headers[0].money = 90; stage.gold = {{11001,910}};
    CHECK(ProveMailStage(stage,command,{}, {},11002,910,2000));
    stage.headers.push_back(stage.headers[0]); CHECK(!ProveMailStage(stage,command,{}, {},11002,910,2000));
}
