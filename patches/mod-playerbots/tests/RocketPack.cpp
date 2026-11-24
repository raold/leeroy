// Game boundaries only. The trigger, execution and equip methods come from the applied module.
#include <cstdint>
#include <iostream>
#include <stdexcept>

using uint32 = std::uint32_t;
using uint16 = std::uint16_t;
constexpr int DONE = 3;
constexpr int ITEM_GOBLIN_ROCKET_PACK = 49278;
constexpr int NPC_CANNONA = 36838, NPC_CANNONH = 36839;
constexpr int NULL_SLOT = 255, EQUIP_ERR_OK = 0;

void check(bool pass) { if (!pass) throw std::runtime_error("rocket pack regression"); }
struct InstanceScript
{
    int state = 0;
    int GetBossState(uint32 encounter) const { check(encounter == 2); return state; }
};
struct Item
{
    bool equipped = false;
    bool IsEquipped() const { return equipped; }
    uint16 GetPos() const { return 23; }
};
struct Player;
struct Unit
{
    bool friendly = true;
    bool IsFriendlyTo(Player*) const { return friendly; }
};
struct Player
{
    InstanceScript* instance = nullptr;
    Item* pack = nullptr;
    Unit* alliance = nullptr;
    Unit* horde = nullptr;
    uint32 map = 631;
    bool alive = true, combat = false, vehicle = false, casting = false;
    bool swapWorks = true;
    int equipResult = EQUIP_ERR_OK;
    int checked = 0, swapped = 0;
    InstanceScript* GetInstanceScript() const { return instance; }
    uint32 GetMapId() const { return map; }
    bool IsAlive() const { return alive; }
    bool IsInCombat() const { return combat; }
    bool GetVehicle() const { return vehicle; }
    bool IsNonMeleeSpellCast(bool) const { return casting; }
    Item* GetItemByEntry(int entry) const { check(entry == ITEM_GOBLIN_ROCKET_PACK); return pack; }
    Unit* FindNearestCreature(int entry, float radius) const
    {
        check(radius == 100.0f);
        return entry == NPC_CANNONA ? alliance : horde;
    }
    int CanEquipItem(int slot, uint16& destination, Item* item, bool swap)
    {
        ++checked;
        check(slot == NULL_SLOT && item == pack && swap);
        destination = 6;
        return equipResult;
    }
    void SwapItem(uint16 from, uint16 to)
    {
        ++swapped;
        check(from == 23 && to == 6);
        if (swapWorks) pack->equipped = true;
    }
};
struct PlayerbotAI { Player* bot; }; // no TellMaster: announcing from these methods cannot compile
struct Event {};
class IccGunshipRocketPackSetupTrigger
{
public:
    explicit IccGunshipRocketPackSetupTrigger(PlayerbotAI* ai) : bot(ai->bot) {}
    bool IsActive();
    Player* bot;
};
class IccGunshipRocketPackSetupAction
{
public:
    explicit IccGunshipRocketPackSetupAction(PlayerbotAI* ai) : bot(ai->bot), botAI(ai) {}
    bool Execute(Event);
    Item* FindRocketPack() const;
    bool EquipRocketPack();
    bool AcquireRocketPack() { ++acquired; return true; }
    Player* bot;
    PlayerbotAI* botAI;
    int acquired = 0;
};
#include "Trigger.inc"
#include "Execute.inc"
#include "Find.inc"
#include "Equip.inc"

int main()
{
    InstanceScript instance;
    Item pack;
    Unit cannon;
    Player bot;
    bot.instance = &instance;
    bot.pack = &pack;
    bot.horde = &cannon;
    PlayerbotAI ai{&bot};
    IccGunshipRocketPackSetupTrigger trigger(&ai);
    IccGunshipRocketPackSetupAction action(&ai);
    check(trigger.IsActive());
    bot.combat = true; // selected while idle, combat starts before Execute
    for (int tick = 0; tick < 100; ++tick)
    {
        check(!trigger.IsActive());
        check(!action.Execute({}));
    }
    check(bot.checked == 0 && bot.swapped == 0 && action.acquired == 0);
    bot.combat = false;
    instance.state = DONE; // friendly cannons persist after completion
    check(!trigger.IsActive() && !action.Execute({}));
    instance.state = 2; // FAIL: setup is available again after a wipe
    check(trigger.IsActive());
    bot.equipResult = 1;
    check(!action.Execute({}) && bot.swapped == 0);
    bot.equipResult = EQUIP_ERR_OK;
    bot.swapWorks = false; // core refuses the actual swap despite CanEquipItem
    check(!action.Execute({}) && !pack.equipped);
    bot.swapWorks = true;
    check(action.Execute({}) && pack.equipped);
    int const swaps = bot.swapped;
    for (int tick = 0; tick < 100; ++tick)
        check(!trigger.IsActive() && !action.Execute({}));
    check(bot.swapped == swaps);
    pack.equipped = false;
    for (bool* busy : {&bot.vehicle, &bot.casting})
    {
        *busy = true;
        check(!trigger.IsActive() && !action.Execute({}));
        *busy = false;
    }
    bot.alive = false;
    check(!trigger.IsActive());
    bot.alive = true;
    bot.map = 0;
    check(!trigger.IsActive());
    bot.map = 631;
    bot.instance = nullptr;
    check(!trigger.IsActive());
    bot.instance = &instance;
    bot.horde = nullptr;
    check(!trigger.IsActive());
    bot.alliance = &cannon;
    cannon.friendly = false;
    check(!trigger.IsActive());
    cannon.friendly = true;
    bot.pack = nullptr;
    check(trigger.IsActive() && action.Execute({}) && action.acquired == 1);
    bot.combat = true;
    check(!action.Execute({}) && action.acquired == 1);
    std::cout << "rocket pack production-method regressions passed\n";
}
