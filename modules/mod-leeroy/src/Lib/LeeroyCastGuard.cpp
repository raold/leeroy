// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyCastGuard.h"

#include <algorithm>
#include <stdexcept>

namespace Leeroy::Realm
{
namespace
{
constexpr std::uint64_t IntentMs = 120000;
constexpr std::uint64_t RetryMs = 60000;
constexpr std::size_t MaxIntents = 1024;
constexpr std::size_t MaxReadds = 5;
constexpr std::size_t MaxReceipts = kMaxKickReceipts;
constexpr std::uint8_t MaxRank = 9; // A guild has ranks 0-9.

bool Elapsed(std::uint64_t now, std::uint64_t then, std::uint64_t duration)
{
    // Subtraction only after the order check: neither rollback nor overflow
    // may turn a recent attempt into an expired cooldown.
    return now >= then && now - then >= duration;
}

bool Space(char ch)
{
    return ch == ' ' || ch == '\t';
}

std::string_view Token(std::string_view& text)
{
    while (!text.empty() && Space(text.front()))
        text.remove_prefix(1);
    auto const end = text.find_first_of(" \t");
    auto const token = text.substr(0, end);
    text.remove_prefix(token.size());
    return token;
}

bool Prefix(std::string_view value, std::string_view full, std::size_t minimum)
{
    if (value.size() < minimum || value.size() > full.size())
        return false;
    for (std::size_t i = 0; i < value.size(); ++i)
    {
        auto ch = value[i];
        if (ch >= 'A' && ch <= 'Z')
            ch = static_cast<char>(ch + ('a' - 'A'));
        if (ch != full[i])
            return false;
    }
    return true;
}

bool PlainName(std::string_view name)
{
    if (name.empty() || name.size() > 48)
        return false;
    // Preserve valid UTF-8 names for the core's name resolver. This validates
    // encoding, not character-name legality, which remains a core responsibility.
    for (std::size_t i = 0; i < name.size();)
    {
        auto const first = static_cast<unsigned char>(name[i++]);
        if (first < 0x80)
        {
            if (!((first >= 'a' && first <= 'z') || (first >= 'A' && first <= 'Z')))
                return false;
            continue;
        }
        unsigned count;
        std::uint32_t point;
        std::uint32_t minimum;
        if (first >= 0xc2 && first <= 0xdf)
            count = 1, point = first & 0x1f, minimum = 0x80;
        else if (first >= 0xe0 && first <= 0xef)
            count = 2, point = first & 0x0f, minimum = 0x800;
        else if (first >= 0xf0 && first <= 0xf4)
            count = 3, point = first & 0x07, minimum = 0x10000;
        else
            return false;
        if (count > name.size() - i)
            return false;
        for (unsigned j = 0; j < count; ++j)
        {
            auto const next = static_cast<unsigned char>(name[i++]);
            if ((next & 0xc0) != 0x80)
                return false;
            point = (point << 6) | (next & 0x3f);
        }
        if (point < minimum || point > 0x10ffff || (point >= 0xd800 && point <= 0xdfff)
            || (point >= 0x80 && point <= 0x9f))
            return false;
    }
    return true;
}
}

GuardState::GuardState(std::uint32_t guildId, std::uint32_t humanLow)
    : _guildId(guildId), _humanLow(humanLow)
{
    if (guildId == 0 || humanLow == 0)
        throw std::invalid_argument("cast guard requires configured guild and human identities");
}

bool GuardState::Protectable(std::uint32_t guid) const
{
    return guid != 0 && guid != _humanLow;
}

void GuardState::PruneIntents(std::uint64_t nowMs)
{
    for (auto it = _commandIntents.begin(); it != _commandIntents.end();)
        if (Elapsed(nowMs, it->second, IntentMs))
            it = _commandIntents.erase(it);
        else
            ++it;
}

void GuardState::NoteCommandKick(std::uint32_t guid, std::uint64_t nowMs)
{
    if (!Protectable(guid) || _removedByHuman.contains(guid))
        return;
    PruneIntents(nowMs);
    auto it = _commandIntents.find(guid);
    if (it != _commandIntents.end())
        it->second = std::max(it->second, nowMs);
    else if (_commandIntents.size() < MaxIntents)
        _commandIntents.emplace(guid, nowMs);
}

bool GuardState::PendingCommandKick(std::uint32_t guid, std::uint64_t nowMs) const
{
    auto const it = _commandIntents.find(guid);
    return it != _commandIntents.end() && nowMs >= it->second && nowMs - it->second < IntentMs;
}

void GuardState::NoteKick(std::uint32_t guid, std::uint64_t /*nowMs*/)
{
    if (!Protectable(guid) || !_removedByHuman.insert(guid).second)
        return;
    auto const it = _known.find(guid);
    _pendingKicks.emplace(guid, it == _known.end() ? std::uint8_t{0} : it->second);
    _commandIntents.erase(guid);
    _attemptedAt.erase(guid);
    _blockedGuild.erase(guid);
}

bool GuardState::AllowRejoin(std::uint32_t guid)
{
    if (!Protectable(guid) || _removedByHuman.erase(guid) == 0)
        return false;
    // A pending confirmed kick may not have passed yet. Its old rank is not
    // proof of a new membership, so admission must be learned from the roster.
    _known.erase(guid);
    _pendingKicks.erase(guid);
    _commandIntents.erase(guid);
    _attemptedAt.erase(guid);
    _blockedGuild.erase(guid);
    return true;
}

bool GuardState::NoteKickReceipt(std::uint32_t guid, std::uint64_t kickTs, std::uint8_t rank)
{
    if (!Protectable(guid) || !kickTs || !_removedByHuman.contains(guid) || _pendingKicks.contains(guid))
        return false;
    if (!_receipts.contains(guid) && _receipts.size() >= MaxReceipts)
    {
        // Drop a settled receipt first; never displace another open consent.
        auto settled = std::find_if(_receipts.begin(), _receipts.end(), [](auto const& entry) {
            return entry.second.state != KickReceipt::State::Open;
        });
        if (settled == _receipts.end())
            return false;
        _receipts.erase(settled);
    }
    _receipts[guid] = KickReceipt{kickTs, rank, KickReceipt::State::Open};
    return true;
}

KickReceipt const* GuardState::Receipt(std::uint32_t guid) const
{
    auto const it = _receipts.find(guid);
    return it == _receipts.end() ? nullptr : &it->second;
}

bool GuardState::CompleteReadmit(std::uint32_t guid, std::uint64_t kickTs)
{
    auto const it = _receipts.find(guid);
    if (it == _receipts.end() || it->second.kickTs != kickTs || it->second.state != KickReceipt::State::Open
        || _pendingKicks.contains(guid) || !AllowRejoin(guid))
        return false;
    it->second.state = KickReceipt::State::Readmitted;
    return true;
}

void GuardState::ReadmitUnknown(std::uint32_t guid, std::uint64_t kickTs)
{
    auto const it = _receipts.find(guid);
    if (it != _receipts.end() && it->second.kickTs == kickTs)
        it->second.state = KickReceipt::State::Unknown;
}

std::size_t GuardState::RestoreReceipts(std::vector<StoredKickReceipt> rows, std::uint64_t nowUnixMs,
    std::uint64_t windowMs)
{
    if (!nowUnixMs)
        return 0;
    std::stable_sort(rows.begin(), rows.end(), [](auto const& a, auto const& b) { return a.kickTs > b.kickTs; });
    std::size_t restored = 0;
    for (auto const& row : rows)
    {
        if (_receipts.size() >= MaxReceipts)
            break;
        // Older than the window has no authority. A future ts is kept: the
        // decision refuses it as a changed clock, as it would have before.
        if (!Protectable(row.guid) || !row.kickTs || (nowUnixMs >= row.kickTs && nowUnixMs - row.kickTs > windowMs))
            continue;
        // What this process learned is newer than any stored row.
        if (_receipts.contains(row.guid) || _removedByHuman.contains(row.guid) || _pendingKicks.contains(row.guid)
            || _known.contains(row.guid))
            continue;
        auto state = KickReceiptFromCode(row.state);
        if (row.rank > MaxRank)
            state = KickReceipt::State::Unknown;
        _receipts[row.guid] = KickReceipt{row.kickTs, row.rank, state};
        if (state != KickReceipt::State::Readmitted)
            _removedByHuman.insert(row.guid); // Readmission already lifted it; nothing else did.
        ++restored;
    }
    return restored;
}

std::uint8_t KickReceiptCode(KickReceipt::State state)
{
    switch (state)
    {
        case KickReceipt::State::Open: return 0;
        case KickReceipt::State::Readmitted: return 1;
        case KickReceipt::State::Unknown: return 2;
    }
    return 2;
}

KickReceipt::State KickReceiptFromCode(std::uint8_t code)
{
    switch (code)
    {
        case 0: return KickReceipt::State::Open;
        case 1: return KickReceipt::State::Readmitted;
        default: return KickReceipt::State::Unknown; // Never a consent nobody recorded.
    }
}

bool StoredReceiptIs(StoredKickReceipt const& row, std::uint32_t guid, KickReceipt const& receipt)
{
    return row.guid == guid && row.kickTs == receipt.kickTs && row.rank == receipt.rank
        && row.state == KickReceiptCode(receipt.state);
}

void GuardState::NoteFailed(std::uint32_t guid, std::uint64_t nowMs)
{
    if (!Protectable(guid) || !_known.contains(guid) || _removedByHuman.contains(guid))
        return;
    auto [it, inserted] = _attemptedAt.emplace(guid, nowMs);
    if (!inserted)
        it->second = std::max(it->second, nowMs);
}

std::vector<GuardStep> GuardState::Pass(GuardInput const& input, std::size_t maxReadds)
{
    PruneIntents(input.nowMs);
    std::vector<GuardStep> result;
    for (auto const& [guid, rank] : _pendingKicks)
    {
        result.push_back({guid, GuardAction::HisKick, rank});
        _known.erase(guid);
    }
    _pendingKicks.clear();

    for (auto const& [guid, rank] : input.members)
    {
        if (!Protectable(guid) || _removedByHuman.contains(guid))
            continue;
        _known[guid] = rank;
        _commandIntents.erase(guid); // Failed command intent cannot poison a later departure.
        _attemptedAt.erase(guid);
        _blockedGuild.erase(guid);
    }

    std::size_t readds = 0;
    auto const cap = std::min(maxReadds, MaxReadds);
    for (auto it = _known.begin(); it != _known.end();)
    {
        auto const [guid, rank] = *it;
        ++it;
        if (input.members.contains(guid))
            continue;
        auto const observed = input.chars.find(guid);
        if (observed == input.chars.end())
            continue; // A failed/partial character lookup is not a deletion.
        auto const& character = observed->second;
        if (!character.exists)
        {
            result.push_back({guid, GuardAction::Gone, rank});
            _known.erase(guid);
            _commandIntents.erase(guid);
            _attemptedAt.erase(guid);
            _blockedGuild.erase(guid);
            continue;
        }
        if (character.guildId == _guildId)
            continue; // Conflicting/partial roster; never add an existing member again.
        if (character.guildId != 0 && !character.botGuild)
        {
            auto [blocked, inserted] = _blockedGuild.emplace(guid, character.guildId);
            if (inserted || blocked->second != character.guildId)
            {
                result.push_back({guid, GuardAction::Blocked, rank});
                blocked->second = character.guildId;
            }
            continue;
        }
        _blockedGuild.erase(guid);
        auto const attempted = _attemptedAt.find(guid);
        if (readds >= cap || (attempted != _attemptedAt.end() && !Elapsed(input.nowMs, attempted->second, RetryMs)))
            continue;
        auto const action = character.guildId == 0 ? GuardAction::Readd : GuardAction::LeaveBotGuildThenReadd;
        result.push_back({guid, action, rank});
        _attemptedAt[guid] = input.nowMs;
        ++readds;
    }
    std::sort(result.begin(), result.end(), [](GuardStep const& a, GuardStep const& b) { return a.guid < b.guid; });
    return result;
}

bool UninviteEventIsHis(bool isUninvite, std::uint32_t guildId, std::uint32_t kickerLow,
    std::uint32_t protectedGuild, std::uint32_t humanLow)
{
    return isUninvite && protectedGuild != 0 && humanLow != 0 && guildId == protectedGuild && kickerLow == humanLow;
}

bool CommandIsHis(bool fromConsole, std::uint32_t invokerLow, std::uint32_t humanLow)
{
    return fromConsole || (humanLow != 0 && invokerLow == humanLow);
}

std::optional<UninviteTarget> ParseGuildUninvite(std::string_view command)
{
    if (command.empty() || command.size() > 256)
        return std::nullopt;
    for (auto ch : command)
        if ((static_cast<unsigned char>(ch) < 0x20 && ch != '\t') || ch == 0x7f)
            return std::nullopt;
    while (!command.empty() && Space(command.front()))
        command.remove_prefix(1);
    if (!command.empty() && command.front() == '.')
        command.remove_prefix(1);
    if (!Prefix(Token(command), "guild", 2) || !Prefix(Token(command), "uninvite", 1))
        return std::nullopt;
    auto const name = Token(command);
    if (!Token(command).empty() || (!name.empty() && !PlainName(name)))
        return std::nullopt;
    return UninviteTarget{name.empty() ? std::nullopt : std::optional<std::string>{std::string{name}}};
}
}
