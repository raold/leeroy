// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "MemberDungeonJob.h"
#include "GuildPorts.h"
#include "OfflineRaidJob.h"
#include "MemberDungeonReceipts.h"
#include "Realm/DayPlans.h"
#include "Config.h"

namespace Leeroy::Guild
{
namespace
{
bool enabled = false;

LeeroyCommandResult Start(nlohmann::json const& message)
{
    std::string error;
    auto command = ParseMemberDungeon(message, error);
    if (!command) return {false, error};
    if (message.value("type",std::string{}) == "start_member_dungeon")
        if (auto replay = MemberDungeonStartReplay(message.at("id").get<std::string>(),*command)) return *replay;
    auto roster = command->members;
    roster.push_back(command->leaderGuid);
    Realm::DayOwnerCheck check(message, std::move(roster), "heroic");
    if (!check.error.empty()) return {false, check.error};
    if (!enabled) return {false, "off"};
    if (!check.Checking() && !message.contains("day_prepared")) return {false,"dungeon_prepare_required"};
    return StartRaidJob(message.at("id").get<std::string>(), *command, RaidJobMode::MemberDungeon, {}, &check);
}

std::string Target(nlohmann::json const& message)
{
    std::string error;
    return ParseDungeonTarget(message,error).value_or("");
}
}

void ReloadMemberDungeon()
{
    bool const previous = enabled;
    enabled = sConfigMgr->GetOption<bool>("Leeroy.MemberDungeon.Enable", false);
#if LEEROY_HAS_DUNGEON_CLEAR
    auto& module = LeeroyModule::Instance();
    if (enabled != previous && module.Bridge().Connected())
        module.Publish(EncodeEvent(NowUnixMs(),{{"type","member_dungeon_capability"},{"enabled",enabled},
            {"guild",module.GuildId()},{"human",HumanGuid()}}),false);
#else
    (void)previous;
#endif
}
bool MemberDungeonEnabled() { return enabled; }

void RegisterMemberDungeon()
{
    RegisterCommand("start_member_dungeon", Start);
    Realm::RegisterDayOwnerCheck("start_member_dungeon", Start);
    RegisterCommand("member_dungeon_inspect", Start);
    RegisterCommand("member_dungeon_prepare_driver", Start);
    RegisterCommand("member_dungeon_stop", [](auto const& message) {
        auto id = Target(message);
        return id.empty() ? LeeroyCommandResult{false, "bad field: start_id"} : StopMemberDungeon(id);
    });
    RegisterCommand("member_dungeon_status", [](auto const& message) {
        auto id = Target(message);
        if (id.empty()) return LeeroyCommandResult{false, "bad field: start_id"};
        return PublishDungeonReceipt(id);
    });
}
}
