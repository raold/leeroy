// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "TrainingBenchmarkLoot.h"
#include "LeeroyBenchmarkLoot.h"
#include "LeeroyModule.h"
#include "Creature.h"
#include "GameObject.h"
#include "Group.h"
#include "LootMgr.h"
#include "Map.h"
#include "Player.h"
#include "Playerbots.h"
#include "ScriptMgr.h"
#include <atomic>

namespace Leeroy::Training
{
namespace
{
std::atomic<bool> ready{false};
template<class T> std::uintptr_t Identity(T* object) { return reinterpret_cast<std::uintptr_t>(object); }
BenchmarkLootActor Actor(Player* player)
{
    BenchmarkLootActor out;
    if (!player || !player->IsInWorld() || player->IsBeingTeleported()) return out;
    out.guid = player->GetGUID().GetCounter();
    out.map = player->GetMapId();
    out.instance = player->GetInstanceId();
    out.player = Identity(player);
    out.ai = Identity(GET_PLAYERBOT_AI(player));
    out.session = Identity(player->GetSession());
    out.currentHuman = LeeroyModule::Instance().HumanGuid(); // Atomic; no mutable world-job access from map hooks.
    if (auto* group = player->GetGroup())
    {
        out.group = group->GetGUID().GetRawValue();
        if (group->GetMembersCount() <= 25)
            for (auto const& slot : group->GetMemberSlots()) out.roster.push_back(slot.guid.GetCounter());
    }
    return out;
}

BenchmarkReward Reward(Player* player, Loot* loot, BenchmarkRewardKind expected = BenchmarkRewardKind::Other)
{
    BenchmarkReward out;
    out.kind = expected;
    if (!player || !loot || !player->IsInWorld() || !player->GetMap()) return out;
    if (expected == BenchmarkRewardKind::Inventory || !loot->containerGUID.IsEmpty())
    {
        out.kind = BenchmarkRewardKind::Inventory;
        return out;
    }
    auto guid = loot->sourceWorldObjectGUID;
    if (guid.IsCreatureOrVehicle())
    {
        out.kind = BenchmarkRewardKind::Creature;
        if (auto* source = player->GetMap()->GetCreature(guid); source && &source->loot == loot)
        {
            out.map = source->GetMapId();
            out.instance = source->GetInstanceId();
            out.groupLow = source->GetLootRecipientGroupGUID();
            // Unit::Kill fills creature loot BEFORE assigning JustDied.
            out.exactLoot = expected == BenchmarkRewardKind::Other || expected == BenchmarkRewardKind::Creature;
        }
    }
    else if (guid.IsGameObject())
    {
        out.kind = BenchmarkRewardKind::GameObject;
        if (auto* source = player->GetMap()->GetGameObject(guid); source && &source->loot == loot)
        {
            out.map = source->GetMapId();
            out.instance = source->GetInstanceId();
            out.groupLow = source->GetLootRecipientGroupGUID();
            out.exactLoot = expected == BenchmarkRewardKind::Other || expected == BenchmarkRewardKind::GameObject;
        }
    }
    return out;
}
}

bool BenchmarkLootHooksReady() { return ready.load(); }

class BenchmarkLootScript : public MiscScript
{
public:
    BenchmarkLootScript() : MiscScript("LeeroyBenchmarkLootScript", {MISCHOOK_ON_AFTER_LOOT_TEMPLATE_PROCESS}) { }
    void OnAfterLootTemplateProcess(Loot* loot, LootTemplate const*, LootStore const& store, Player* owner,
        bool, bool, uint16) override
    {
        // Never erase inventory containers, prospecting, disenchanting, fishing or somebody else's reward table.
        auto kind = &store == &LootTemplates_Creature ? BenchmarkRewardKind::Creature
            : &store == &LootTemplates_Gameobject ? BenchmarkRewardKind::GameObject : BenchmarkRewardKind::Other;
        if (kind == BenchmarkRewardKind::Other || !loot) return;
        BenchmarkRewards().Observe(Actor(owner), Reward(owner, loot, kind), [loot]
        {
            // This hook precedes FillNotNormalLootFor and its automatic FFA currency storage.
            loot->items.clear();
            loot->quest_items.clear();
            loot->unlootedCount = 0;
        });
    }
};

class BenchmarkMoneyScript : public PlayerScript
{
public:
    BenchmarkMoneyScript() : PlayerScript("LeeroyBenchmarkMoneyScript", {PLAYERHOOK_ON_BEFORE_LOOT_MONEY}) { }
    void OnPlayerBeforeLootMoney(Player* player, Loot* loot) override
    {
        if (!loot) return;
        auto guid = player ? player->GetLootGUID() : ObjectGuid::Empty;
        auto kind = guid.IsCreatureOrVehicle() ? BenchmarkRewardKind::Creature
            : guid.IsGameObject() ? BenchmarkRewardKind::GameObject
            : guid.IsItem() ? BenchmarkRewardKind::Inventory : BenchmarkRewardKind::Other;
        BenchmarkRewards().Observe(Actor(player), Reward(player, loot, kind), [loot] { loot->gold = 0; });
    }
};
}

void AddSC_leeroy_benchmark_loot()
{
    new Leeroy::Training::BenchmarkLootScript();
    new Leeroy::Training::BenchmarkMoneyScript();
    Leeroy::Training::ready.store(true);
}
