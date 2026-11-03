// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmMailHooks.h"
#include "TestHarness.h"
#include <array>
#include <vector>
using namespace Leeroy::Realm;

LEEROY_TEST(RealmMailHooks_InstalledDefaultEnabledNoOpsPermitBagAndMoneyMail)
{
    // These three installed providers register both boundaries through empty
    // PlayerScript enabled-hooks lists. The assembled bank observer is tested below.
    std::array<std::string_view,3> const money{"IndividualProgression","NaxxPlayerScript","OnyNaxxLogoutTeleport"};
    CHECK(MailHooksSupported(MailHookBoundary::MoneyChanged,money));
    std::array<std::string_view,4> const move{"IndividualProgression","NaxxPlayerScript","OnyNaxxLogoutTeleport","Player_Transmogrify"};
    CHECK(MailHooksSupported(MailHookBoundary::AfterMoveFromInventory,move));
    for (auto provider : money)
    {
        CHECK(MailProviderContract(MailHookBoundary::MoneyChanged,provider) == MailProviderEffect::NoEffect);
        CHECK(MailProviderContract(MailHookBoundary::AfterMoveFromInventory,provider) == MailProviderEffect::NoEffect);
    }
}
LEEROY_TEST(RealmMailHooks_AssembledBankMoneyObserverPermitsMoneyMailOnlyAtItsBoundary)
{
    std::array<std::string_view,4> const money{
        "IndividualProgression","NaxxPlayerScript","OnyNaxxLogoutTeleport","LeeroyRealmBankPlayerScripts"};
    CHECK(MailHooksSupported(MailHookBoundary::MoneyChanged,money));
    CHECK(MailProviderContract(MailHookBoundary::MoneyChanged,"LeeroyRealmBankPlayerScripts") == MailProviderEffect::ReceiptObserver);
    for (auto boundary : {MailHookBoundary::BeforeSend,MailHookBoundary::AfterMoveFromInventory,
        MailHookBoundary::ItemCreate,MailHookBoundary::ObjectConstruct,MailHookBoundary::ObjectDestruct})
    {
        CHECK(MailProviderContract(boundary,"LeeroyRealmBankPlayerScripts") == MailProviderEffect::Unsupported);
        std::array<std::string_view,1> const bank{"LeeroyRealmBankPlayerScripts"};
        CHECK(!MailHooksSupported(boundary,bank));
    }
    std::array<std::string_view,5> const dangerous{
        "IndividualProgression","NaxxPlayerScript","OnyNaxxLogoutTeleport","LeeroyRealmBankPlayerScripts","ForeignMoneyMutator"};
    CHECK(!MailHooksSupported(MailHookBoundary::MoneyChanged,dangerous));
}
LEEROY_TEST(RealmMailHooks_ForeignProviderCannotRideTheInstalledNoOpAllowance)
{
    std::array<std::string_view,4> const dangerous{"IndividualProgression","NaxxPlayerScript","OnyNaxxLogoutTeleport","ForeignMoneyOrItemMutator"};
    CHECK(!MailHooksSupported(MailHookBoundary::MoneyChanged,dangerous));
    CHECK(!MailHooksSupported(MailHookBoundary::AfterMoveFromInventory,dangerous));
    for (auto provider : {std::string_view{},std::string_view{"IndividualProgressionMoney"},std::string_view{"naxxplayerscript"}})
    {
        CHECK(MailProviderContract(MailHookBoundary::MoneyChanged,provider) == MailProviderEffect::Unsupported);
        CHECK(MailProviderContract(MailHookBoundary::AfterMoveFromInventory,provider) == MailProviderEffect::Unsupported);
    }
}
LEEROY_TEST(RealmMailHooks_ProviderEffectsRemainSpecificToTheirAuditedBoundary)
{
    CHECK(MailProviderContract(MailHookBoundary::BeforeSend,"AHBot_MailScript") == MailProviderEffect::ReceiverPolicy);
    CHECK(MailProviderContract(MailHookBoundary::AfterMoveFromInventory,"Player_Transmogrify") == MailProviderEffect::AppearanceRemoval);
    CHECK(MailProviderContract(MailHookBoundary::MoneyChanged,"Player_Transmogrify") == MailProviderEffect::Unsupported);
    CHECK(MailProviderContract(MailHookBoundary::AfterMoveFromInventory,"AHBot_MailScript") == MailProviderEffect::Unsupported);
    for (auto boundary : {MailHookBoundary::BeforeSend,MailHookBoundary::ItemCreate,MailHookBoundary::ObjectConstruct,MailHookBoundary::ObjectDestruct})
        for (auto provider : {"IndividualProgression","NaxxPlayerScript","OnyNaxxLogoutTeleport"})
            CHECK(MailProviderContract(boundary,provider) == MailProviderEffect::Unsupported);
}
LEEROY_TEST(RealmMailHooks_EmptyAndMaximumProviderSetsAreBounded)
{
    std::vector<std::string_view> providers(kMailBoundaryProviders,"IndividualProgression");
    CHECK(MailHooksSupported(MailHookBoundary::MoneyChanged,providers));
    providers.push_back("NaxxPlayerScript"); CHECK(!MailHooksSupported(MailHookBoundary::MoneyChanged,providers));
    CHECK(MailHooksSupported(MailHookBoundary::MoneyChanged,{}));
    CHECK(MailHooksSupported(MailHookBoundary::ItemCreate,{}));
}
