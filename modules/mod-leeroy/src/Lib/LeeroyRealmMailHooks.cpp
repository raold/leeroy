// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmMailHooks.h"

namespace Leeroy::Realm
{
MailProviderEffect MailProviderContract(MailHookBoundary boundary, std::string_view provider)
{
    switch (boundary)
    {
        case MailHookBoundary::BeforeSend:
            // mod-ah-bot-plus/AuctionHouseBotScript.cpp: normal mail can be
            // vetoed by receiver policy, but this provider cannot delete its
            // attachments. The native adapter probes this policy first.
            return provider == "AHBot_MailScript" ? MailProviderEffect::ReceiverPolicy : MailProviderEffect::Unsupported;
        case MailHookBoundary::AfterMoveFromInventory:
            // mod-transmog/transmog_scripts.cpp: this exact observer removes
            // only the fake appearance record, preserving the Item lifetime.
            if (provider == "Player_Transmogrify") return MailProviderEffect::AppearanceRemoval;
            [[fallthrough]];
        case MailHookBoundary::MoneyChanged:
            // These installed mod-individual-progression PlayerScript classes
            // use an empty enabled-hooks list, which the core expands to all
            // hooks. IndividualPlayerProgression (IndividualProgressionPlayer.cpp),
            // NaxxPlayerScript (naxx40Scripts/custom_scripts_40.cpp), and
            // OnyNaxxLogoutTeleport (naxx40Scripts/instance_naxxramas.cpp)
            // override neither of these two methods. Both inherited methods
            // are empty in PlayerScript.h. Their other hooks remain unaudited
            // by this contract and receive no allowance here.
            if (provider == "IndividualProgression" || provider == "NaxxPlayerScript" || provider == "OnyNaxxLogoutTeleport")
                return MailProviderEffect::NoEffect;
            return MailProviderEffect::Unsupported;
        case MailHookBoundary::ItemCreate:
        case MailHookBoundary::ObjectConstruct:
        case MailHookBoundary::ObjectDestruct:
            return MailProviderEffect::Unsupported;
    }
    return MailProviderEffect::Unsupported;
}

bool MailHooksSupported(MailHookBoundary boundary, std::span<std::string_view const> providers)
{
    if (providers.size() > kMailBoundaryProviders) return false;
    for (auto provider : providers)
        if (MailProviderContract(boundary,provider) == MailProviderEffect::Unsupported) return false;
    return true;
}
}
