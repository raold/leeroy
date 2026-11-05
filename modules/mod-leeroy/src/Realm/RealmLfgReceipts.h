// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_NATIVE_REALM_LFG_RECEIPTS_H
#define MOD_LEEROY_NATIVE_REALM_LFG_RECEIPTS_H
#include "LeeroyRealmLfgReceipts.h"

namespace Leeroy::Realm
{
void AddRealmLfgReceiptScripts();
// World-thread only. A missing receipt never authorizes native shared leave.
std::optional<LfgApplicantReceipt> ReadLfgApplicants(std::uint64_t group, bool queued);
std::string BeginLfgApplicantCleanup(std::string const& operation, LfgApplicantReceipt const& receipt);
// Caller has validated, reserved, charged and marked the whole native closure.
// This only scopes the exact synchronous rolecheck-abort packet attribution.
void CancelLfgApplicants(LfgApplicantReceipt const& receipt);
bool VerifyLfgApplicantCleanup(std::string const& operation, std::uint64_t group, std::uint64_t join);
}
#endif
