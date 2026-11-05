// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_MAIL_PERSISTENCE_H
#define MOD_LEEROY_REALM_MAIL_PERSISTENCE_H
#include "LeeroyRealmMail.h"
namespace Leeroy::Realm
{
struct MailPersistedRow
{
    MailStagedHeader header;
    std::uint32_t expiry = 0, linkReceiver = 0;
    std::optional<MailSavedItem> item;
};
// Readback is keyed by the exact native-generated mail ID from the staged
// transaction. Empty/failed/missing/changed reads never prove persistence.
bool ProvePersistedMail(MailStagedHeader const& expected, std::uint32_t expiry,
    std::vector<MailSavedItem> const& attachments, std::vector<MailPersistedRow> const& rows);
}
#endif
