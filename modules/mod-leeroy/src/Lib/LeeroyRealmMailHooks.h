// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_MAIL_HOOKS_H
#define MOD_LEEROY_REALM_MAIL_HOOKS_H
#include <cstddef>
#include <span>
#include <string_view>

namespace Leeroy::Realm
{
inline constexpr std::size_t kMailBoundaryProviders = 32;
enum class MailHookBoundary { BeforeSend, AfterMoveFromInventory, MoneyChanged, ItemCreate, ObjectConstruct, ObjectDestruct };
enum class MailProviderEffect { Unsupported, NoEffect, AppearanceRemoval, ReceiverPolicy, ReceiptObserver };

// Contracts cover an exact installed provider at one exact hook. An audited
// no-op on a PlayerScript boundary grants no authority on other boundaries.
// Behavior-changing provider updates require a new source audit.
MailProviderEffect MailProviderContract(MailHookBoundary, std::string_view provider);
bool MailHooksSupported(MailHookBoundary, std::span<std::string_view const> providers);
}
#endif
