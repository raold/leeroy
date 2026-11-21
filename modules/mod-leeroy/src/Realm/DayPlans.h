// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_DAY_PLANS_H
#define MOD_LEEROY_REALM_DAY_PLANS_H
#include "RealmModule.h"
namespace Leeroy::Realm
{
enum class DayHandoff;
void RegisterDayPlans();
void TickDayPlans();
std::string CheckDayStep(Queued const& entry);
std::string DayMutationRefusal(Queued const& entry);
void DayAdmitted(Queued const& entry);
void DayFinished(Queued const& entry, bool released, bool success);
bool DayRevoking(Queued const& entry);
std::string DayForMember(std::uint32_t guid);
bool DayOwnsMember(std::string const& token, std::uint32_t guid);
void DayMoveIssued(Queued const& entry, std::uint32_t map, float x, float y, float z);
bool DayMoving(std::string const& token, Player* bot);
Player* DayPartner(std::string const& token, std::uint32_t guid);
bool DayDueling(std::string const& token, std::string const& operation, Player* bot);
void RevokeDay(std::string const& token, std::string const& reason);
DayHandoff DayGuildAddProgress(std::set<std::string> const& tokens, std::int64_t deadline);
} // namespace Leeroy::Realm
#endif
