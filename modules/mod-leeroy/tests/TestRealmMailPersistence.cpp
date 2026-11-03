// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmMailPersistence.h"
#include "TestHarness.h"
using namespace Leeroy::Realm;
namespace
{
MailStagedHeader Header() { return {7001,11001,11002,90,2000,"Delivery","A gift.",true,true,true,true,true,true}; }
std::vector<MailSavedItem> Items() { return {{901,41001,11002,2},{902,41002,11002,1}}; }
std::vector<MailPersistedRow> Rows()
{
    auto items = Items(); return {{Header(),26000,11002,items[1]},{Header(),26000,11002,items[0]}};
}
bool Proved(std::vector<MailPersistedRow> const& rows) { return ProvePersistedMail(Header(),26000,Items(),rows); }
}
LEEROY_TEST(RealmMailPersistence_AcknowledgementWithoutExactRowsCannotComplete)
{
    CHECK(!Proved({})); CHECK(Proved(Rows()));
    auto rows = Rows(); rows.pop_back(); CHECK(!Proved(rows));
    rows = Rows(); rows.push_back(rows.front()); CHECK(!Proved(rows));
    rows = Rows(); rows[1] = rows[0]; CHECK(!Proved(rows));
    rows = Rows(); rows[0].item.reset(); CHECK(!Proved(rows));
}
LEEROY_TEST(RealmMailPersistence_BindsExactNativeIdReceiverHeaderAndItemOwnership)
{
    auto rows = Rows(); rows[0].header.id++; CHECK(!Proved(rows));
    rows = Rows(); rows[0].header.sender++; CHECK(!Proved(rows));
    rows = Rows(); rows[0].header.receiver++; CHECK(!Proved(rows));
    rows = Rows(); rows[0].header.money++; CHECK(!Proved(rows));
    rows = Rows(); rows[0].header.subject = "Unrelated"; CHECK(!Proved(rows));
    rows = Rows(); rows[0].header.body = "Other"; CHECK(!Proved(rows));
    rows = Rows(); rows[0].header.deliverAt++; CHECK(!Proved(rows));
    rows = Rows(); rows[0].header.hasBody = false; CHECK(!Proved(rows));
    rows = Rows(); rows[0].header.noCod = false; CHECK(!Proved(rows));
    rows = Rows(); rows[0].header.stationeryDefault = false; CHECK(!Proved(rows));
    rows = Rows(); rows[0].expiry++; CHECK(!Proved(rows));
    rows = Rows(); rows[0].linkReceiver++; CHECK(!Proved(rows));
    rows = Rows(); rows[0].item->guid++; CHECK(!Proved(rows));
    rows = Rows(); rows[0].item->entry++; CHECK(!Proved(rows));
    rows = Rows(); rows[0].item->count++; CHECK(!Proved(rows));
    rows = Rows(); rows[0].item->owner++; CHECK(!Proved(rows));
}
LEEROY_TEST(RealmMailPersistence_EmptyMoneyMailStillNeedsExactIdAndNoAttachments)
{
    auto header = Header(); header.hasAttachments = false;
    std::vector<MailPersistedRow> rows{{header,26000,0,{}}};
    CHECK(ProvePersistedMail(header,26000,{},rows));
    rows[0].item = Items().front(); CHECK(!ProvePersistedMail(header,26000,{},rows));
    rows[0].item.reset(); rows[0].linkReceiver = 11002; CHECK(!ProvePersistedMail(header,26000,{},rows));
}
