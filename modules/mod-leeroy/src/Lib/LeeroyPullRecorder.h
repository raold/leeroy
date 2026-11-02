// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_PULL_RECORDER_H
#define MOD_LEEROY_PULL_RECORDER_H

#include <array>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <string>
#include <vector>

namespace Leeroy::Training
{
inline constexpr std::size_t kMaxMembers = 40, kMaxSpells = 32, kMaxTakenBy = 32, kMaxDebuffs = 24,
    kMaxCasts = 48, kLastHits = 8, kMaxDeaths = 80, kMaxSamples = 1200, kTrackChunk = 600,
    kLiveRing = 256, kLimiterSlots = 64, kMaxBossCasts = 64, kLineLimit = 60000;
inline constexpr std::uint32_t kMeleeSpell = 6603;

// Totals never wrap. Exact up to UINT64_MAX; beyond that they remain saturated.
constexpr std::uint64_t SaturatingAdd(std::uint64_t a, std::uint64_t b)
{
    return b > std::numeric_limits<std::uint64_t>::max() - a
        ? std::numeric_limits<std::uint64_t>::max() : a + b;
}

enum class Outcome { Killed, Wiped, Aborted };
enum class Source { Tracked, Benchmark };
struct PullHeader
{
    std::uint32_t map = 0, instanceId = 0, bossId = 0;
    std::uint8_t difficulty = 0, size = 0;
    std::uint64_t startMs = 0;
    Source source = Source::Tracked;
};
struct DamageHit
{
    std::uint32_t tMs = 0, attacker = 0, attackerEntry = 0, victim = 0;
    bool victimIsBoss = false;
    std::uint32_t spell = 0, amount = 0, victimHpBefore = 0;
};
struct HealHit { std::uint32_t healer = 0, target = 0, spell = 0, effective = 0, raw = 0; };
struct CreatureCast
{
    std::uint32_t tMs = 0, casterEntry = 0, caster = 0, spell = 0, target = 0, castMs = 0;
    std::uint8_t state = 0; // 0 start, 1 done, 2 cancelled
};
struct SpellTotal { std::uint32_t spell = 0; std::uint64_t amount = 0, hits = 0; };
struct TakenTotal
{
    std::uint32_t spell = 0, source = 0;
    std::uint64_t amount = 0, hits = 0;
};
struct DebuffTotal { std::uint32_t spell = 0; std::uint64_t count = 0; std::uint8_t maxStack = 0; };
struct CastTotal { std::uint32_t spell = 0; std::uint64_t count = 0; };
struct RecentHit { std::uint32_t atMs = 0, spell = 0, source = 0, amount = 0, hp = 0; };
struct MemberSample
{
    std::int32_t x = 0, y = 0;
    std::uint8_t hp = 0, power = 0;
    bool present = false;
};
struct BossPoint
{
    std::int32_t x = 0, y = 0;
    std::uint32_t target = 0;
    std::int16_t o = 0;
    std::uint8_t hp = 0;
    bool present = false;
};
struct TrackCast { std::uint32_t spell = 0; std::uint16_t atS = 0; };
struct MemberRow
{
    std::uint32_t guid = 0;
    std::uint8_t cls = 0;
    std::int8_t specTab = -1;
    bool tank = false;
    std::uint16_t ilvl = 0;
    std::uint64_t damage = 0, damageBoss = 0, healing = 0, overheal = 0, taken = 0, deaths = 0, interrupts = 0;
    // The last slot is a separate overflow/unknown bucket, leaving all named slots available.
    std::array<SpellTotal, kMaxSpells + 1> done{};
    std::array<TakenTotal, kMaxTakenBy + 1> takenBy{};
    std::array<DebuffTotal, kMaxDebuffs> debuffs{};
    std::array<CastTotal, kMaxCasts> casts{};
    std::array<RecentHit, kLastHits> hits{};
    std::array<MemberSample, kMaxSamples> samples{};
    std::size_t doneCount = 0, takenCount = 0, debuffCount = 0, castCount = 0, hitCount = 0, hitNext = 0;
    std::uint32_t lastDeathMs = 0;
    bool hadDeath = false;
};
struct DeathRow
{
    std::uint32_t guid = 0, atMs = 0, spell = 0, source = 0;
    std::int32_t x = 0, y = 0;
    std::array<RecentHit, kLastHits> hits{};
    std::size_t hitCount = 0;
};

// One owner at a time; the tap supplies the map/encoder handoff barrier. No hook method allocates.
// Reset is encoder/startup work. Allocate tables on the heap, rather than the small Windows thread stack.
class PullTable
{
public:
    void Reset(PullHeader const&);
    // Metadata only, for a pristine startup/encoder-prepared table. Never clears storage on a map hook.
    bool Arm(PullHeader const&);
    int Member(std::uint32_t guid, std::uint8_t cls, std::int8_t specTab);
    void Damage(DamageHit const&);
    void Heal(HealHit const&);
    void Aura(std::uint32_t member, std::uint32_t spell, std::uint8_t stack);
    void Cast(std::uint32_t member, std::uint32_t spell, bool interrupt);
    void BossCast(std::uint32_t spell, std::uint16_t tS);
    bool Live(CreatureCast const&);
    void Death(std::uint32_t member, std::uint32_t tMs, std::int32_t x, std::int32_t y);
    void Sample(std::uint32_t member, std::uint16_t tS, std::int32_t x, std::int32_t y,
        std::uint8_t hp, std::uint8_t power);
    void BossSample(std::uint16_t tS, std::uint32_t entry, std::int32_t x, std::int32_t y,
        std::int16_t o, std::uint8_t hp, std::uint32_t target);
    void SetIlvl(std::uint32_t member, std::uint16_t ilvl);
    void SetTank(std::uint32_t member, bool tank);
    void SetSpecTab(std::uint32_t member, std::int8_t specTab);
    void MarkAssisted() { if (_open) { _assisted = true; _pristine = false; } }
    void SetBoss(std::uint32_t entry)
    {
        if (_open && entry && !_bossEntry) { _bossEntry = entry; _pristine = false; }
    }
    void Close(Outcome, std::uint64_t endMs, std::uint32_t updMeanMs);
    std::size_t DrainLive(std::vector<CreatureCast>& out);

    PullHeader const& Header() const { return _header; }
    MemberRow const* Find(std::uint32_t guid) const;
    auto const& Members() const { return _members; }
    auto const& Deaths() const { return _deaths; }
    auto const& BossSamples() const { return _bossSamples; }
    auto const& BossCasts() const { return _bossCasts; }
    std::size_t MemberCount() const { return _memberCount; }
    std::size_t DeathCount() const { return _deathCount; }
    std::size_t BossCastCount() const { return _bossCastCount; }
    std::uint32_t BossEntry() const { return _bossEntry; }
    std::uint64_t EndMs() const { return _endMs; }
    std::uint32_t UpdateMeanMs() const { return _updMeanMs; }
    Outcome Result() const { return _outcome; }
    bool Assisted() const { return _assisted; }
    bool Closed() const { return _closed; }
    // Observed alive milliseconds only: first sample wins per second; missing seconds count as zero.
    // Each sample covers at most its second, clipped to closed duration. This is not combat engagement time.
    std::uint64_t ActiveMs(MemberRow const&) const;

private:
    MemberRow* Mutable(std::uint32_t guid);
    struct LimitEntry { std::uint32_t caster = 0, spell = 0, atMs = 0; bool used = false; };
    PullHeader _header{};
    std::array<MemberRow, kMaxMembers> _members{};
    std::array<DeathRow, kMaxDeaths> _deaths{};
    std::array<BossPoint, kMaxSamples> _bossSamples{};
    std::array<TrackCast, kMaxBossCasts> _bossCasts{};
    std::array<CreatureCast, kLiveRing> _live{};
    std::array<LimitEntry, kLimiterSlots> _limits{};
    std::size_t _memberCount = 0, _deathCount = 0, _bossCastCount = 0, _liveCount = 0;
    std::uint32_t _bossEntry = 0, _updMeanMs = 0;
    std::uint64_t _endMs = 0;
    Outcome _outcome = Outcome::Aborted;
    bool _open = false, _closed = false, _assisted = false;
    bool _pristine = false;
};

std::string PullId(PullHeader const&);
std::vector<std::string> EncodePull(PullTable const&, std::int64_t ts);
std::string EncodeBossCast(PullHeader const&, CreatureCast const&, std::int64_t ts);
std::string EncodePulse(PullHeader const&, std::uint32_t atMs, std::uint8_t bossHp,
    std::uint32_t alive, std::int64_t ts);
}
#endif
