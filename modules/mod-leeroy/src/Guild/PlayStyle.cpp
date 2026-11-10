// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "PlayStyle.h"
#include "LeeroyPlayStyle.h"
#include "GuildPorts.h"
#include "OfflineRaidJob.h"
#include "Training/TrainingCommands.h"
#include "Realm/RealmPvp.h"
#include "LeeroyRealmCleanup.h"
#include "AiObjectContext.h"
#include "Battlefield.h"
#include "BattlefieldMgr.h"
#include "Log.h"
#include "Map.h"
#include "PlayerbotAI.h"
#include "Playerbots.h"
#include "WorldSession.h"
#include <map>
#include <vector>

namespace Leeroy::PlayStyle
{
namespace
{
struct Owned
{
    Player* player = nullptr;
    PlayerbotAI* ai = nullptr;
    WorldSession* session = nullptr;
    uint32 guild = 0, human = 0;
    Journal journal{};
};
std::map<uint32, Owned> journals; // World thread; neither another body nor an AI replacement inherits ownership.
std::uint64_t epoch = 0;
Life Identity(Player* player, PlayerbotAI* ai, WorldSession* session)
{
    return {reinterpret_cast<std::uintptr_t>(player), reinterpret_cast<std::uintptr_t>(ai),
        reinterpret_cast<std::uintptr_t>(session)};
}
bool Same(Owned const& saved, Player* bot)
{
    return SameLife(Identity(saved.player, saved.ai, saved.session),
        Identity(bot, GET_PLAYERBOT_AI(bot), bot->GetSession()));
}

bool Controlled(Player* bot, uint32 human)
{
    auto* ai = GET_PLAYERBOT_AI(bot);
    auto* master = ai ? ai->GetMaster() : nullptr;
    auto* group = bot->GetGroup();
    return ai && !IsSelfBot(bot) && (!master || master->GetGUID().GetCounter() == human)
        && (!group || (group->GetLeaderGUID().GetCounter() == human
            && group->IsMember(ObjectGuid::Create<HighGuid::Player>(human))));
}
bool Pvp(Player* bot)
{
    auto const session = reinterpret_cast<std::uintptr_t>(bot->GetSession());
    if (bot->duel || bot->InBattleground() || bot->InBattlegroundQueue()
        || Realm::RealmPvpArenaObserverOwns(bot->GetGUID().GetCounter(), session)) return true;
    Realm::WintergraspMemberFacts wg;
    wg.foreignHistory = Realm::RealmPvpForeignWintergraspHistory(bot->GetGUID().GetCounter(), session);
    wg.inZone = bot->GetZoneId() == 4197;
    wg.inBattlefieldGroup = bot->GetGroup() && bot->GetGroup()->isBFGroup();
    if (auto* field = sBattlefieldMgr->GetBattlefieldByBattleId(BATTLEFIELD_BATTLEID_WG))
        for (auto team : {TEAM_ALLIANCE, TEAM_HORDE})
        {
            wg.queued = wg.queued || field->GetPlayersQueueSet(team).contains(bot->GetGUID());
            wg.invited = wg.invited || field->GetInvitedPlayersMap(team).contains(bot->GetGUID());
            wg.inWar = wg.inWar || field->GetPlayersInWarSet(team).contains(bot->GetGUID());
        }
    return !Realm::WintergraspPrivateReason(wg).empty();
}
bool Busy(Player* bot)
{
    auto guid = bot->GetGUID().GetCounter();
    return Realm::RealmOwns(guid) || Guild::GroupHasRealmOwner(bot->GetGroup())
        || Guild::OfflineRaidOwns(guid) || Training::DummyTrialOwns(guid)
        || Training::TrainingDcOwns(guid) || Training::HasTrainingJournal(guid)
        || Training::TrainingCleanupPending() || Guild::OfflineTrainingCleanupPending();
}
Facts ReadFacts(Player* bot)
{
    Facts f;
    auto& module = LeeroyModule::Instance();
    f.enabled = module.Enabled() && module.Config().playStyleEnable;
    if (!bot) return f;
    f.owned = bot->GetGUID().GetCounter() != module.HumanGuid()
        && module.Cast().IsCast(bot->GetGUID().GetCounter()) && bot->GetGuildId() == module.GuildId();
    f.alive = bot->IsAlive();
    f.quiet = bot->GetMap() && !bot->IsInCombat() && !bot->IsBeingTeleported() && !bot->IsInFlight()
        && !bot->GetTradeData() && !bot->IsNonMeleeSpellCast(false);
    f.instance = bot->GetMap() && bot->GetMap()->IsDungeon(); // Includes every dungeon/raid encounter, even missing boss telemetry.
    f.raid = bot->GetGroup() && bot->GetGroup()->isRaidGroup();
    f.foreign = !Controlled(bot, module.HumanGuid());
    f.busy = Busy(bot);
    // Expensive PvP producer facts last, only on a body the cheap facts already admit.
    if (Refusal(f).empty()) f.pvp = Pvp(bot);
    return f;
}
std::optional<Values> Read(Player* bot)
{
    auto* ai = GET_PLAYERBOT_AI(bot);
    if (!ai) return {};
    auto* value = ai->GetAiObjectContext()->GetValue<uint8>("wait for attack time");
    if (!value) return {};
    return Values{ai->HasStrategy("wait for attack", BOT_STATE_COMBAT), value->Get(),
        ai->HasStrategy("potions", BOT_STATE_COMBAT)};
}
bool Write(Player* bot, Values desired)
{
    auto before = Read(bot);
    if (!before) return false;
    auto* ai = GET_PLAYERBOT_AI(bot);
    if (before->wait != desired.wait)
        ai->ChangeStrategy(desired.wait ? "+wait for attack" : "-wait for attack", BOT_STATE_COMBAT);
    if (before->seconds != desired.seconds)
        ai->GetAiObjectContext()->GetValue<uint8>("wait for attack time")->Set(desired.seconds);
    if (before->potions != desired.potions)
        ai->ChangeStrategy(desired.potions ? "+potions" : "-potions", BOT_STATE_COMBAT);
    return Read(bot) == desired;
}
}

void Reload() { ++epoch; } // Every config load invalidates queued style additions, including off/on pairs.
std::uint64_t Epoch() { return epoch; }
void Forget(uint32 guid) { journals.erase(guid); }
bool Owns(uint32 guid, std::string_view name)
{
    auto found = journals.find(guid);
    return found != journals.end() && found->second.journal.Owns(name);
}
bool Restore(Player* bot, std::string_view name)
{
    if (!bot) return false;
    auto found = journals.find(bot->GetGUID().GetCounter());
    if (found == journals.end() || !found->second.journal.Owns(name)) return true;
    auto& saved = found->second;
    if (!Same(saved, bot))
    {
        journals.erase(found); // Retired life: never write the replacement's baseline.
        return true;
    }
    if (bot->GetGuildId() != saved.guild || !Controlled(bot, saved.human)
        || Realm::RealmOwns(bot->GetGUID().GetCounter()) || Guild::GroupHasRealmOwner(bot->GetGroup())
        || bot->IsBeingTeleported()) return false;
    auto current = Read(bot);
    if (!current || !Write(bot, saved.journal.Restore(*current, name))) return false;
    saved.journal.Restored(name);
    if (!saved.journal.Owns()) journals.erase(found);
    return true;
}
std::string_view Apply(Player* bot, std::string_view param)
{
    auto request = Parse(param);
    if (!request) return "bad field: param";
    if (auto why = Refusal(ReadFacts(bot)); !why.empty()) return why;
    auto guid = bot->GetGUID().GetCounter();
    // A fresh desired-state request first unwinds our previous baseline, never stacks it.
    if (!Restore(bot)) return "playstyle_cleanup";
    auto* ai = GET_PLAYERBOT_AI(bot);
    auto current = Read(bot);
    if (!current || (request->potions && !ai->GetAiObjectContext()->GetStrategy("potions"))
        || (request->wait && !ai->GetAiObjectContext()->GetStrategy("wait for attack"))) return "unsupported";
    auto desired = Plan(*current, *request, !PlayerbotAI::IsTank(bot, true) && !PlayerbotAI::IsHeal(bot, true));
    if (desired == *current) return {};
    auto& saved = journals[guid];
    saved = {bot, ai, bot->GetSession(), bot->GetGuildId(), Guild::HumanGuid(), {}};
    saved.journal.Note(*current, desired); // Before the first mutation; failed writes retain cleanup.
    if (!Write(bot, desired)) { Restore(bot); return "playstyle_failed"; }
    bool const waitChanged = current->wait != desired.wait || current->seconds != desired.seconds;
    LOG_INFO("module.leeroy", "mod-leeroy: playstyle guid {} wait_added {} potions_added {}", guid,
        waitChanged ? unsigned(desired.seconds) : 0u, !current->potions && desired.potions ? 1 : 0);
    return {};
}
void Tick()
{
    std::vector<uint32> guids;
    for (auto const& [guid, saved] : journals) { (void)saved; guids.push_back(guid); }
    for (auto guid : guids)
    {
        auto* bot = Guild::FindOnline(guid);
        if (!bot) continue; // Logout forgets it; transient worldport keeps proof until the same body returns.
        auto const& saved = journals.at(guid);
        bool const changed = !Same(saved, bot);
        auto facts = ReadFacts(bot);
        // Cleanup must see PvP even when ordinary combat makes additions ineligible.
        // In particular, a duel can begin while its participant already owns a style.
        facts.pvp = Pvp(bot);
        // Ordinary combat does not undo the style it was prepared for. Off/raid/instance and ownership changes do.
        bool const roleChanged = GET_PLAYERBOT_AI(bot)
            && (PlayerbotAI::IsTank(bot, true) || PlayerbotAI::IsHeal(bot, true))
            && saved.journal.Owns("wait for attack time");
        if (CleanupDue(facts, changed, roleChanged))
            Restore(bot);
    }
}
}
