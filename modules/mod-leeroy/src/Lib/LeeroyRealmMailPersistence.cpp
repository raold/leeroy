// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmMailPersistence.h"
#include <map>

namespace Leeroy::Realm
{
bool ProvePersistedMail(MailStagedHeader const& expected, std::uint32_t expiry,
    std::vector<MailSavedItem> const& attachments, std::vector<MailPersistedRow> const& rows)
{
    if (!expected.id || !expected.sender || !expected.receiver || !expected.normal || !expected.stationeryDefault
        || !expected.noTemplate || !expected.hasBody || !expected.noCod || !expiry
        || attachments.size() > kMailAttachments || rows.empty() || rows.size() > kMailAttachments
        || rows.size() != (attachments.empty() ? 1 : attachments.size())
        || expected.hasAttachments != !attachments.empty()) return false;
    std::map<std::uint32_t,MailSavedItem> remaining;
    for (auto const& item : attachments)
        if (!item.guid || !item.count || item.owner != expected.receiver || !remaining.emplace(item.guid,item).second) return false;
    for (auto const& row : rows)
    {
        auto const& header = row.header;
        if (header.id != expected.id || header.sender != expected.sender || header.receiver != expected.receiver
            || header.money != expected.money || header.deliverAt != expected.deliverAt || header.subject != expected.subject
            || header.body != expected.body || header.normal != expected.normal || header.stationeryDefault != expected.stationeryDefault
            || header.noTemplate != expected.noTemplate || header.hasBody != expected.hasBody || header.noCod != expected.noCod
            || header.hasAttachments != expected.hasAttachments || row.expiry != expiry) return false;
        if (attachments.empty())
        {
            if (row.item || row.linkReceiver) return false;
            continue;
        }
        if (!row.item || row.linkReceiver != expected.receiver) return false;
        auto found = remaining.find(row.item->guid);
        if (found == remaining.end() || found->second != *row.item) return false;
        remaining.erase(found);
    }
    return remaining.empty();
}
}
