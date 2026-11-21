// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_DAY_PLANS_H
#define MOD_LEEROY_REALM_DAY_PLANS_H
#include "RealmModule.h"
#include "LeeroyCommands.h"
namespace Leeroy::Realm
{
enum class DayHandoff;
// Optional control fields on the four actual higher-owner commands. A check runs admission only.
class DayOwnerCheck
{
public:
    DayOwnerCheck(nlohmann::json const& message, std::vector<std::uint32_t> members, std::string reason);
    bool Checking() const { return checking; }
    bool InspectingDungeon() const { return inspectingDungeon; }
    bool ProjectsDungeon(std::uint32_t guid) const;
    std::string Token(std::uint32_t guid) const;
    bool DriverPreparationCurrent() const;
    bool PreparingDriver() const { return preparingDriver; }
    LeeroyCommandResult Accept(bool coldDriver = false) const;
    std::string error;
private:
    bool checking = false, preparingDriver = false, inspectingDungeon = false;
    std::int64_t expires = 0;
    nlohmann::json original, binding;
    std::string checkId, why, heroic;
    std::vector<std::uint32_t> heroicMembers;
    std::vector<std::uint32_t> targets;
    std::set<std::string> tokens;
};
void RegisterDayPlans();
void InvalidateDayOwnerChecks();
bool HasPreparedOwner(std::uint32_t guid);
void CancelPreparedDungeon(std::vector<std::uint32_t> const& picks);
void RegisterDayOwnerCheck(std::string const& originalType, LeeroyCommandHandler handler);
void TickDayPlans();
std::string CheckDayStep(Queued const& entry);
std::string DayMutationRefusal(Queued const& entry);
void DayAdmitted(Queued const& entry);
void DayFinished(Queued const& entry, bool released, bool success);
bool DayRevoking(Queued const& entry);
std::string DayForMember(std::uint32_t guid);
std::string DayPreviewFence(std::string const& token, std::uint32_t guid);
bool DayOwnsMember(std::string const& token, std::uint32_t guid);
void DayMoveIssued(Queued const& entry, std::uint32_t map, float x, float y, float z);
bool DayMoving(std::string const& token, Player* bot);
Player* DayPartner(std::string const& token, std::uint32_t guid);
bool DayDueling(std::string const& token, std::string const& operation, Player* bot);
void RevokeDay(std::string const& token, std::string const& reason);
DayHandoff DayGuildAddProgress(std::set<std::string> const& tokens, std::int64_t deadline);
} // namespace Leeroy::Realm
#endif
