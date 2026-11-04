// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "RealmMail.h"
#include "RealmPorts.h"
#include "LeeroyRealmMail.h"
#include "LeeroyRealmMailHooks.h"
#include "LeeroyRealmMailPersistence.h"
#include "Bag.h"
#include "DatabaseEnv.h"
#include "GameTime.h"
#include "Mail.h"
#include "MailScript.h"
#include "MiscScript.h"
#include "PlayerScript.h"
#include "PreparedStatement.h"
#include "QuestDef.h"
#include "QueryCallback.h"
#include "QueryResult.h"
#include "ScriptMgr.h"
#include "Transaction.h"
#include "WorldSession.h"
#include <algorithm>
#include <array>
#include <limits>
#include <map>
#include <memory>
#include <optional>
#include <variant>

namespace Leeroy::Realm
{
namespace
{
static_assert(MAX_MAIL_ITEMS == kMailAttachments);
constexpr std::size_t kMaxStatements = 64, kMaxItemFields = 256;
struct Source
{
    Item* item = nullptr;
    std::uint32_t guid = 0, entry = 0, count = 0, bagGuid = 0;
    std::uint8_t bag = 0, slot = 0;
    std::vector<std::uint32_t> fields;
};
struct Attempt
{
    nlohmann::json admitted;
    std::uint32_t guid = 0, human = 0, first = 0, mail = 0;
    std::uint64_t epoch = 0;
    bool mutated = false;
    std::optional<bool> committed;
    std::unique_ptr<TransactionCallback> commit;
    MailStagedHeader header;
    std::uint32_t expiry = 0;
    std::vector<MailSavedItem> attachments;
    std::string readbackSql;
    std::unique_ptr<QueryCallback> readback;
    std::optional<std::vector<MailPersistedRow>> persisted;
    // Exact detached objects remain here if staging stops before native mail
    // transfer. They are never restored into an arbitrary later inventory.
    std::vector<std::unique_ptr<Item>> escrow;
    // During/after a throwing native transfer ownership is uncertain. Do not
    // dereference or delete these pointers, including at module destruction.
    std::vector<Item*> uncertain;
};
// Receipt storage only. The shared module is the sole body/pin/queue owner.
// It bounds pending and retained reservations together by ActionQueue::Capacity.
std::map<std::string, Attempt> attempts;

template<class Script>
bool SupportedHooks(std::size_t hook, MailHookBoundary boundary)
{
    auto const& hooks = ScriptRegistry<Script>::EnabledHooks;
    if (hook >= hooks.size()) return true;
    if (hooks[hook].size() > kMailBoundaryProviders) return false;
    std::array<std::string_view,kMailBoundaryProviders> providers;
    std::size_t count = 0;
    for (auto const* script : hooks[hook])
    {
        if (!script) return false;
        providers[count++] = script->GetName();
    }
    return MailHooksSupported(boundary,std::span<std::string_view const>(providers.data(),count));
}

std::string BoundaryPolicy(MailSendCmd const& command, std::uint32_t human)
{
    // Every allowance is tied to one audited provider/method contract,
    // including inherited no-ops registered through default-enabled hooks.
    if (!SupportedHooks<MailScript>(MAILHOOK_ON_BEFORE_MAIL_DRAFT_SEND_MAIL_TO,MailHookBoundary::BeforeSend))
        return "mail_scripts_unsupported";
    bool const bags = std::any_of(command.items.begin(), command.items.end(), [](auto const& item) { return item.from == "bags"; });
    if (bags && !SupportedHooks<PlayerScript>(PLAYERHOOK_ON_AFTER_MOVE_ITEM_FROM_INVENTORY,MailHookBoundary::AfterMoveFromInventory))
        return "inventory_scripts_unsupported";
    if (command.money && !SupportedHooks<PlayerScript>(PLAYERHOOK_ON_MONEY_CHANGED,MailHookBoundary::MoneyChanged))
        return "money_scripts_unsupported";
    if (!command.items.empty() && (!SupportedHooks<MiscScript>(MISCHOOK_ON_ITEM_CREATE,MailHookBoundary::ItemCreate)
        || !SupportedHooks<MiscScript>(MISCHOOK_ON_CONSTRUCT_OBJECT,MailHookBoundary::ObjectConstruct)
        || !SupportedHooks<MiscScript>(MISCHOOK_ON_DESTRUCT_OBJECT,MailHookBoundary::ObjectDestruct))) return "item_scripts_unsupported";

    // AHBot_MailScript ignores the draft and only decides receiver policy. Its
    // MAIL_NORMAL path cannot request item deletion. Probe that policy before
    // any debit/detachment; SendMailTo still runs its ordinary native hook.
    MailDraft probe(command.subject, command.body);
    MailReceiver receiver(ObjectAccessor::FindConnectedPlayer(PlayerGuid(human)), human);
    MailSender sender(MAIL_NORMAL, command.fromGuid, MAIL_STATIONERY_DEFAULT);
    auto checked = MAIL_CHECK_MASK_HAS_BODY;
    auto delay = command.delayS;
    std::uint32_t expiration = 0;
    bool deleteItems = false, send = true;
    auto const& hooks = ScriptRegistry<MailScript>::EnabledHooks;
    if (MAILHOOK_ON_BEFORE_MAIL_DRAFT_SEND_MAIL_TO < hooks.size())
        for (auto* script : hooks[MAILHOOK_ON_BEFORE_MAIL_DRAFT_SEND_MAIL_TO])
            script->OnBeforeMailDraftSendMailTo(&probe, receiver, sender, checked, delay, expiration, deleteItems, send);
    if (!send || deleteItems || checked != MAIL_CHECK_MASK_HAS_BODY || delay != command.delayS || expiration)
        return "mail_policy";
    return "";
}

bool DeliveryQuest(Player* player, std::uint32_t entry)
{
    // ItemRemovedQuestCheck can change zone-dependent auras on an incomplete
    // delivery quest. Keep that additional native closure outside this lane.
    for (std::uint16_t slot = 0; slot < MAX_QUEST_LOG_SIZE; ++slot)
        if (auto const* quest = sObjectMgr->GetQuestTemplate(player->GetQuestSlotQuestId(slot)))
            for (auto required : quest->RequiredItemId)
                if (required == entry) return true;
    return false;
}
bool Transferable(Player* player, Item* item, std::uint32_t recipientAccount)
{
    if (!item || !item->GetTemplate() || item->GetOwnerGUID() != player->GetGUID()
        || item->IsInTrade() || item->IsRefundable() || item->IsBOPTradable() || item->IsWrapped()
        || item->m_lootGenerated || item->IsNotEmptyBag() || !item->CanBeTraded(true)
        || item->GetTemplate()->HasFlag(ITEM_FLAG_CONJURED) || item->GetUInt32Value(ITEM_FIELD_DURATION)
        || item->GetState() == ITEM_REMOVED || DeliveryQuest(player, item->GetEntry())) return false;
    if (item->IsBoundAccountWide() && item->IsSoulBound() && player->GetSession()->GetAccountId() != recipientAccount)
        return false;
    for (auto const& spell : item->GetTemplate()->Spells)
        if (spell.SpellId > 0 && spell.SpellTrigger == ITEM_SPELLTRIGGER_ON_NO_DELAY_USE) return false;
    return item->GetValuesCount() <= kMaxItemFields;
}
std::optional<std::vector<Source>> BagSources(Player* player, std::uint32_t account)
{
    std::vector<Source> result;
    auto add = [&](Item* item, std::uint8_t bag, std::uint8_t slot)
    {
        if (!Transferable(player, item, account)) return;
        Source source{item, item->GetGUID().GetCounter(), item->GetEntry(), item->GetCount(), 0, bag, slot, {}};
        if (auto const* container = item->GetContainer()) source.bagGuid = container->GetGUID().GetCounter();
        source.fields.reserve(item->GetValuesCount());
        for (std::uint16_t field = 0; field < item->GetValuesCount(); ++field) source.fields.push_back(item->GetUInt32Value(field));
        result.push_back(std::move(source));
    };
    for (std::uint8_t slot = INVENTORY_SLOT_ITEM_START; slot < INVENTORY_SLOT_ITEM_END; ++slot)
        add(player->GetItemByPos(INVENTORY_SLOT_BAG_0, slot), INVENTORY_SLOT_BAG_0, slot);
    for (std::uint8_t bag = INVENTORY_SLOT_BAG_START; bag < INVENTORY_SLOT_BAG_END; ++bag)
        if (auto const* container = player->GetBagByPos(bag))
        {
            if (container->GetBagSize() > MAX_BAG_SIZE) return std::nullopt;
            for (std::uint8_t slot = 0; slot < container->GetBagSize(); ++slot)
                add(player->GetItemByPos(bag, slot), bag, slot);
        }
    if (result.size() > kMailBagSlots) return std::nullopt;
    return result;
}
bool ExactSource(Player* player, Source const& source, std::uint32_t account)
{
    auto* item = player->GetItemByPos(source.bag, source.slot);
    if (item != source.item || !Transferable(player, item, account) || item->GetValuesCount() != source.fields.size()) return false;
    for (std::uint16_t field = 0; field < item->GetValuesCount(); ++field)
        if (item->GetUInt32Value(field) != source.fields[field]) return false;
    return true;
}
bool ExactClone(Item const* original, Item const* clone, std::uint32_t count)
{
    if (!clone || clone->GetEntry() != original->GetEntry() || clone->GetCount() != count
        || clone->GetValuesCount() != original->GetValuesCount() || clone->IsInWorld() || clone->IsInUpdateQueue()
        || !clone->GetOwnerGUID().IsEmpty() || !clone->GetText().empty() || !original->GetText().empty()) return false;
    // The native CloneItem intentionally copies only a subset of item fields.
    // Refuse a split if that would discard enchants, charges, random suffix,
    // durability, text or any other property. A whole stack keeps its object.
    for (std::uint16_t field = 0; field < original->GetValuesCount(); ++field)
    {
        if (field < OBJECT_FIELD_TYPE || field == ITEM_FIELD_STACK_COUNT
            || (field >= ITEM_FIELD_OWNER && field < ITEM_FIELD_CREATOR)) continue;
        if (clone->GetUInt32Value(field) != original->GetUInt32Value(field)) return false;
    }
    return true;
}

// We construct this subtype ourselves; there is no downcast of a core-owned
// transaction. Protected query access and public typed parameter access are
// the installed APIs. Inspection ends before an async worker owns the batch.
class MailTransaction final : public Transaction<CharacterDatabaseConnection>
{
    template<class T> static T Read(PreparedStatementBase const* statement, std::size_t index)
    { return std::get<T>(statement->GetParameters().at(index).data); }
public:
    MailStage Inspect(std::vector<Source> const& sources, std::uint32_t sender, std::uint32_t expiry) const
    {
        MailStage stage;
        if (m_queries.size() > kMaxStatements) { stage.unsupported = true; return stage; }
        try
        {
            for (auto const& query : m_queries)
            {
                if (query.type != SQL_ELEMENT_PREPARED) { stage.unsupported = true; break; }
                auto const* statement = std::get<PreparedStatementBase*>(query.element);
                if (!statement) { stage.unsupported = true; break; }
                auto const size = statement->GetParameters().size();
                switch (statement->GetIndex())
                {
                    case CHAR_INS_MAIL:
                    {
                        if (size != 14 || Read<std::uint32_t>(statement,9) != expiry) { stage.unsupported = true; break; }
                        stage.headers.push_back({Read<std::uint32_t>(statement,0), Read<std::uint32_t>(statement,4),
                            Read<std::uint32_t>(statement,5), Read<std::uint32_t>(statement,11), Read<std::uint32_t>(statement,10),
                            Read<std::string>(statement,6), Read<std::string>(statement,7),
                            Read<std::uint8_t>(statement,1) == MAIL_NORMAL,
                            Read<std::int8_t>(statement,2) == MAIL_STATIONERY_DEFAULT,
                            Read<std::uint16_t>(statement,3) == 0, Read<std::uint8_t>(statement,13) == MAIL_CHECK_MASK_HAS_BODY,
                            Read<std::uint32_t>(statement,12) == 0, Read<bool>(statement,8)});
                        break;
                    }
                    case CHAR_INS_MAIL_ITEM:
                        if (size != 3) { stage.unsupported = true; break; }
                        stage.links.push_back({Read<std::uint32_t>(statement,0),Read<std::uint32_t>(statement,1),Read<std::uint32_t>(statement,2)});
                        break;
                    case CHAR_REP_ITEM_INSTANCE:
                    case CHAR_UPD_ITEM_INSTANCE:
                    {
                        auto const owner = Read<std::uint64_t>(statement,1);
                        if (size != 14 || owner > std::numeric_limits<std::uint32_t>::max()) { stage.unsupported = true; break; }
                        stage.items.push_back({Read<std::uint32_t>(statement,13),Read<std::uint32_t>(statement,0),
                            static_cast<std::uint32_t>(owner),Read<std::uint32_t>(statement,4)});
                        break;
                    }
                    case CHAR_DEL_CHAR_INVENTORY_BY_ITEM:
                        if (size != 1) { stage.unsupported = true; break; }
                        stage.inventoryDeletes.push_back(Read<std::uint32_t>(statement,0));
                        break;
                    case CHAR_REP_INVENTORY_ITEM:
                    {
                        auto const item = Read<std::uint32_t>(statement,3);
                        auto found = std::find_if(sources.begin(), sources.end(), [&](auto const& source) { return source.guid == item; });
                        if (size != 4 || found == sources.end() || Read<std::uint64_t>(statement,0) != sender
                            || Read<std::uint32_t>(statement,1) != found->bagGuid || Read<std::uint8_t>(statement,2) != found->slot)
                        { stage.unsupported = true; break; }
                        stage.inventorySaves.push_back(item); break;
                    }
                    case CHAR_UDP_CHAR_MONEY:
                    {
                        auto const owner = Read<std::uint64_t>(statement,1);
                        if (size != 2 || owner > std::numeric_limits<std::uint32_t>::max()) { stage.unsupported = true; break; }
                        stage.gold.emplace_back(static_cast<std::uint32_t>(owner),Read<std::uint32_t>(statement,0)); break;
                    }
                    default: stage.unsupported = true; break;
                }
                if (stage.unsupported) break;
            }
        }
        catch (...) { stage.unsupported = true; }
        return stage;
    }
};

std::string Eligibility(Queued const& entry, std::uint32_t guid)
{
    if (!IsCast(guid)) return "not_cast";
    if (entry.guids != std::vector<std::uint32_t>{guid}) return "bad field: from_guid";
    std::string error;
    auto command = ParseMailSend(entry.payload, error);
    if (!command || command->fromGuid != guid) return command ? "bad field: from_guid" : error;
    auto* sender = FindOnline(guid);
    if (!sender) return "offline";
    if (sender->IsBeingTeleported() || sender->duel || sender->GetTradeData()) return "busy:transition";
    if (sender->IsNonMeleeSpellCast(false)) return "busy:casting";
    if (auto reason = BusyReason(Body(sender),Purpose::Mail); !reason.empty()) return "busy:" + reason;
    return "";
}

std::string ReadbackSql(std::uint32_t mail, MailSendCmd const& command, std::uint32_t moneyAfter,
    MailPlan const& plan, std::vector<Source> const& sources)
{
    // Only bounded native numeric identities/quantities enter SQL. Wire text is
    // compared against returned fields, never interpolated into a query.
    std::string sql = "SELECT m.id,m.sender,m.receiver,m.money,m.deliver_time,m.subject,m.body,"
        "m.messageType,m.stationery,m.mailTemplateId,m.checked,m.cod,m.has_items,m.expire_time,"
        "mi.item_guid,mi.receiver,ii.itemEntry,ii.owner_guid,ii.count FROM mail m "
        "LEFT JOIN mail_items mi ON mi.mail_id=m.id LEFT JOIN item_instance ii ON ii.guid=mi.item_guid WHERE m.id=";
    sql += std::to_string(mail);
    if (command.money)
        sql += " AND EXISTS (SELECT 1 FROM characters c WHERE c.guid=" + std::to_string(command.fromGuid)
            + " AND c.money=" + std::to_string(moneyAfter) + ")";
    for (auto const& use : plan.sources)
    {
        if (!use.after)
        {
            sql += " AND NOT EXISTS (SELECT 1 FROM character_inventory ri WHERE ri.item=" + std::to_string(use.guid) + ")";
            continue;
        }
        auto const& source = *std::find_if(sources.begin(),sources.end(),[&](auto const& row) { return row.guid == use.guid; });
        sql += " AND EXISTS (SELECT 1 FROM item_instance rs JOIN character_inventory ri ON ri.item=rs.guid WHERE rs.guid="
            + std::to_string(use.guid) + " AND rs.itemEntry=" + std::to_string(use.entry)
            + " AND rs.owner_guid=" + std::to_string(command.fromGuid) + " AND rs.count=" + std::to_string(use.after)
            + " AND ri.guid=" + std::to_string(command.fromGuid) + " AND ri.bag=" + std::to_string(source.bagGuid)
            + " AND ri.slot=" + std::to_string(source.slot) + ")";
    }
    sql += " ORDER BY mi.item_guid LIMIT 13";
    return sql;
}
std::vector<MailPersistedRow> ReadPersistedRows(QueryResult const& result)
{
    std::vector<MailPersistedRow> rows;
    if (!result || result->GetFieldCount() != 19 || result->GetRowCount() > kMailAttachments) return rows;
    rows.reserve(static_cast<std::size_t>(result->GetRowCount()));
    do
    {
        auto const* fields = result->Fetch();
        if (!fields) return {};
        for (std::size_t field = 0; field < 14; ++field) if (fields[field].IsNull()) return {};
        if (fields[5].Get<std::string_view>().size() > 64 || fields[6].Get<std::string_view>().size() > 512) return {};
        MailPersistedRow row;
        row.header = {fields[0].Get<std::uint32_t>(),fields[1].Get<std::uint32_t>(),fields[2].Get<std::uint32_t>(),
            fields[3].Get<std::uint32_t>(),fields[4].Get<std::uint32_t>(),fields[5].Get<std::string>(),fields[6].Get<std::string>(),
            fields[7].Get<std::uint8_t>() == MAIL_NORMAL,fields[8].Get<std::uint8_t>() == MAIL_STATIONERY_DEFAULT,
            fields[9].Get<std::uint16_t>() == 0,fields[10].Get<std::uint8_t>() == MAIL_CHECK_MASK_HAS_BODY,
            fields[11].Get<std::uint32_t>() == 0,fields[12].Get<bool>()};
        row.expiry = fields[13].Get<std::uint32_t>();
        if (!fields[14].IsNull())
        {
            for (std::size_t field = 15; field < 19; ++field) if (fields[field].IsNull()) return {};
            row.linkReceiver = fields[15].Get<std::uint32_t>();
            row.item = MailSavedItem{fields[14].Get<std::uint32_t>(),fields[16].Get<std::uint32_t>(),
                fields[17].Get<std::uint32_t>(),fields[18].Get<std::uint32_t>()};
        }
        rows.push_back(std::move(row));
    } while (result->NextRow());
    return rows;
}
RealmStep ReadResult(Queued const& entry, std::uint32_t guid, Attempt& attempt)
{
    auto& module = RealmModule::Instance();
    if (attempt.admitted != entry.payload || attempt.guid != guid || attempt.human != HumanGuid()
        || attempt.epoch != module.Epoch() || !module.On() || !module.ValidateOwnedBody(guid,entry.id).empty())
        return {StepState::Unknown,"mail_operation_identity"};
    if (!attempt.commit) return {StepState::Unknown,"mail_stage_unproved"};
    // InvokeIfReady is nonblocking. The callback records one immutable boolean
    // and never accesses a Player or mutates a native object.
    attempt.commit->InvokeIfReady();
    if (!attempt.committed) return {StepState::Pending,""};
    if (!*attempt.committed) return {StepState::Unknown,"mail_commit_failed"};
    // Installed MySQLConnection::CommitTransaction discards COMMIT's result.
    // AsyncCommit true therefore needs an independent exact-ID persisted read.
    if (!attempt.readback)
    {
        auto* const receipt = &attempt;
        attempt.readback = std::make_unique<QueryCallback>(CharacterDatabase.AsyncQuery(attempt.readbackSql));
        attempt.readback->WithCallback([receipt](QueryResult result)
        {
            // Immutable fact recording only; no native body/lifecycle access.
            try { receipt->persisted = ReadPersistedRows(result); }
            catch (...) { receipt->persisted = std::vector<MailPersistedRow>{}; }
        });
    }
    attempt.readback->InvokeIfReady();
    if (!attempt.persisted) return {StepState::Pending,""};
    if (!ProvePersistedMail(attempt.header,attempt.expiry,attempt.attachments,*attempt.persisted))
        return {StepState::Unknown,"mail_persistence_unproved"};
    auto const first = attempt.first;
    attempts.erase(entry.id);
    return {StepState::Complete,"",first};
}

RealmStep Send(Queued& entry, std::uint32_t guid)
{
    if (auto found = attempts.find(entry.id); found != attempts.end()) return ReadResult(entry,guid,found->second);
    if (auto why = Eligibility(entry,guid); !why.empty()) return {StepState::Refused,std::move(why)};
    if (auto why = AdmissionReason(guid,Purpose::Mail,entry.id); !why.empty()) return {StepState::Refused,std::move(why)};
    std::string error;
    auto command = ParseMailSend(entry.payload,error);
    if (!command) return {StepState::Refused,std::move(error)};
    auto const human = HumanGuid();
    auto const* recipient = sCharacterCache->GetCharacterCacheByGuid(PlayerGuid(human));
    if (!human || !recipient || human == guid) return {StepState::Refused,"no_receiver"};
    if (auto why = BoundaryPolicy(*command,human); !why.empty()) return {StepState::Refused,std::move(why)};
    auto* sender = FindOnline(guid);
    if (!sender) return {StepState::Refused,"offline"};
    auto const before = sender->GetMoney();
    if (!MailDebitAllowed(before,command->money,MAX_MONEY_AMOUNT)) return {StepState::Refused,"money"};
    auto const allSources = BagSources(sender,recipient->AccountId);
    if (!allSources) return {StepState::Refused,"no_item"};
    std::vector<MailBagFacts> bags;
    for (auto const& source : *allSources) bags.push_back({source.guid,source.entry,source.count,true});
    std::vector<MailTemplateFacts> templates;
    for (auto const& request : command->items)
    {
        if (std::any_of(templates.begin(),templates.end(),[&](auto const& row) { return row.entry == request.itemId; })) continue;
        auto const* proto = sObjectMgr->GetItemTemplate(request.itemId);
        if (!proto || proto->Class == ITEM_CLASS_CONTAINER || proto->Class == ITEM_CLASS_QUIVER) return {StepState::Refused,"no_item"};
        templates.push_back({request.itemId,proto->GetMaxStackSize(),proto->MaxCount > 0 ? static_cast<std::uint32_t>(proto->MaxCount) : 0});
    }
    auto plan = PlanMail(*command,RealmModule::Instance().Items(),templates,bags,error);
    if (!plan) return {StepState::Refused,std::move(error)};
    auto const now = GameTime::GetGameTime().count();
    auto const lifetime = sender->GetSession()->GetSecurity() ? 90u * DAY : 30u * DAY;
    if (now < 0 || static_cast<std::uint64_t>(now) + command->delayS + lifetime > std::numeric_limits<std::uint32_t>::max())
        return {StepState::Refused,"mail_time"};
    auto const delivery = static_cast<std::uint32_t>(now) + command->delayS;
    if (attempts.size() >= ActionQueue::Capacity) return {StepState::Refused,"mail_receipt_capacity"};
    Attempt initial;
    initial.admitted = entry.payload; initial.guid = guid; initial.human = human; initial.epoch = RealmModule::Instance().Epoch();
    auto& attempt = attempts.emplace(entry.id,std::move(initial)).first->second;
    auto refuse = [&](std::string reason) -> RealmStep
    {
        if (attempt.mutated) return {StepState::Unknown,std::move(reason)};
        attempts.erase(entry.id); return {StepState::Refused,std::move(reason)};
    };
    std::vector<Source> sources;
    for (auto const& use : plan->sources)
    {
        auto found = std::find_if(allSources->begin(),allSources->end(),[&](auto const& source) { return source.guid == use.guid; });
        if (found == allSources->end() || !ExactSource(sender,*found,recipient->AccountId)) return refuse("no_item");
        sources.push_back(*found);
    }
    std::vector<Item*> attachments;
    std::vector<MailSavedItem> expected;
    attachments.reserve(kMailAttachments); expected.reserve(kMailAttachments);
    attempt.escrow.reserve(kMailAttachments); attempt.uncertain.reserve(kMailAttachments);
    for (std::size_t i = 0; i < plan->attachments.size(); ++i)
    {
        auto const& want = plan->attachments[i];
        auto source = std::find_if(sources.begin(),sources.end(),[&](auto const& row) { return row.guid == want.source; });
        auto usage = std::find_if(plan->sources.begin(),plan->sources.end(),[&](auto const& row) { return row.guid == want.source; });
        bool const last = std::none_of(plan->attachments.begin()+i+1,plan->attachments.end(),[&](auto const& row) { return row.source == want.source; });
        Item* item = nullptr;
        if (want.source && usage != plan->sources.end() && !usage->after && last) item = source->item;
        else
        {
            std::unique_ptr<Item> created(want.source ? source->item->CloneItem(want.count) : Item::CreateItem(want.entry,want.count));
            if (!created || created->GetCount() != want.count || created->GetEntry() != want.entry
                || created->IsInWorld() || created->IsInUpdateQueue() || !created->GetOwnerGUID().IsEmpty()
                || (want.source && !ExactClone(source->item,created.get(),want.count))) return refuse("no_item");
            item = created.get(); attempt.escrow.push_back(std::move(created));
        }
        attachments.push_back(item);
        expected.push_back({item->GetGUID().GetCounter(),want.entry,human,want.count});
    }
    attempt.first = expected.empty() ? 0 : expected.front().guid;
    // All planning/allocation ends before consuming a sender asset. Recheck the
    // exact lease and every selected native object after allocation hooks.
    if (auto why = AdmissionReason(guid,Purpose::Mail,entry.id); !why.empty()) return refuse(std::move(why));
    if (attempt.epoch != RealmModule::Instance().Epoch() || HumanGuid() != human || sender->GetMoney() != before)
        return refuse("mail_operation_identity");
    for (auto const& source : sources) if (!ExactSource(sender,source,recipient->AccountId)) return refuse("no_item");
    auto transaction = std::make_shared<MailTransaction>();
    if (command->money)
    {
        attempt.mutated = true;
        sender->ModifyMoney(-static_cast<std::int32_t>(command->money),false);
        if (!MailDebitProved(before,sender->GetMoney(),command->money,MAX_MONEY_AMOUNT)) return refuse("money");
    }
    for (auto const& use : plan->sources)
    {
        auto const& source = *std::find_if(sources.begin(),sources.end(),[&](auto const& row) { return row.guid == use.guid; });
        auto* item = source.item;
        attempt.mutated = true;
        if (!use.after)
        {
            sender->MoveItemFromInventory(source.bag,source.slot,true);
            // The inspected transmog observer and inherited no-op providers
            // preserve Item lifetime. No entry-count delta is consulted.
            if (sender->GetItemByPos(source.bag,source.slot) == item) return refuse("mail_source_unproved");
            attempt.escrow.emplace_back(item);
            if (sender->GetItemByPos(source.bag,source.slot) || item->GetGUID().GetCounter() != source.guid
                || item->GetOwnerGUID() != sender->GetGUID() || item->GetCount() != source.count)
                return refuse("mail_source_unproved");
            item->DeleteFromInventoryDB(transaction);
        }
        else
        {
            sender->ItemRemovedQuestCheck(source.entry,use.before-use.after);
            item->SetCount(use.after); item->SetState(ITEM_CHANGED,sender);
            // Persist only this exact source/position; SaveInventoryAndGoldToDB
            // would sweep unrelated dirty inventory into this operation.
            auto* statement = CharacterDatabase.GetPreparedStatement(CHAR_REP_INVENTORY_ITEM);
            statement->SetData(0,sender->GetGUID().GetRawValue()); statement->SetData(1,source.bagGuid);
            statement->SetData(2,source.slot); statement->SetData(3,source.guid); transaction->Append(statement);
            item->RemoveFromUpdateQueueOf(sender); item->SaveToDB(transaction);
            if (sender->GetItemByPos(source.bag,source.slot) != item || item->GetCount() != use.after
                || item->GetOwnerGUID() != sender->GetGUID()) return refuse("mail_source_unproved");
        }
        if (auto why = AdmissionReason(guid,Purpose::Mail,entry.id); !why.empty()) return refuse(std::move(why));
    }
    MailDraft draft(command->subject,command->body);
    for (std::size_t i = 0; i < attachments.size(); ++i)
    {
        auto* item = attachments[i];
        item->SetCount(expected[i].count);
        item->SetOwnerGUID(PlayerGuid(human));
        item->SetGuidValue(ITEM_FIELD_CONTAINED,ObjectGuid::Empty);
        if (item->GetState() == ITEM_UNCHANGED) item->FSetState(ITEM_CHANGED);
        item->SaveToDB(transaction); draft.AddItem(item);
    }
    if (!MailDebitProved(before,sender->GetMoney(),command->money,MAX_MONEY_AMOUNT)) return refuse("money");
    if (auto why = AdmissionReason(guid,Purpose::Mail,entry.id); !why.empty()) return refuse(std::move(why));
    if (HumanGuid() != human || attempt.epoch != RealmModule::Instance().Epoch()) return refuse("mail_operation_identity");
    // Release object ownership immediately before the native transfer. The
    // online receiver owns them afterwards; the offline path deletes them.
    attempt.mutated = true;
    for (auto& item : attempt.escrow) attempt.uncertain.push_back(item.release());
    attempt.escrow.clear();
    draft.AddMoney(command->money).SendMailTo(transaction,
        MailReceiver(ObjectAccessor::FindConnectedPlayer(PlayerGuid(human)),human),
        MailSender(MAIL_NORMAL,guid,MAIL_STATIONERY_DEFAULT),MAIL_CHECK_MASK_HAS_BODY,command->delayS);
    if (command->money) sender->SaveGoldToDB(transaction);
    auto const stage = transaction->Inspect(sources,guid,delivery+lifetime);
    if (!ProveMailStage(stage,*command,*plan,expected,human,before-command->money,delivery))
        return {StepState::Unknown,"mail_stage_unproved"};
    attempt.mail = stage.headers.front().id;
    attempt.header = stage.headers.front(); attempt.expiry = delivery+lifetime; attempt.attachments = expected;
    attempt.readbackSql = ReadbackSql(attempt.mail,*command,before-command->money,*plan,sources);
    if (attempt.readbackSql.size() > 8192) return {StepState::Unknown,"mail_readback_bound"};
    attempt.uncertain.clear(); // Exact native transfer is proved; never touch these objects again.
    // Unlike MySQLConnection's implicit void CommitTransaction, a raw statement
    // is checked by ExecuteTransaction. Make this transaction's own COMMIT an
    // acknowledged statement, then still require exact persisted readback.
    // The core's subsequent implicit COMMIT has no remaining writes to commit.
    transaction->Append("COMMIT");
    auto* const receipt = &attempt;
    attempt.commit = std::make_unique<TransactionCallback>(CharacterDatabase.AsyncCommitTransaction(transaction));
    attempt.commit->AfterComplete([receipt](bool committed) { receipt->committed = committed; });
    return ReadResult(entry,guid,attempt);
}

class MailTakenScripts final : public PlayerScript
{
public:
    MailTakenScripts() : PlayerScript("LeeroyRealmMailTaken", {PLAYERHOOK_ON_AFTER_TAKE_ITEM_FROM_MAIL}) { }
    void OnPlayerAfterTakeItemFromMail(Player* player, Item* item, std::uint32_t count) override
    {
        if (!RealmModule::Instance().On()) return;
        if (!player || !item || player->GetGUID().GetCounter() != HumanGuid()) return;
        // Native hook supplies the actual stored stack (which may be merged)
        // and the extracted count. It is an observation, not a commit receipt.
        Publish(MakeMailTaken(item->GetGUID().GetCounter(),item->GetEntry(),count));
    }
};
}

void RegisterRealmMailHandlers()
{
    RealmModule::Instance().RegisterExecutor("mail_send",Send,Eligibility);
    RegisterCommand("mail_send",[](nlohmann::json const& message) -> LeeroyCommandResult
    {
        std::string error;
        auto command = ParseMailSend(message,error);
        if (!command) return {false,error};
        Queued entry;
        entry.id = message.at("id").get<std::string>(); entry.kind = "mail_send";
        entry.guids = {command->fromGuid}; entry.payload = message;
        if (!RealmModule::Instance().Accept(std::move(entry),error)) return {false,error};
        return {true,""};
    });
}
void AddRealmMailScripts() { new MailTakenScripts(); }
}
