// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_MAIL_POLICY_H
#define MOD_LEEROY_REALM_MAIL_POLICY_H

#include "LeeroyRealmCommands.h"
#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace Leeroy::Realm
{
inline constexpr std::size_t kMailAttachments = 12, kMailBagSlots = 160;
struct MailTemplateFacts { std::uint32_t entry = 0, maxStack = 0, maxCount = 0; };
struct MailBagFacts
{
    std::uint32_t guid = 0, entry = 0, count = 0;
    bool transferable = false;
};
struct MailAttachment
{
    std::uint32_t entry = 0, count = 0, source = 0;
    bool operator==(MailAttachment const&) const = default;
};
struct MailSource
{
    std::uint32_t guid = 0, entry = 0, before = 0, after = 0;
    bool operator==(MailSource const&) const = default;
};
struct MailPlan { std::vector<MailAttachment> attachments; std::vector<MailSource> sources; };

// Quantities are conserved per exact source GUID. Fragmented bag stacks and
// nonstacking new gifts become separate native attachments, up to the core cap.
// Whitelist maxima apply to the aggregate new quantity of each entry, so
// repeated wire rows cannot bypass them. Bag transfers need no creation rule.
std::optional<MailPlan> PlanMail(MailSendCmd const&, std::vector<ItemRule> const&,
    std::vector<MailTemplateFacts> const&, std::vector<MailBagFacts> const&, std::string& error);
bool MailDebitAllowed(std::uint32_t before, std::uint32_t amount, std::uint32_t nativeCap);
bool MailDebitProved(std::uint32_t before, std::uint32_t after, std::uint32_t amount, std::uint32_t nativeCap);

struct MailSavedItem
{
    std::uint32_t guid = 0, entry = 0, owner = 0, count = 0;
    bool operator==(MailSavedItem const&) const = default;
};
struct MailStagedHeader
{
    std::uint32_t id = 0, sender = 0, receiver = 0, money = 0, deliverAt = 0;
    std::string subject, body;
    bool normal = false, stationeryDefault = false, noTemplate = false, hasBody = false, noCod = false;
    bool hasAttachments = false;
};
struct MailStagedLink { std::uint32_t mail = 0, item = 0, receiver = 0; };
struct MailStage
{
    std::vector<MailStagedHeader> headers;
    std::vector<MailStagedLink> links;
    std::vector<MailSavedItem> items;
    std::vector<std::uint32_t> inventoryDeletes;
    std::vector<std::uint32_t> inventorySaves;
    std::vector<std::pair<std::uint32_t, std::uint32_t>> gold; // character, copper
    bool unsupported = false;
};
// The native adapter normalizes the typed prepared statements in its own
// transaction. No query count, inventory delta or mailbox search is a receipt.
bool ProveMailStage(MailStage const&, MailSendCmd const&, MailPlan const&,
    std::vector<MailSavedItem> const& attachments, std::uint32_t human,
    std::uint32_t moneyAfter, std::uint32_t deliverAt);
}
#endif
