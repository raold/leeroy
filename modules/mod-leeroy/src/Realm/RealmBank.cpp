// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "RealmBank.h"
#include "RealmPorts.h"
#include "LeeroyRealmBankProof.h"
#include "Bag.h"
#include "GuildScript.h"
#include "PlayerScript.h"
#include "World.h"
#include "WorldSession.h"
#include <algorithm>
#include <array>
#include <limits>

namespace Leeroy::Realm
{
namespace
{
using Json = nlohmann::json;
BankItemFact ItemFact(Item const* item)
{
    return item ? BankItemFact{item->GetGUID().GetRawValue(), item->GetOwnerGUID().GetRawValue(),
        item->GetEntry(), item->GetCount(), item->GetMaxStackCount(), item->GetItemRandomPropertyId()} : BankItemFact{};
}
struct TabFact
{
    std::string name, icon;
    bool operator==(TabFact const&) const = default;
};
// Guild has no public tab/item getter in this installed core. This const-only
// view uses a protected pointer-to-member on the actual Guild object. It never
// casts a Guild to a derived object, exposes a native pointer, or changes core
// state. The private BankTab type is consumed solely through its public API.
class GuildBankReadView : public ::Guild
{
public:
    static bool Read(::Guild const& guild, BankSnapshot& result, std::vector<TabFact>& metadata)
    {
        auto const& tabs = guild.*&GuildBankReadView::m_bankTabs;
        if (tabs.size() > GUILD_BANK_MAX_TABS) return false;
        result.tabs = static_cast<std::uint32_t>(tabs.size());
        metadata.reserve(tabs.size());
        for (std::size_t tab = 0; tab < tabs.size(); ++tab)
        {
            metadata.push_back({tabs[tab].GetName(), tabs[tab].GetIcon()});
            for (std::uint8_t slot = 0; slot < GUILD_BANK_MAX_SLOTS; ++slot)
                result.slots.push_back({{true, static_cast<std::uint8_t>(tab), slot}, ItemFact(tabs[tab].GetItem(slot))});
        }
        return true;
    }
};
static_assert(GUILD_BANK_MAX_TABS == 6 && GUILD_BANK_MAX_SLOTS == 98 && MAX_BAG_SIZE == 36);
struct NativeSnapshot
{
    BankSnapshot items;
    std::vector<TabFact> tabs;
    bool valid = false;
};
NativeSnapshot Snapshot(::Guild const* guild, Player* player)
{
    NativeSnapshot result;
    if (!guild || !player) return result;
    result.items.slots.reserve(kBankSnapshotSlots);
    result.items.bankMoney = guild->GetTotalBankMoney(); result.items.playerMoney = player->GetMoney();
    // Root inventory includes equipment, personal-bank, keyring and currency
    // slots. Eleven equipped bag containers cover bags and the personal bank.
    for (std::uint8_t slot = 0; slot < CURRENCYTOKEN_SLOT_END; ++slot)
        result.items.slots.push_back({{false, INVENTORY_SLOT_BAG_0, slot},
            ItemFact(player->GetItemByPos(INVENTORY_SLOT_BAG_0, slot))});
    constexpr std::array<std::pair<std::uint8_t, std::uint8_t>, 2> bagRanges{{
        {INVENTORY_SLOT_BAG_START, INVENTORY_SLOT_BAG_END}, {BANK_SLOT_BAG_START, BANK_SLOT_BAG_END}}};
    for (auto const [start, end] : bagRanges)
        for (std::uint8_t bag = start; bag < end; ++bag)
            if (auto* container = player->GetBagByPos(bag))
            {
                if (container->GetBagSize() > MAX_BAG_SIZE) return result;
                for (std::uint8_t slot = 0; slot < container->GetBagSize(); ++slot)
                    result.items.slots.push_back({{false, bag, slot}, ItemFact(player->GetItemByPos(bag, slot))});
            }
    result.valid = GuildBankReadView::Read(*guild, result.items, result.tabs) && ValidBankSnapshot(result.items);
    return result;
}
bool InBags(BankPosition const& position)
{
    return !position.bank && ((position.container == INVENTORY_SLOT_BAG_0
        && position.slot >= INVENTORY_SLOT_ITEM_START && position.slot < INVENTORY_SLOT_ITEM_END)
        || (position.container >= INVENTORY_SLOT_BAG_START && position.container < INVENTORY_SLOT_BAG_END));
}
std::string Eligible(std::uint32_t guid, std::string_view operation, bool officer)
{
    if (!IsCast(guid)) return officer ? "not_officer" : "not_cast";
    if (auto why = AdmissionReason(guid, Purpose::Bank, operation); !why.empty()) return why;
    auto* player = FindOnline(guid);
    auto* guild = sGuildMgr->GetGuildById(GuildId());
    auto const* member = guild ? guild->GetMember(PlayerGuid(guid)) : nullptr;
    if (!player || !member || player->GetGuildId() != GuildId()) return officer ? "not_officer" : "not_member";
    if (officer && member->GetRankId() > GR_OFFICER) return "not_officer";
    if (player->IsBeingTeleported() || player->duel || player->IsNonMeleeSpellCast(false)) return "busy:transition";
    return "";
}
std::string Admit(Queued const& entry, std::uint32_t guid)
{
    return Eligible(guid, entry.id, entry.kind == "bank_stock");
}
std::uint32_t BankWhitelistLimit(std::uint32_t entry)
{
    auto const& items = RealmModule::Instance().Items();
    auto const found = std::find_if(items.begin(), items.end(),
        [&](ItemRule const& rule) { return rule.itemId == entry && rule.use == "bank"; });
    return found == items.end() ? 0 : found->maxCount;
}
struct NativeMove
{
    ::Guild* guild;
    Player* player;
    std::uint32_t guid;
    BankTransfer transfer;
    BankItemFact source;
    BankMoveReceipt receipt;
};
thread_local NativeMove* moveReceipt = nullptr;
struct MoneyReceipt
{
    Player* player;
    std::int32_t expected;
    std::uint32_t calls = 0;
    bool exact = true;
};
thread_local MoneyReceipt* moneyReceipt = nullptr;
struct CreationReceipt
{
    Player* player;
    std::uint32_t entry, count, calls = 0;
    Item const* returned = nullptr;
    BankItemFact item{};
    bool exact = true;
};
thread_local CreationReceipt* creationReceipt = nullptr;
template <class T> class ReceiptScope
{
public:
    ReceiptScope(T*& slot, T& value) : _slot(slot), _previous(slot)
    {
        if (_previous) _previous->receipt.exact = false;
        _slot = &value;
    }
    ~ReceiptScope() { _slot = _previous; }
private:
    T*& _slot;
    T* _previous;
};
// A money invocation cannot recursively replace an existing receipt.
bool ChangeMoney(Player* player, std::int32_t amount, std::uint32_t expected)
{
    if (moneyReceipt) { moneyReceipt->exact = false; return false; }
    auto const guid = player->GetGUID().GetCounter();
    auto const generation = RealmModule::Instance().BodyGeneration(guid);
    MoneyReceipt receipt{player, amount}; moneyReceipt = &receipt;
    struct Clear { ~Clear() { moneyReceipt = nullptr; } } clear;
    bool const accepted = player->ModifyMoney(amount, false);
    return accepted && receipt.exact && receipt.calls == 1 && FindOnline(guid) == player
        && RealmModule::Instance().BodyGeneration(guid) == generation && player->GetMoney() == expected;
}
class BankPlayerScripts final : public PlayerScript
{
public:
    BankPlayerScripts() : PlayerScript("LeeroyRealmBankPlayerScripts", {
        PLAYERHOOK_ON_MONEY_CHANGED, PLAYERHOOK_ON_STORE_NEW_ITEM}) { }
    void OnPlayerMoneyChanged(Player* player, int32& amount) override
    {
        if (!moneyReceipt) return;
        if (player != moneyReceipt->player || amount != moneyReceipt->expected || ++moneyReceipt->calls != 1)
            moneyReceipt->exact = false;
    }
    void OnPlayerStoreNewItem(Player* player, Item* item, uint32 count) override
    {
        if (!creationReceipt) return;
        auto& receipt = *creationReceipt;
        if (player != receipt.player || !item || item->GetEntry() != receipt.entry || count != receipt.count
            || item->GetCount() != receipt.count || item->GetOwnerGUID() != player->GetGUID() || ++receipt.calls != 1)
        { receipt.exact = false; return; }
        receipt.returned = item; receipt.item = ItemFact(item);
    }
};
class BankGuildScripts final : public GuildScript
{
public:
    BankGuildScripts() : GuildScript("LeeroyRealmBankGuildScripts", {GUILDHOOK_ON_ITEM_MOVE, GUILDHOOK_ON_BANK_EVENT}) { }
    void OnItemMove(::Guild* guild, Player* player, Item* item, bool fromBank, uint8 fromContainer, uint8 fromSlot,
        bool toBank, uint8 toContainer, uint8 toSlot) override
    {
        if (!moveReceipt) return;
        auto& expected = *moveReceipt;
        expected.receipt.Move(guild == expected.guild && player == expected.player && ItemFact(item) == expected.source
            && BankPosition{fromBank, fromContainer, fromSlot} == expected.transfer.source
            && BankPosition{toBank, toContainer, toSlot} == expected.transfer.destination);
    }
    void OnBankEvent(::Guild* guild, uint8 kind, uint8 tab, ObjectGuid::LowType player,
        uint32 entry, uint16 count, uint8 destination) override
    {
        if (!moveReceipt) return;
        auto& expected = *moveReceipt;
        expected.receipt.Event(guild == expected.guild && player == expected.guid
            && kind == (expected.transfer.source.bank ? GUILD_BANK_LOG_WITHDRAW_ITEM : GUILD_BANK_LOG_DEPOSIT_ITEM)
            && tab == (expected.transfer.source.bank ? expected.transfer.source.container : expected.transfer.destination.container)
            && entry == expected.source.entry && count == expected.transfer.count && destination == 0);
    }
};
bool ExactQueuedBody(std::uint32_t guid, std::string_view operation, ::Guild* guild, Player* player)
{
    return FindOnline(guid) == player && sGuildMgr->GetGuildById(GuildId()) == guild
        && RealmModule::Instance().ValidateOwnedBody(guid, operation).empty();
}
BankTransferResult Transfer(::Guild* guild, Player* player, std::uint32_t guid, std::string_view operation,
    NativeSnapshot const& before, BankTransfer const& transfer)
{
    auto const* source = BankSlot(before.items, transfer.source);
    if (!before.valid || !source || moveReceipt || !ExactQueuedBody(guid, operation, guild, player))
        return BankTransferResult::Unknown;
    NativeMove receipt{guild, player, guid, transfer, source->item, {}};
    {
        ReceiptScope scope(moveReceipt, receipt);
        auto const bank = transfer.source.bank ? transfer.source : transfer.destination;
        auto const bags = transfer.source.bank ? transfer.destination : transfer.source;
        guild->SwapItemsWithInventory(player, transfer.source.bank, bank.container, bank.slot, bags.container, bags.slot,
            transfer.count == source->item.count ? 0 : transfer.count);
    }
    if (!ExactQueuedBody(guid, operation, guild, player)) return BankTransferResult::Unknown;
    auto const after = Snapshot(guild, player);
    if (!after.valid || before.tabs != after.tabs) return BankTransferResult::Unknown;
    return ProveBankTransfer(before.items, after.items, transfer, receipt.receipt);
}
std::optional<BankTransfer> Destination(NativeSnapshot const& snapshot, BankSlotFact const& source,
    bool toBank, std::uint8_t tab, std::uint32_t wanted, Player* player)
{
    // Prefer a compatible existing stack, then an empty slot. Explicit slots
    // prevent core's fallback swap path from moving an unrelated item.
    for (bool merge : {true, false})
        for (auto const& candidate : snapshot.items.slots)
        {
            if ((toBank ? (!candidate.position.bank || candidate.position.container != tab) : !InBags(candidate.position))
                || static_cast<bool>(candidate.item.guid) != merge) continue;
            if (merge && (candidate.item.entry != source.item.entry || candidate.item.property != source.item.property
                || candidate.item.maxStack != source.item.maxStack || candidate.item.count >= source.item.maxStack)) continue;
            auto const amount = std::min({wanted, source.item.count, source.item.maxStack - candidate.item.count,
                std::uint32_t(std::numeric_limits<std::uint16_t>::max())});
            if (!amount || (amount < source.item.count && source.item.property)) continue;
            if (!toBank)
            {
                // CanStoreNewItem checks exact entry/count and native bag
                // restrictions without cloning/mutating the bank source.
                ItemPosCountVec positions;
                if (player->CanStoreNewItem(candidate.position.container, candidate.position.slot, positions,
                    source.item.entry, amount) != EQUIP_ERR_OK || positions.size() != 1
                    || positions.front().pos != ((candidate.position.container << 8) | candidate.position.slot)
                    || positions.front().count != amount) continue;
            }
            return BankTransfer{source.position, candidate.position, amount, player->GetGUID().GetRawValue()};
        }
    return std::nullopt;
}
Json SaveSource(BankSlotFact const& source, std::uint32_t amount)
{
    return {source.position.bank, source.position.container, source.position.slot, source.item.guid, source.item.owner,
        source.item.entry, source.item.count, source.item.maxStack, source.item.property, amount};
}
BankSlotFact LoadSource(Json const& source)
{
    return {{source.at(0).get<bool>(), source.at(1).get<std::uint8_t>(), source.at(2).get<std::uint8_t>()},
        {source.at(3).get<std::uint64_t>(), source.at(4).get<std::uint64_t>(), source.at(5).get<std::uint32_t>(),
            source.at(6).get<std::uint32_t>(), source.at(7).get<std::uint32_t>(), source.at(8).get<std::int32_t>()}};
}
RealmStep Failure(Queued const& entry, std::string reason)
{
    return {entry.payload.value("moved", std::uint64_t{0}) ? StepState::Unknown : StepState::Refused, std::move(reason)};
}
RealmStep MoveItems(Queued& entry, std::uint32_t guid)
{
    if (auto why = Eligible(guid, entry.id, false); !why.empty()) return Failure(entry, why);
    auto* player = FindOnline(guid); auto* guild = sGuildMgr->GetGuildById(GuildId());
    auto before = Snapshot(guild, player);
    if (!before.valid) return Failure(entry, "bank_snapshot_unavailable");
    auto const tab = entry.payload.at("tab").get<std::uint8_t>();
    bool const deposit = entry.kind == "bank_deposit";
    if (tab >= before.items.tabs) return Failure(entry, "no_tab");
    auto const itemId = entry.payload.at("item_id").get<std::uint32_t>();
    if (!entry.payload.contains("sources"))
    {
        Json sources = Json::array();
        std::uint64_t available = 0;
        for (auto const& source : before.items.slots)
            if (source.item.entry == itemId && (deposit ? InBags(source.position)
                : (source.position.bank && source.position.container == tab))) available += source.item.count;
        auto requested = std::uint64_t(entry.payload.at("count").get<std::uint32_t>());
        if (!requested) requested = available;
        if (!requested || available < requested) return Failure(entry, "no_item");
        auto left = requested;
        for (auto const& source : before.items.slots)
            if (left && source.item.entry == itemId && (deposit ? InBags(source.position)
                : (source.position.bank && source.position.container == tab)))
            {
                auto const amount = static_cast<std::uint32_t>(std::min(left, std::uint64_t(source.item.count)));
                sources.push_back(SaveSource(source, amount)); left -= amount;
            }
        if (sources.size() > kBankManifestSlots) return Failure(entry, "bank_manifest_capacity");
        entry.payload["sources"] = std::move(sources); entry.payload["source_index"] = 0u;
        entry.payload["requested"] = requested; entry.payload["moved"] = std::uint64_t{0};
    }
    auto& sources = entry.payload.at("sources");
    auto index = entry.payload.at("source_index").get<std::size_t>();
    if (sources.empty() || sources.size() > kBankManifestSlots || index >= sources.size())
        return {StepState::Unknown, "bank_manifest_identity"};
    auto expected = LoadSource(sources.at(index));
    auto const* source = BankSlot(before.items, expected.position);
    auto const remaining = sources.at(index).at(9).get<std::uint32_t>();
    if (!source || source->item != expected.item || !remaining || remaining > source->item.count)
        return Failure(entry, "bank_source_changed");
    if (source->item.property && remaining < source->item.count)
        return Failure(entry, "bank_split_property_unsupported");
    auto transfer = Destination(before, *source, deposit, tab, remaining, player);
    if (!transfer) return Failure(entry, deposit ? "tab_full" : "bags_full");
    auto const result = Transfer(guild, player, guid, entry.id, before, *transfer);
    if (result == BankTransferResult::Unknown) return {StepState::Unknown, "bank_transfer_unproved"};
    if (result == BankTransferResult::Unchanged) return Failure(entry, "no_rights");
    auto const moved = entry.payload.at("moved").get<std::uint64_t>() + transfer->count;
    auto const requested = entry.payload.at("requested").get<std::uint64_t>();
    if (moved > requested) return {StepState::Unknown, "bank_progress_unproved"};
    entry.payload["moved"] = moved;
    sources.at(index).at(6) = expected.item.count - transfer->count;
    sources.at(index).at(9) = remaining - transfer->count;
    if (remaining == transfer->count) entry.payload["source_index"] = ++index;
    if (moved == requested && index == sources.size()) return {StepState::Complete, "", moved};
    if (index >= sources.size()) return {StepState::Unknown, "bank_progress_unproved"};
    return {StepState::Pending, "", moved};
}
RealmStep Stock(Queued& entry, std::uint32_t guid)
{
    if (auto why = Eligible(guid, entry.id, true); !why.empty()) return Failure(entry, why);
    auto const itemId = entry.payload.at("item_id").get<std::uint32_t>();
    auto const requested = entry.payload.at("count").get<std::uint32_t>();
    auto const limit = BankWhitelistLimit(itemId);
    if (!limit || requested > limit) return Failure(entry, "not_whitelisted");
    auto const* proto = sObjectMgr->GetItemTemplate(itemId);
    if (!proto || !proto->GetMaxStackSize() || proto->Bonding == BIND_WHEN_PICKED_UP
        || proto->Bonding == BIND_QUEST_ITEM || proto->Duration > 0) return Failure(entry, "not_whitelisted");
    auto* player = FindOnline(guid); auto* guild = sGuildMgr->GetGuildById(GuildId());
    auto const baseline = Snapshot(guild, player);
    if (!baseline.valid) return Failure(entry, "bank_snapshot_unavailable");
    auto const tab = entry.payload.at("tab").get<std::uint8_t>();
    if (tab >= baseline.items.tabs) return Failure(entry, "no_tab");
    auto const moved = entry.payload.value("moved", 0u);
    if (moved >= requested) return {StepState::Unknown, "bank_progress_unproved"};
    auto count = std::min(requested - moved, proto->GetMaxStackSize());
    // One created stack may be smaller than the template maximum to fill a
    // fragmented tab one exact destination at a time. No created stack is
    // subsequently split, so failed-move cleanup remains unambiguous.
    BankSlotFact proposed{{false, INVENTORY_SLOT_BAG_0, INVENTORY_SLOT_ITEM_START},
        {1, player->GetGUID().GetRawValue(), itemId, count, proto->GetMaxStackSize(), 0}};
    auto destination = Destination(baseline, proposed, true, tab, count, player);
    if (!destination) return Failure(entry, "tab_full");
    count = destination->count;
    // Stock must create exactly one fresh stack. Never merge new stock into
    // preexisting officer items, because a later refusal must destroy only it.
    std::optional<BankPosition> empty;
    ItemPosCountVec positions;
    for (auto const& slot : baseline.items.slots)
        if (InBags(slot.position) && !slot.item.guid)
        {
            ItemPosCountVec inventoryPositions;
            if (player->CanStoreNewItem(slot.position.container, slot.position.slot, inventoryPositions, itemId, count) == EQUIP_ERR_OK
                && inventoryPositions.size() == 1 && inventoryPositions.front().count == count
                && inventoryPositions.front().pos == ((slot.position.container << 8) | slot.position.slot))
            { empty = slot.position; positions = std::move(inventoryPositions); break; }
        }
    if (!empty) return Failure(entry, "bags_full");
    if (creationReceipt) { creationReceipt->exact = false; return Failure(entry, "bank_creation_busy"); }
    CreationReceipt creation{player, itemId, count};
    creationReceipt = &creation;
    Item* created = nullptr;
    {
        struct Clear { ~Clear() { creationReceipt = nullptr; } } clear;
        created = player->StoreNewItem(positions, itemId, true);
    }
    // The callback copies the exact native-created identity. Never dereference
    // a pointer returned after arbitrary scripts have run on the new item.
    auto const actual = creation.item;
    if (!ExactQueuedBody(guid, entry.id, guild, player)) return {StepState::Unknown, "bank_creation_identity"};
    auto const before = Snapshot(guild, player);
    if (!creation.exact || creation.calls != 1 || !created || created != creation.returned
        || !before.valid || baseline.tabs != before.tabs || actual.entry != itemId || actual.count != count
        || actual.owner != player->GetGUID().GetRawValue()
        || !ExactBankCreation(baseline.items, before.items, *empty, actual)
        || !Eligible(guid, entry.id, true).empty() || !RealmModule::Instance().ValidateOwnedBody(guid, entry.id).empty())
        return {StepState::Unknown, "bank_creation_unproved"};
    auto const* source = BankSlot(before.items, *empty);
    destination = Destination(before, *source, true, tab, count, player);
    BankTransferResult result = BankTransferResult::Unchanged;
    if (destination && destination->count == count) result = Transfer(guild, player, guid, entry.id, before, *destination);
    if (result == BankTransferResult::Complete)
    {
        entry.payload["moved"] = moved + count;
        return {moved + count == requested ? StepState::Complete : StepState::Pending, "", moved + count};
    }
    if (result == BankTransferResult::Unknown || !ExactQueuedBody(guid, entry.id, guild, player))
        return {StepState::Unknown, "bank_stock_unproved"};
    auto const now = Snapshot(guild, player);
    if (!now.valid || baseline.tabs != now.tabs
        || !CanDestroyBankCreation(baseline.items, now.items, *empty, actual))
        return {StepState::Unknown, "bank_stock_unproved"};
    // Identity, owner, position and full original count were just proved. This
    // is the only DestroyItem site; no entry-count search can authorize it.
    auto* stillCreated = player->GetItemByPos(empty->container, empty->slot);
    if (ItemFact(stillCreated) != actual || !Eligible(guid, entry.id, true).empty()
        || !RealmModule::Instance().ValidateOwnedBody(guid, entry.id).empty()) return {StepState::Unknown, "bank_cleanup_identity"};
    player->DestroyItem(empty->container, empty->slot, true);
    if (!ExactQueuedBody(guid, entry.id, guild, player)) return {StepState::Unknown, "bank_cleanup_identity"};
    auto const cleaned = Snapshot(guild, player);
    if (!cleaned.valid || cleaned.items != baseline.items || cleaned.tabs != baseline.tabs)
        return {StepState::Unknown, "bank_cleanup_unproved"};
    return Failure(entry, "no_rights");
}
std::uint32_t TabPrice(std::uint32_t tab)
{
    constexpr std::array<ServerConfigs, 6> costs{CONFIG_GUILD_BANK_TAB_COST_0, CONFIG_GUILD_BANK_TAB_COST_1,
        CONFIG_GUILD_BANK_TAB_COST_2, CONFIG_GUILD_BANK_TAB_COST_3, CONFIG_GUILD_BANK_TAB_COST_4, CONFIG_GUILD_BANK_TAB_COST_5};
    return tab < costs.size() ? sWorld->getIntConfig(costs[tab]) : 0;
}
bool SameOfficer(std::uint32_t guid, Player* player, ::Guild* guild, BodyIdentity const& identity,
    std::uint64_t epoch, std::uint32_t configuredGuild, std::uint64_t generation)
{
    auto& module = RealmModule::Instance();
    auto const current = Identity(guid);
    return module.On() && module.Epoch() == epoch && GuildId() == configuredGuild && module.BodyGeneration(guid) == generation
        && sGuildMgr->GetGuildById(configuredGuild) == guild && FindOnline(guid) == player
        && current && *current == identity && module.Holder(guid).empty() && module.MutationReason(guid).empty()
        && Eligible(guid, {}, true).empty();
}
LeeroyCommandResult BuyTab(BankTabBuyCmd const& command)
{
    if (auto why = Eligible(command.officerGuid, {}, true); !why.empty()) return {false, why};
    auto& module = RealmModule::Instance();
    auto* player = FindOnline(command.officerGuid); auto* guild = sGuildMgr->GetGuildById(GuildId());
    auto const identity = Identity(command.officerGuid);
    auto const epoch = module.Epoch(); auto const guildId = GuildId();
    auto const generation = module.BodyGeneration(command.officerGuid);
    if (!identity || !module.Holder(command.officerGuid).empty()) return {false, "busy:operation_identity"};
    auto const before = Snapshot(guild, player);
    if (!before.valid || before.items.tabs != command.tab || command.tab >= GUILD_BANK_MAX_TABS) return {false, "no_tab"};
    if (before.items.bankMoney > GUILD_BANK_MONEY_LIMIT) return {false, "money"};
    auto const price = TabPrice(command.tab);
    auto const trialLimit = player->GetSession()->IsTrialAccount() ? sWorld->getIntConfig(CONFIG_TRIAL_MONEY_CAP) : 0;
    if (!BankTabLoanAllowed(before.items.bankMoney, before.items.playerMoney, price, MAX_MONEY_AMOUNT, trialLimit)) return {false, "money"};
    if (!SameOfficer(command.officerGuid, player, guild, *identity, epoch, guildId, generation)) return {false, "busy:operation_identity"};
    auto transaction = CharacterDatabase.BeginTransaction();
    // Keep this original lane transaction local through the transient loan.
    // SQL order is guaranteed within a transaction, not between DB workers.
    // Every unknown return before submission drops only this unsubmitted SQL;
    // it does not pretend native partial effects were reversed or persisted.
    auto submitSettled = [&](NativeSnapshot const& settled)
    {
        return SubmitSettledBankTabTransaction(before.items.playerMoney, transaction,
            [&](std::uint32_t originalMoney)
            {
                if (!SameOfficer(command.officerGuid, player, guild, *identity, epoch, guildId, generation)) return false;
                auto const current = Snapshot(guild, player);
                return current.valid && current.items.playerMoney == originalMoney
                    && current.items == settled.items && current.tabs == settled.tabs;
            },
            [&](CharacterDatabaseTransaction const& original) { player->SaveGoldToDB(original); },
            [](CharacterDatabaseTransaction original) { CharacterDatabase.CommitTransaction(original); });
    };
    if (!guild->ModifyBankMoney(transaction, price, false)) return {false, "money"};
    if (!ChangeMoney(player, static_cast<std::int32_t>(price), before.items.playerMoney + price))
    {
        // Reverse the bank loan only when the officer demonstrably received
        // nothing and every other observed item/tab/money fact stayed exact.
        if (!SameOfficer(command.officerGuid, player, guild, *identity, epoch, guildId, generation))
            return {false, "bank_outcome_unknown"};
        auto const unchanged = Snapshot(guild, player);
        auto expected = before.items; expected.bankMoney -= price;
        if (!unchanged.valid || unchanged.items != expected || unchanged.tabs != before.tabs
            || !SameOfficer(command.officerGuid, player, guild, *identity, epoch, guildId, generation))
            return {false, "bank_outcome_unknown"};
        if (!guild->ModifyBankMoney(transaction, price, true) || !submitSettled(before))
            return {false, "bank_outcome_unknown"};
        return {false, "money"};
    }
    if (!SameOfficer(command.officerGuid, player, guild, *identity, epoch, guildId, generation)) return {false, "bank_outcome_unknown"};
    auto const loanSnapshot = Snapshot(guild, player);
    auto loanExpected = before.items; loanExpected.bankMoney -= price; loanExpected.playerMoney += price;
    if (!loanSnapshot.valid || loanSnapshot.items != loanExpected || loanSnapshot.tabs != before.tabs
        || TabPrice(command.tab) != price
        || !SameOfficer(command.officerGuid, player, guild, *identity, epoch, guildId, generation)) return {false, "bank_outcome_unknown"};
    if (moneyReceipt) return {false, "bank_outcome_unknown"};
    MoneyReceipt purchase{player, -static_cast<std::int32_t>(price)}; moneyReceipt = &purchase;
    {
        struct Clear { ~Clear() { moneyReceipt = nullptr; } } clear;
        guild->HandleBuyBankTab(player->GetSession(), static_cast<std::uint8_t>(command.tab));
    }
    bool const same = SameOfficer(command.officerGuid, player, guild, *identity, epoch, guildId, generation);
    if (!same) return {false, "bank_outcome_unknown"};
    auto const after = Snapshot(guild, player);
    auto expected = before.items; expected.bankMoney -= price; expected.tabs += 1;
    // New tab contains only empty native slots; all original items and tabs
    // remain exact. A money-only net result is never the purchase receipt.
    bool bought = same && purchase.exact && purchase.calls == 1 && after.valid
        && after.items.playerMoney == before.items.playerMoney && after.items.bankMoney == expected.bankMoney
        && after.items.tabs == before.items.tabs + 1 && after.tabs.size() == before.tabs.size() + 1
        && std::equal(before.tabs.begin(), before.tabs.end(), after.tabs.begin())
        && after.items.slots.size() == before.items.slots.size() + GUILD_BANK_MAX_SLOTS;
    if (bought)
    {
        bought = std::equal(before.items.slots.begin(), before.items.slots.end(), after.items.slots.begin())
            && std::all_of(after.items.slots.begin() + before.items.slots.size(), after.items.slots.end(),
                [&](BankSlotFact const& slot) { return slot.position.bank && slot.position.container == command.tab && !slot.item.guid; });
    }
    if (!bought)
    {
        auto loaned = before.items; loaned.bankMoney -= price; loaned.playerMoney += price;
        if (!same || !after.valid || !purchase.exact || purchase.calls || after.items != loaned || after.tabs != before.tabs)
            return {false, "bank_outcome_unknown"};
        if (!ChangeMoney(player, -static_cast<std::int32_t>(price), before.items.playerMoney))
            return {false, "bank_outcome_unknown"};
        if (!SameOfficer(command.officerGuid, player, guild, *identity, epoch, guildId, generation)) return {false, "bank_outcome_unknown"};
        if (!guild->ModifyBankMoney(transaction, price, true) || !submitSettled(before))
            return {false, "bank_outcome_unknown"};
        return {false, "no_tab"};
    }
    // Native purchase submits its own tab transaction but does not save gold.
    // Append only settled actual gold to the original bank-debit transaction
    // and submit it once. No queued write contains the temporary loan balance.
    if (!submitSettled(after)) return {false, "bank_outcome_unknown"};
    guild->HandleSetBankTabInfo(player->GetSession(), static_cast<std::uint8_t>(command.tab), command.name, command.icon);
    if (!SameOfficer(command.officerGuid, player, guild, *identity, epoch, guildId, generation)) return {false, "bank_outcome_unknown"};
    auto named = Snapshot(guild, player);
    auto names = after.tabs; names.back() = {command.name, command.icon};
    if (!named.valid || named.items != after.items || named.tabs != names
        || !SameOfficer(command.officerGuid, player, guild, *identity, epoch, guildId, generation)) return {false, "bank_outcome_unknown"};
    // Native in-memory completion and transaction submission only. The core's
    // async CommitTransaction is not a synchronous durable database receipt.
    return {true, ""};
}
LeeroyCommandResult CreditMoney(BankMoneyCmd const& command)
{
    auto& module = RealmModule::Instance();
    if (!module.On()) return {false, "realm_off"};
    auto const guildId = GuildId(); auto const epoch = module.Epoch();
    auto* guild = sGuildMgr->GetGuildById(guildId);
    if (!guild) return {false, "refused_guild"};
    auto const before = guild->GetTotalBankMoney();
    if (!BankGoldCreditAllowed(before, command.amount, GUILD_BANK_MONEY_LIMIT)) return {false, "money"};
    auto const copper = std::uint64_t(command.amount) * 10000;
    auto transaction = CharacterDatabase.BeginTransaction();
    if (!guild->ModifyBankMoney(transaction, copper, true)) return {false, "money"};
    CharacterDatabase.CommitTransaction(transaction);
    if (!module.On() || epoch != module.Epoch() || guildId != GuildId()
        || sGuildMgr->GetGuildById(guildId) != guild || guild->GetTotalBankMoney() != before + copper)
        return {false, "bank_outcome_unknown"};
    return {true, ""};
}
template <class Parse, class Fill>
void QueueHandler(char const* kind, Parse parse, Fill fill, RealmModule::Executor executor)
{
    RealmModule::Instance().RegisterExecutor(kind, std::move(executor), Admit);
    RegisterCommand(kind, [kind, parse, fill](Json const& message) -> LeeroyCommandResult
    {
        std::string error; auto command = parse(message, error);
        if (!command) return {false, error};
        Queued entry; entry.id = message.at("id").get<std::string>(); entry.kind = kind;
        fill(*command, entry);
        if (!RealmModule::Instance().Accept(std::move(entry), error)) return {false, error};
        return {true, ""};
    });
}
}
void RegisterRealmBankHandlers()
{
    RegisterCommand("bank_tab_buy", [](Json const& message) -> LeeroyCommandResult
    {
        std::string error; auto command = ParseBankTabBuy(message, error);
        if (!command) return {false, error};
        try { return BuyTab(*command); }
        catch (...) { return {false, "bank_outcome_unknown"}; }
    });
    RegisterCommand("bank_money", [](Json const& message) -> LeeroyCommandResult
    {
        std::string error; auto command = ParseBankMoney(message, error);
        if (!command) return {false, error};
        try { return CreditMoney(*command); }
        catch (...) { return {false, "bank_outcome_unknown"}; }
    });
    QueueHandler("bank_deposit", ParseBankDeposit, [](BankDepositCmd const& command, Queued& entry)
    { entry.guids = {command.guid}; entry.payload = {{"item_id", command.itemId}, {"count", command.count}, {"tab", command.tab}}; }, MoveItems);
    QueueHandler("bank_withdraw", ParseBankWithdraw, [](BankWithdrawCmd const& command, Queued& entry)
    { entry.guids = {command.guid}; entry.payload = {{"item_id", command.itemId}, {"count", command.count}, {"tab", command.tab}}; }, MoveItems);
    QueueHandler("bank_stock", ParseBankStock, [](BankStockCmd const& command, Queued& entry)
    { entry.guids = {command.officerGuid}; entry.payload = {{"item_id", command.itemId}, {"count", command.count}, {"tab", command.tab}}; }, Stock);
}
void AddRealmBankScripts() { new BankPlayerScripts(); new BankGuildScripts(); }
}
