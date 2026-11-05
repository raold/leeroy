// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyPullRecorder.h"
#include "LeeroyProtocol.h"
#include <algorithm>
#include <stdexcept>
#include <tuple>

namespace Leeroy::Training
{
namespace
{
using json = nlohmann::json;

void AddDamage(MemberRow& row, std::uint32_t spell, std::uint32_t amount)
{
    auto* bucket = &row.done[kMaxSpells];
    if (spell)
    {
        auto end = row.done.begin() + row.doneCount;
        auto found = std::find_if(row.done.begin(), end, [spell](auto const& v) { return v.spell == spell; });
        if (found != end)
            bucket = &*found;
        else if (row.doneCount < kMaxSpells)
        {
            bucket = &row.done[row.doneCount++];
            bucket->spell = spell;
        }
    }
    bucket->amount = SaturatingAdd(bucket->amount, amount);
    bucket->hits = SaturatingAdd(bucket->hits, 1);
}

void AddTaken(MemberRow& row, DamageHit const& hit)
{
    auto* bucket = &row.takenBy[kMaxTakenBy];
    if (hit.spell || hit.attackerEntry)
    {
        auto end = row.takenBy.begin() + row.takenCount;
        auto found = std::find_if(row.takenBy.begin(), end, [&](auto const& v)
            { return v.spell == hit.spell && v.source == hit.attackerEntry; });
        if (found != end)
            bucket = &*found;
        else if (row.takenCount < kMaxTakenBy)
        {
            bucket = &row.takenBy[row.takenCount++];
            bucket->spell = hit.spell;
            bucket->source = hit.attackerEntry;
        }
    }
    bucket->amount = SaturatingAdd(bucket->amount, hit.amount);
    bucket->hits = SaturatingAdd(bucket->hits, 1);
}

template <typename T, std::size_t N, typename Less, typename Encode, typename Used>
json Sorted(std::array<T, N> const& rows, Less less, Encode encode, Used used)
{
    std::vector<T const*> selected;
    for (auto const& row : rows)
        if (used(row))
            selected.push_back(&row);
    std::sort(selected.begin(), selected.end(), [&](auto a, auto b) { return less(*a, *b); });
    auto out = json::array();
    for (auto row : selected)
        out.push_back(encode(*row));
    return out;
}

void Append(std::vector<std::string>& out, std::int64_t ts, json body)
{
    auto line = EncodeEvent(ts, std::move(body));
    if (line.empty() || line.size() >= kLineLimit)
        throw std::length_error("training event exceeds its line limit");
    out.push_back(std::move(line));
}

json MemberBody(PullTable const& table, MemberRow const& row, std::string const& pull)
{
    auto done = Sorted(row.done, [](auto const& a, auto const& b)
        { return a.amount != b.amount ? a.amount > b.amount : a.spell < b.spell; },
        [](auto const& v) { return json{{"spell", v.spell}, {"amount", v.amount}, {"hits", v.hits}}; },
        [](auto const& v) { return v.hits != 0; });
    auto taken = Sorted(row.takenBy, [](auto const& a, auto const& b)
        {
            return a.amount != b.amount ? a.amount > b.amount
                : std::tie(a.spell, a.source) < std::tie(b.spell, b.source);
        }, [](auto const& v)
        { return json{{"spell", v.spell}, {"source", v.source}, {"amount", v.amount}, {"hits", v.hits}}; },
        [](auto const& v) { return v.hits != 0; });
    auto debuffs = Sorted(row.debuffs, [](auto const& a, auto const& b)
        { return a.count != b.count ? a.count > b.count : a.spell < b.spell; },
        [](auto const& v) { return json{{"spell", v.spell}, {"count", v.count}, {"max_stack", v.maxStack}}; },
        [](auto const& v) { return v.count != 0; });
    auto casts = Sorted(row.casts, [](auto const& a, auto const& b)
        { return a.count != b.count ? a.count > b.count : a.spell < b.spell; },
        [](auto const& v) { return json{{"spell", v.spell}, {"count", v.count}}; },
        [](auto const& v) { return v.count != 0; });
    return {{"type", "pull_member"}, {"pull", pull}, {"guid", row.guid}, {"class", row.cls},
        {"spec_tab", row.specTab}, {"tank", row.tank}, {"ilvl", row.ilvl}, {"damage", row.damage},
        {"damage_boss", row.damageBoss}, {"healing", row.healing}, {"overheal", row.overheal},
        {"taken", row.taken}, {"deaths", row.deaths}, {"interrupts", row.interrupts},
        {"active_ms", table.ActiveMs(row)}, {"done", std::move(done)}, {"taken_by", std::move(taken)},
        {"debuffs", std::move(debuffs)}, {"casts", std::move(casts)}};
}

json Track(std::string const& pull, std::uint32_t who, std::uint32_t entry, std::size_t from)
{
    auto empty = json::array();
    return {{"type", "pull_track"}, {"pull", pull}, {"who", who}, {"entry", entry}, {"from_s", from},
        {"xs", empty}, {"ys", empty}, {"o", empty}, {"hp", empty}, {"power", empty},
        {"target", empty}, {"casts", empty}};
}

// Emit only actual contiguous runs. An omitted second is not a repeated observation or zero coordinate.
template <typename Points, typename Emit>
void Runs(Points const& points, std::uint64_t duration, Emit emit)
{
    std::size_t i = 0;
    while (i < points.size() && std::uint64_t(i) * 1000 <= duration)
    {
        if (!points[i].present)
        {
            ++i;
            continue;
        }
        auto start = i++;
        while (i < points.size() && i - start < kTrackChunk && points[i].present
            && std::uint64_t(i) * 1000 <= duration)
            ++i;
        emit(start, i);
    }
}
}

void PullTable::Reset(PullHeader const& header)
{
    _header = header;
    std::fill(_members.begin(), _members.end(), MemberRow{});
    std::fill(_deaths.begin(), _deaths.end(), DeathRow{});
    std::fill(_bossSamples.begin(), _bossSamples.end(), BossPoint{});
    std::fill(_bossCasts.begin(), _bossCasts.end(), TrackCast{});
    std::fill(_live.begin(), _live.end(), CreatureCast{});
    std::fill(_limits.begin(), _limits.end(), LimitEntry{});
    _memberCount = _deathCount = _bossCastCount = _liveCount = 0;
    _bossEntry = _updMeanMs = 0;
    _endMs = 0;
    _outcome = Outcome::Aborted;
    _closed = _assisted = false;
    _open = true;
    _pristine = true;
}

bool PullTable::Arm(PullHeader const& header)
{
    if (!_open || !_pristine)
        return false;
    _header = header;
    _pristine = false;
    return true;
}

MemberRow const* PullTable::Find(std::uint32_t guid) const
{
    for (std::size_t i = 0; i < _memberCount; ++i)
        if (_members[i].guid == guid)
            return &_members[i];
    return nullptr;
}

MemberRow* PullTable::Mutable(std::uint32_t guid)
{
    return _open ? const_cast<MemberRow*>(Find(guid)) : nullptr;
}

int PullTable::Member(std::uint32_t guid, std::uint8_t cls, std::int8_t specTab)
{
    if (!_open || !guid)
        return -1;
    for (std::size_t i = 0; i < _memberCount; ++i)
        if (_members[i].guid == guid)
            return static_cast<int>(i);
    if (_memberCount == kMaxMembers)
        return -1;
    _pristine = false;
    auto& row = _members[_memberCount];
    row.guid = guid;
    row.cls = cls;
    row.specTab = specTab;
    return static_cast<int>(_memberCount++);
}

void PullTable::Damage(DamageHit const& hit)
{
    if (auto row = Mutable(hit.attacker))
    {
        row->damage = SaturatingAdd(row->damage, hit.amount);
        if (hit.victimIsBoss)
            row->damageBoss = SaturatingAdd(row->damageBoss, hit.amount);
        AddDamage(*row, hit.spell, hit.amount);
    }
    if (auto row = Mutable(hit.victim))
    {
        row->taken = SaturatingAdd(row->taken, hit.amount);
        AddTaken(*row, hit);
        row->hits[row->hitNext] = {hit.tMs, hit.spell, hit.attackerEntry, hit.amount, hit.victimHpBefore};
        row->hitNext = (row->hitNext + 1) % kLastHits;
        row->hitCount = std::min(row->hitCount + 1, kLastHits);
    }
}

void PullTable::Heal(HealHit const& hit)
{
    if (auto row = Mutable(hit.healer))
    {
        row->healing = SaturatingAdd(row->healing, hit.effective);
        row->overheal = SaturatingAdd(row->overheal, hit.raw > hit.effective ? hit.raw - hit.effective : 0);
    }
}

void PullTable::Aura(std::uint32_t member, std::uint32_t spell, std::uint8_t stack)
{
    auto row = Mutable(member);
    if (!row || !spell)
        return;
    auto end = row->debuffs.begin() + row->debuffCount;
    auto found = std::find_if(row->debuffs.begin(), end, [spell](auto const& v) { return v.spell == spell; });
    if (found == end)
    {
        if (row->debuffCount == kMaxDebuffs)
            return;
        found = row->debuffs.begin() + row->debuffCount++;
        found->spell = spell;
    }
    found->count = SaturatingAdd(found->count, 1);
    found->maxStack = std::max(found->maxStack, stack);
}

void PullTable::Cast(std::uint32_t member, std::uint32_t spell, bool interrupt)
{
    auto row = Mutable(member);
    if (!row)
        return;
    if (interrupt)
        row->interrupts = SaturatingAdd(row->interrupts, 1);
    if (!spell)
        return;
    auto end = row->casts.begin() + row->castCount;
    auto found = std::find_if(row->casts.begin(), end, [spell](auto const& v) { return v.spell == spell; });
    if (found == end)
    {
        if (row->castCount == kMaxCasts)
            return;
        found = row->casts.begin() + row->castCount++;
        found->spell = spell;
    }
    found->count = SaturatingAdd(found->count, 1);
}

void PullTable::BossCast(std::uint32_t spell, std::uint16_t tS)
{
    if (_open && spell && _bossCastCount < kMaxBossCasts)
    {
        _pristine = false;
        _bossCasts[_bossCastCount++] = {spell, tS};
    }
}

bool PullTable::Live(CreatureCast const& cast)
{
    if (!_open || !cast.caster || !cast.spell || cast.state > 2 || _liveCount == kLiveRing)
        return false;
    auto slot = _limits.end();
    for (auto it = _limits.begin(); it != _limits.end(); ++it)
    {
        if (it->used && it->caster == cast.caster && it->spell == cast.spell)
        {
            // A late/out-of-order callback cannot underflow into a fresh one-second allowance.
            if (cast.tMs < it->atMs || cast.tMs - it->atMs < 1000)
                return false;
            slot = it;
            break;
        }
        if (!it->used && slot == _limits.end())
            slot = it;
    }
    if (slot == _limits.end())
        slot = std::min_element(_limits.begin(), _limits.end(), [](auto const& a, auto const& b)
            { return a.atMs < b.atMs; });
    *slot = {cast.caster, cast.spell, cast.tMs, true};
    _pristine = false;
    _live[_liveCount++] = cast;
    return true;
}

void PullTable::Death(std::uint32_t member, std::uint32_t tMs, std::int32_t x, std::int32_t y)
{
    auto row = Mutable(member);
    // Death callbacks are chronological for a member. Replayed/older callbacks do not invent another death.
    if (!row || (row->hadDeath && tMs <= row->lastDeathMs))
        return;
    auto const priorDeath = row->hadDeath;
    auto const priorDeathMs = row->lastDeathMs;
    row->hadDeath = true;
    row->lastDeathMs = tMs;
    row->deaths = SaturatingAdd(row->deaths, 1);
    if (_deathCount == kMaxDeaths)
        return;
    auto& death = _deaths[_deathCount++];
    death.guid = member;
    death.atMs = tMs;
    death.x = x;
    death.y = y;
    auto first = (row->hitNext + kLastHits - row->hitCount) % kLastHits;
    for (std::size_t i = 0; i < row->hitCount; ++i)
    {
        auto const& hit = row->hits[(first + i) % kLastHits];
        if (hit.atMs <= tMs && (!priorDeath || hit.atMs > priorDeathMs))
            death.hits[death.hitCount++] = hit;
    }
    // The ring retains the last eight arrivals. Present them chronologically and never cite a future hit.
    for (std::size_t i = 1; i < death.hitCount; ++i)
    {
        auto hit = death.hits[i];
        auto j = i;
        while (j && death.hits[j - 1].atMs > hit.atMs)
        {
            death.hits[j] = death.hits[j - 1];
            --j;
        }
        death.hits[j] = hit;
    }
    if (death.hitCount)
    {
        auto const& last = death.hits[death.hitCount - 1];
        death.spell = last.spell;
        death.source = last.source;
    }
}

void PullTable::Sample(std::uint32_t member, std::uint16_t tS, std::int32_t x, std::int32_t y,
    std::uint8_t hp, std::uint8_t power)
{
    auto row = Mutable(member);
    if (row && tS < kMaxSamples && !row->samples[tS].present && hp <= 100 && power <= 100)
        row->samples[tS] = {x, y, hp, power, true};
}

void PullTable::BossSample(std::uint16_t tS, std::uint32_t entry, std::int32_t x, std::int32_t y,
    std::int16_t o, std::uint8_t hp, std::uint32_t target)
{
    if (!_open || !entry || tS >= kMaxSamples || hp > 100 || _bossSamples[tS].present)
        return;
    SetBoss(entry);
    if (entry == _bossEntry)
        _bossSamples[tS] = {x, y, target, o, hp, true};
}

void PullTable::SetIlvl(std::uint32_t member, std::uint16_t ilvl)
{
    if (auto row = Mutable(member))
        row->ilvl = ilvl;
}

void PullTable::SetTank(std::uint32_t member, bool tank)
{
    if (auto row = Mutable(member))
        row->tank = tank;
}

void PullTable::SetSpecTab(std::uint32_t member, std::int8_t specTab)
{
    if (auto row = Mutable(member); row && row->specTab == -1 && specTab >= 0 && specTab <= 2)
        row->specTab = specTab;
}

void PullTable::Close(Outcome outcome, std::uint64_t endMs, std::uint32_t updMeanMs)
{
    if (!_open)
        return;
    _outcome = outcome;
    _endMs = std::max(endMs, _header.startMs);
    _updMeanMs = updMeanMs;
    _open = false;
    _closed = true;
}

std::size_t PullTable::DrainLive(std::vector<CreatureCast>& out)
{
    auto count = _liveCount;
    out.insert(out.end(), _live.begin(), _live.begin() + count);
    _liveCount = 0;
    return count;
}

std::uint64_t PullTable::ActiveMs(MemberRow const& row) const
{
    if (!_closed)
        return 0;
    auto duration = _endMs - _header.startMs;
    std::uint64_t total = 0;
    for (std::size_t i = 0; i < kMaxSamples && std::uint64_t(i) * 1000 < duration; ++i)
        if (row.samples[i].present && row.samples[i].hp)
            total += std::min<std::uint64_t>(1000, duration - std::uint64_t(i) * 1000);
    return total;
}

std::string PullId(PullHeader const& header)
{
    return std::to_string(header.startMs) + "-" + std::to_string(header.instanceId)
        + "-" + std::to_string(header.bossId);
}

std::vector<std::string> EncodePull(PullTable const& table, std::int64_t ts)
{
    std::vector<std::string> out;
    if (!table.Closed())
        return out;
    auto const& header = table.Header();
    auto const pull = PullId(header);
    auto const duration = table.EndMs() - header.startMs;
    std::vector<MemberRow const*> members;
    for (std::size_t i = 0; i < table.MemberCount(); ++i)
        members.push_back(&table.Members()[i]);
    std::sort(members.begin(), members.end(), [](auto a, auto b) { return a->guid < b->guid; });
    for (auto row : members)
        Append(out, ts, MemberBody(table, *row, pull));
    for (std::size_t i = 0; i < table.DeathCount(); ++i)
    {
        auto const& death = table.Deaths()[i];
        auto hits = json::array();
        for (std::size_t j = 0; j < death.hitCount; ++j)
        {
            auto const& hit = death.hits[j];
            hits.push_back({{"at_ms", hit.atMs}, {"spell", hit.spell}, {"source", hit.source},
                {"amount", hit.amount}, {"hp", hit.hp}});
        }
        Append(out, ts, {{"type", "pull_death"}, {"pull", pull}, {"guid", death.guid},
            {"at_ms", death.atMs}, {"spell", death.spell}, {"source", death.source},
            {"x", death.x}, {"y", death.y}, {"hits", std::move(hits)}});
    }
    std::size_t tracks = 0;
    for (auto row : members)
        Runs(row->samples, duration, [&](std::size_t begin, std::size_t end)
        {
            auto track = Track(pull, row->guid, 0, begin);
            for (auto i = begin; i != end; ++i)
            {
                auto const& point = row->samples[i];
                track["xs"].push_back(point.x);
                track["ys"].push_back(point.y);
                track["hp"].push_back(point.hp);
                track["power"].push_back(point.power);
            }
            Append(out, ts, std::move(track));
            ++tracks;
        });
    std::vector<TrackCast> casts(table.BossCasts().begin(), table.BossCasts().begin() + table.BossCastCount());
    std::sort(casts.begin(), casts.end(), [](auto const& a, auto const& b)
        { return std::tie(a.atS, a.spell) < std::tie(b.atS, b.spell); });
    auto encodedCasts = json::array();
    for (auto const& cast : casts)
        if (std::uint64_t(cast.atS) * 1000 <= duration)
            encodedCasts.push_back({{"spell", cast.spell}, {"at_s", cast.atS}});
    bool bossTrack = false;
    Runs(table.BossSamples(), duration, [&](std::size_t begin, std::size_t end)
    {
        auto track = Track(pull, 0, table.BossEntry(), begin);
        if (!bossTrack)
            track["casts"] = encodedCasts;
        bossTrack = true;
        for (auto i = begin; i != end; ++i)
        {
            auto const& point = table.BossSamples()[i];
            track["xs"].push_back(point.x);
            track["ys"].push_back(point.y);
            track["o"].push_back(point.o);
            track["hp"].push_back(point.hp);
            track["target"].push_back(point.target);
        }
        Append(out, ts, std::move(track));
        ++tracks;
    });
    // A very short pull can have casts but no one-second sample. Preserve those casts without inventing a point.
    if (!bossTrack && !encodedCasts.empty())
    {
        auto track = Track(pull, 0, table.BossEntry(), 0);
        track["casts"] = std::move(encodedCasts);
        Append(out, ts, std::move(track));
        ++tracks;
    }
    auto outcome = table.Result() == Outcome::Killed ? "killed"
        : table.Result() == Outcome::Wiped ? "wiped" : "aborted";
    Append(out, ts, {{"type", "pull"}, {"id", pull}, {"map", header.map}, {"instance_id", header.instanceId},
        {"boss_id", header.bossId}, {"difficulty", header.difficulty}, {"size", header.size}, {"outcome", outcome},
        {"start_ms", header.startMs}, {"end_ms", table.EndMs()}, {"members", table.MemberCount()},
        {"deaths", table.DeathCount()}, {"tracks", tracks}, {"assisted", table.Assisted()},
        {"source", header.source == Source::Tracked ? "tracked" : "benchmark"},
        {"damage_basis", "requested_pre_ai"},
        {"healing_basis", "effective_gain_with_pre_absorb_raw"},
        {"spell_basis", "best_effort_thread_pair"}, {"interrupt_basis", "successful_interrupt_spell_cast"},
        {"boss_entry", table.BossEntry()}, {"upd_mean_ms", table.UpdateMeanMs()}});
    return out;
}

std::string EncodeBossCast(PullHeader const& header, CreatureCast const& cast, std::int64_t ts)
{
    if (cast.state > 2)
        throw std::invalid_argument("invalid creature cast state");
    return EncodeEvent(ts, {{"type", "boss_cast"}, {"pull", PullId(header)}, {"map", header.map},
        {"instance_id", header.instanceId}, {"caster_entry", cast.casterEntry}, {"caster", cast.caster},
        {"spell", cast.spell}, {"target", cast.target},
        {"state", cast.state == 0 ? "start" : cast.state == 1 ? "done" : "cancelled"}, {"cast_ms", cast.castMs}});
}

std::string EncodePulse(PullHeader const& header, std::uint32_t atMs, std::uint8_t bossHp,
    std::uint32_t alive, std::int64_t ts)
{
    return EncodeEvent(ts, {{"type", "pull_pulse"}, {"pull", PullId(header)}, {"map", header.map},
        {"instance_id", header.instanceId}, {"boss_id", header.bossId}, {"at_ms", atMs},
        {"boss_hp", bossHp}, {"alive", alive}});
}
}
