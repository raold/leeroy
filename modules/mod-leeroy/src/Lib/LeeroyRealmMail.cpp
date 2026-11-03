// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmMail.h"
#include <algorithm>
#include <limits>
#include <map>
#include <set>

namespace Leeroy::Realm
{
std::optional<MailPlan> PlanMail(MailSendCmd const& command, std::vector<ItemRule> const& rules,
    std::vector<MailTemplateFacts> const& templates, std::vector<MailBagFacts> const& bags, std::string& error)
{
    error.clear();
    auto fail = [&](char const* why) -> std::optional<MailPlan> { error = why; return std::nullopt; };
    if (command.items.size() > 4 || bags.size() > kMailBagSlots || templates.size() > 4
        || rules.size() > kMaxCsvRows) return fail("no_item");
    MailPlan plan;
    std::map<std::uint32_t, std::uint32_t> remaining, newCounts;
    for (auto const& bag : bags)
        if (!bag.guid || !bag.entry || !bag.count || !remaining.emplace(bag.guid, bag.count).second)
            return fail("no_item");
    auto add = [&](MailAttachment attachment)
    {
        if (plan.attachments.size() == kMailAttachments) return false;
        plan.attachments.push_back(attachment); return true;
    };
    for (auto const& request : command.items)
    {
        if (!request.count || request.count > 20) return fail("no_item");
        auto proto = std::find_if(templates.begin(), templates.end(), [&](auto const& row) { return row.entry == request.itemId; });
        if (proto == templates.end() || !proto->maxStack) return fail("no_item");
        auto count = request.count;
        if (request.from == "new")
        {
            std::uint32_t maximum = 0;
            for (auto const& rule : rules)
                if (rule.itemId == request.itemId && (rule.use == "gift" || rule.use == "holiday" || rule.use == "prize"))
                    maximum = std::max(maximum, rule.maxCount);
            auto& total = newCounts[request.itemId];
            if (maximum < count || total > maximum - count || (proto->maxCount && total + count > proto->maxCount))
                return fail("not_whitelisted");
            total += count;
            while (count)
            {
                auto const take = std::min(count, proto->maxStack);
                if (!add({request.itemId, take, 0})) return fail("no_item");
                count -= take;
            }
        }
        else if (request.from == "bags")
        {
            for (auto const& bag : bags)
            {
                if (!count) break;
                if (bag.entry != request.itemId || !bag.transferable) continue;
                auto& left = remaining.at(bag.guid);
                auto const take = std::min({count, left, proto->maxStack});
                if (!take) continue;
                if (!add({request.itemId, take, bag.guid})) return fail("no_item");
                left -= take; count -= take;
            }
            if (count) return fail("no_item");
        }
        else return fail("no_item");
    }
    for (auto const& bag : bags)
        if (remaining.at(bag.guid) != bag.count)
            plan.sources.push_back({bag.guid, bag.entry, bag.count, remaining.at(bag.guid)});
    return plan;
}

bool MailDebitAllowed(std::uint32_t before, std::uint32_t amount, std::uint32_t nativeCap)
{
    return before <= nativeCap && amount <= 1000000 && amount <= before
        && amount <= static_cast<std::uint32_t>(std::numeric_limits<std::int32_t>::max());
}
bool MailDebitProved(std::uint32_t before, std::uint32_t after, std::uint32_t amount, std::uint32_t nativeCap)
{
    return MailDebitAllowed(before, amount, nativeCap) && after == before - amount;
}

bool ProveMailStage(MailStage const& stage, MailSendCmd const& command, MailPlan const& plan,
    std::vector<MailSavedItem> const& attachments, std::uint32_t human, std::uint32_t moneyAfter, std::uint32_t deliverAt)
{
    if (stage.unsupported || !human || attachments.size() > kMailAttachments
        || stage.headers.size() != 1 || attachments.size() != plan.attachments.size()
        || stage.links.size() != attachments.size() || stage.items.size() != attachments.size() + plan.sources.size()
            - static_cast<std::size_t>(std::count_if(plan.sources.begin(), plan.sources.end(), [](auto const& s) { return !s.after; })))
        return false;
    auto const& header = stage.headers.front();
    if (!header.id || !header.normal || !header.stationeryDefault || !header.noTemplate || !header.hasBody || !header.noCod
        || header.sender != command.fromGuid || header.receiver != human || header.subject != command.subject
        || header.body != command.body || header.money != command.money || header.deliverAt != deliverAt
        || header.hasAttachments != !attachments.empty())
        return false;
    std::map<std::uint32_t, MailSavedItem> expected;
    std::set<std::uint32_t> linked, deleted, saved;
    for (std::size_t i = 0; i < attachments.size(); ++i)
    {
        auto const& item = attachments[i]; auto const& request = plan.attachments[i];
        if (!item.guid || item.entry != request.entry || item.count != request.count || item.owner != human
            || !expected.emplace(item.guid, item).second) return false;
    }
    for (auto const& source : plan.sources)
        if (source.after)
        {
            if (!expected.emplace(source.guid, MailSavedItem{source.guid, source.entry, command.fromGuid, source.after}).second)
                return false;
            saved.insert(source.guid);
        }
        else
        {
            bool original = false;
            for (std::size_t i = 0; i < attachments.size(); ++i)
                if (plan.attachments[i].source == source.guid && attachments[i].guid == source.guid) original = true;
            if (!original) return false;
            deleted.insert(source.guid);
        }
    for (auto const& item : stage.items)
    {
        auto found = expected.find(item.guid);
        if (found == expected.end() || found->second != item) return false;
        expected.erase(found);
    }
    if (!expected.empty()) return false;
    for (auto const& link : stage.links)
        if (link.mail != header.id || link.receiver != human || !linked.insert(link.item).second
            || std::none_of(attachments.begin(), attachments.end(), [&](auto const& item) { return item.guid == link.item; }))
            return false;
    for (auto guid : stage.inventoryDeletes) if (deleted.erase(guid) != 1) return false;
    for (auto guid : stage.inventorySaves) if (saved.erase(guid) != 1) return false;
    if (!deleted.empty() || !saved.empty()) return false;
    if (!command.money) return stage.gold.empty();
    return stage.gold.size() == 1 && stage.gold.front() == std::pair{command.fromGuid, moneyAfter};
}
}
