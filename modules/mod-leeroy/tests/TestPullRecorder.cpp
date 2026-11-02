// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyPullRecorder.h"
#include "TestHarness.h"
#include <nlohmann/json.hpp>
#include <fstream>
#include <limits>
#include <memory>
#include <stdexcept>

using namespace Leeroy::Training;
using json = nlohmann::json;

namespace
{
PullHeader Header() { return {631, 77, 0, 1, 25, 1700000000000ULL, Source::Tracked}; }

std::unique_ptr<PullTable> Table()
{
    auto table = std::make_unique<PullTable>();
    table->Reset(Header());
    return table;
}

std::vector<json> Encoded(PullTable const& table)
{
    std::vector<json> out;
    for (auto const& line : EncodePull(table, 0))
        out.push_back(json::parse(line));
    return out;
}

std::vector<json> Select(std::vector<json> const& lines, std::string_view type)
{
    std::vector<json> out;
    for (auto const& row : lines)
        if (row.at("type").get<std::string>() == type)
            out.push_back(row);
    return out;
}
}

LEEROY_TEST(Training_PullTableTotals)
{
    auto t = Table();
    t->Member(1101, 6, 1);
    t->Member(1102, 1, 2);
    t->Member(1103, 5, 1);
    for (unsigned i = 0; i < 3; ++i)
        t->Damage({i, 1101, 0, 0, true, 49143, 1000, 0});
    t->Damage({10, 1101, 0, 0, true, kMeleeSpell, 500, 0});
    for (unsigned i = 0; i < 2; ++i)
        t->Damage({i, 0, 36672, 1102, false, 69146, 9000, 12000});
    t->Heal({1103, 1102, 100, 4000, 6000});
    CHECK_EQ(t->Find(1101)->damage, 3500);
    CHECK_EQ(t->Find(1101)->damageBoss, 3500);
    CHECK_EQ(t->Find(1101)->done[0].amount, 3000);
    CHECK_EQ(t->Find(1101)->done[0].hits, 3);
    CHECK_EQ(t->Find(1102)->taken, 18000);
    CHECK_EQ(t->Find(1102)->takenCount, 1);
    CHECK_EQ(t->Find(1102)->takenBy[0].hits, 2);
    CHECK_EQ(t->Find(1103)->healing, 4000);
    CHECK_EQ(t->Find(1103)->overheal, 2000);
    t->Heal({1103, 1102, 100, 3, 2});
    CHECK_EQ(t->Find(1103)->overheal, 2000); // No unsigned underflow for inconsistent native input.
}

LEEROY_TEST(Training_PullTableOverflowFoldsIntoZero)
{
    auto t = Table();
    for (std::uint32_t guid = 1; guid <= kMaxMembers; ++guid)
        CHECK_EQ(t->Member(guid, 1, 0), static_cast<int>(guid - 1));
    CHECK_EQ(t->Member(41, 1, 0), -1);
    CHECK_EQ(t->Member(0, 1, 0), -1);
    CHECK_EQ(t->Member(1, 8, 2), 0);
    CHECK_EQ(t->Find(1)->cls, 1); // Duplicate registration does not rewrite the pinned identity.
    for (std::uint32_t spell = 1; spell <= 40; ++spell)
    {
        t->Damage({spell, 1, 0, 0, true, spell, 10, 0});
        t->Damage({spell, 0, spell, 2, false, 100, 20, 100});
    }
    t->Damage({50, 1, 0, 0, true, 0, 5, 0});
    t->Damage({50, 41, 0, 99, true, 1, 5000, 0});
    t->Heal({41, 1, 10, 5000, 5000});
    CHECK_EQ(t->MemberCount(), 40);
    CHECK_EQ(t->Find(1)->damage, 405);
    CHECK_EQ(t->Find(1)->doneCount, 32);
    CHECK_EQ(t->Find(1)->done[kMaxSpells].amount, 85);
    CHECK_EQ(t->Find(1)->done[kMaxSpells].hits, 9);
    CHECK_EQ(t->Find(2)->takenCount, 32);
    CHECK_EQ(t->Find(2)->takenBy[kMaxTakenBy].amount, 160);
    CHECK_EQ(t->Find(2)->takenBy[kMaxTakenBy].spell, 0);
    CHECK_EQ(t->Find(2)->takenBy[kMaxTakenBy].source, 0);
    std::uint64_t sum = 0;
    for (auto const& entry : t->Find(1)->done)
        sum += entry.amount;
    CHECK_EQ(sum, t->Find(1)->damage);
}

LEEROY_TEST(Training_LastHitsRingAndOutOfOrderEvidence)
{
    auto t = Table();
    t->Member(1102, 1, 2);
    for (std::uint32_t i = 0; i < 10; ++i)
        t->Damage({i * 10, 0, 20, 1102, false, 100 + i, i, 200 - i});
    t->Death(1102, 100, -5, 7);
    auto const& death = t->Deaths()[0];
    CHECK_EQ(death.hitCount, 8);
    CHECK_EQ(death.hits[0].atMs, 20);
    CHECK_EQ(death.hits[7].atMs, 90);
    CHECK_EQ(death.hits[7].hp, 191);
    CHECK_EQ(death.spell, 109);
    t->Death(1102, 100, 0, 0);
    t->Death(1102, 99, 0, 0);
    CHECK_EQ(t->Find(1102)->deaths, 1);
    t->Damage({300, 0, 20, 1102, false, 999, 10, 10});
    t->Damage({150, 0, 30, 1102, false, 888, 10, 10});
    t->Death(1102, 200, 0, 0);
    auto const& second = t->Deaths()[1];
    CHECK_EQ(second.spell, 888);
    CHECK_EQ(second.source, 30);
    for (std::size_t i = 0; i < second.hitCount; ++i)
    {
        CHECK(second.hits[i].atMs <= 200);
        if (i) CHECK(second.hits[i - 1].atMs <= second.hits[i].atMs);
    }
}

LEEROY_TEST(Training_DeathsPastTheCapCountButAreNotDetailed)
{
    auto t = Table();
    t->Member(1101, 6, 1);
    for (std::uint32_t i = 0; i < 85; ++i)
        t->Death(1101, i, 0, 0);
    t->Death(99, 900, 0, 0);
    CHECK_EQ(t->Find(1101)->deaths, 85);
    CHECK_EQ(t->DeathCount(), 80);
    t->Close(Outcome::Wiped, Header().startMs + 1000, 1);
    auto lines = Encoded(*t);
    CHECK_EQ(Select(lines, "pull_death").size(), 80);
    CHECK_EQ(lines.back().at("deaths"), 80);
}

LEEROY_TEST(Training_DeathNeverAttributesAnEarlierLifeHit)
{
    auto t = Table();
    t->Member(1, 1, 0);
    t->Damage({10, 0, 9, 1, false, 40, 5, 10});
    t->Damage({300, 0, 8, 1, false, 50, 5, 10}); // A delayed death callback must preserve this later hit.
    t->Death(1, 20, 0, 0);
    t->Death(1, 200, 0, 0);
    CHECK_EQ(t->Deaths()[1].spell, 0);
    CHECK_EQ(t->Deaths()[1].source, 0);
    CHECK_EQ(t->Deaths()[1].hitCount, 0);
    t->Death(1, 400, 0, 0);
    CHECK_EQ(t->Deaths()[2].spell, 50);
    CHECK_EQ(t->Deaths()[2].hitCount, 1);
}

LEEROY_TEST(Training_LiveLimiterAndFullRing)
{
    auto t = Table();
    CHECK(t->Live({0, 30, 4012, 72762, 1, 2000, 0}));
    CHECK(!t->Live({999, 30, 4012, 72762, 1, 2000, 0}));
    CHECK(t->Live({1000, 30, 4012, 72762, 1, 0, 1}));
    CHECK(!t->Live({500, 30, 4012, 72762, 1, 0, 1}));
    CHECK(t->Live({10, 30, 4012, 72763, 1, 0, 2}));
    CHECK(!t->Live({10, 30, 4012, 72764, 1, 0, 3}));
    std::vector<CreatureCast> live;
    CHECK_EQ(t->DrainLive(live), 3);
    CHECK_EQ(live[0].tMs, 0);
    CHECK_EQ(live[1].tMs, 1000);
    CHECK_EQ(live[2].tMs, 10);
    CHECK_EQ(t->DrainLive(live), 0);
    CHECK_EQ(live.size(), 3);
    for (std::uint32_t i = 0; i < kLiveRing; ++i)
        CHECK(t->Live({2000 + i, 30, 5000 + i, 1, 1, 0, 0}));
    CHECK(!t->Live({3000, 30, 9999, 1, 1, 0, 0}));
    CHECK_EQ(t->DrainLive(live), kLiveRing);
    CHECK(t->Live({3000, 30, 9999, 1, 1, 0, 0})); // Full ring did not consume this key's allowance.
}

LEEROY_TEST(Training_LimiterEvictsOldestRatherThanHashCollision)
{
    auto t = Table();
    for (std::uint32_t i = 1; i <= kLimiterSlots; ++i)
        CHECK(t->Live({i, 10, i, 5, 0, 0, 0}));
    CHECK(t->Live({100, 10, 999, 5, 0, 0, 0}));
    CHECK(!t->Live({101, 10, 2, 5, 0, 0, 0}));
    CHECK(t->Live({101, 10, 1, 5, 0, 0, 0}));
}

LEEROY_TEST(Training_EncodeMatchesCoherentFixture)
{
    // Wire examples in training_protocol.jsonl are independent examples, not one physically coherent pull.
    auto t = Table();
    t->Member(1101, 6, 1);
    t->SetIlvl(1101, 264);
    for (std::uint32_t i = 0; i < 3; ++i)
        t->Damage({i, 1101, 0, 0, true, 49143, 1000, 0});
    t->Damage({1000, 0, 36672, 1101, false, 69146, 9000, 12000});
    t->Heal({1101, 1101, 1, 4000, 6000});
    t->Aura(1101, 69065, 1);
    t->Cast(1101, 49143, true);
    t->Death(1101, 1500, -3900, 22150);
    t->Sample(1101, 0, -3900, 22150, 100, 40);
    t->Sample(1101, 1, -3901, 22151, 50, 55);
    t->BossSample(0, 36612, -3880, 22100, 314, 100, 1101);
    t->BossSample(1, 36612, -3880, 22100, 310, 99, 1101);
    t->BossCast(69076, 1);
    t->Close(Outcome::Killed, Header().startMs + 2500, 38);
    std::ifstream file(LEEROY_TEST_FIXTURES "/training_pull.jsonl");
    CHECK(file.good());
    std::vector<json> expected;
    for (std::string line; std::getline(file, line);)
        if (!line.empty())
            expected.push_back(json::parse(line));
    CHECK(Encoded(*t) == expected);
}

LEEROY_TEST(Training_SparseSamplesAreNotFabricatedAndAliveTimeIsObserved)
{
    auto t = Table();
    t->Member(1101, 6, 1);
    t->Sample(1101, 4, 40, 41, 10, 20);
    t->Sample(1101, 0, 0, 1, 100, 20);
    t->Sample(1101, 1, 10, 11, 0, 20);
    t->Sample(1101, 4, 99, 99, 0, 0); // First observation per second is immutable.
    t->Sample(1101, 5, 50, 51, 100, 20); // Beyond close: never attributed to this pull.
    t->Sample(1101, 1200, 1, 1, 100, 20);
    t->Sample(9999, 0, 1, 1, 100, 20);
    t->Sample(1101, 2, 1, 1, 101, 20);
    t->BossSample(1, 9, 1, 2, 0, 100, 1101);
    t->BossSample(3, 9, 3, 4, 0, 99, 1101);
    t->BossSample(2, 10, 2, 3, 0, 100, 1101); // A different creature cannot overwrite the boss track.
    t->BossCast(50, 3);
    t->BossCast(40, 1);
    t->BossCast(30, 10);
    t->Close(Outcome::Wiped, Header().startMs + 4500, 10);
    CHECK_EQ(t->ActiveMs(*t->Find(1101)), 1500);
    auto tracks = Select(Encoded(*t), "pull_track");
    CHECK_EQ(tracks.size(), 4);
    if (tracks.size() != 4) return;
    CHECK_EQ(tracks[0].at("from_s"), 0);
    CHECK(tracks[0].at("xs") == json::array({0, 10}));
    CHECK_EQ(tracks[1].at("from_s"), 4);
    CHECK(tracks[1].at("xs") == json::array({40}));
    CHECK_EQ(tracks[2].at("from_s"), 1);
    CHECK_EQ(tracks[2].at("casts").size(), 2);
    CHECK_EQ(tracks[2].at("casts")[0].at("spell"), 40);
    CHECK_EQ(tracks[3].at("from_s"), 3);
    CHECK(tracks[3].at("casts").empty());
}

LEEROY_TEST(Training_TrackChunkBoundaryAndCastOnlyPull)
{
    auto t = Table();
    t->Member(1101, 6, 1);
    for (std::uint16_t s = 0; s <= 600; ++s)
        t->Sample(1101, s, 0, 0, 100, 100);
    t->SetBoss(9);
    t->BossCast(40, 0);
    t->Close(Outcome::Killed, Header().startMs + 601000, 1);
    auto tracks = Select(Encoded(*t), "pull_track");
    CHECK_EQ(tracks.size(), 3);
    if (tracks.size() != 3) return;
    CHECK_EQ(tracks[0].at("xs").size(), 600);
    CHECK_EQ(tracks[1].at("from_s"), 600);
    CHECK_EQ(tracks[1].at("xs").size(), 1);
    CHECK(tracks[2].at("xs").empty());
    CHECK_EQ(tracks[2].at("casts").size(), 1);
}

LEEROY_TEST(Training_CumulativeCountersNeverWrapAndEqualHitsAreRealEvents)
{
    auto t = Table();
    t->Member(1, 1, 0);
    auto max32 = std::numeric_limits<std::uint32_t>::max();
    for (unsigned i = 0; i < 3; ++i)
    {
        t->Damage({0, 1, 10, 1, true, 10, max32, max32});
        t->Heal({1, 1, 10, 0, max32});
    }
    CHECK_EQ(t->Find(1)->damage, std::uint64_t(max32) * 3);
    CHECK_EQ(t->Find(1)->taken, std::uint64_t(max32) * 3);
    CHECK_EQ(t->Find(1)->overheal, std::uint64_t(max32) * 3);
    CHECK_EQ(t->Find(1)->done[0].hits, 3);
    auto max64 = std::numeric_limits<std::uint64_t>::max();
    CHECK_EQ(SaturatingAdd(max64 - 2, 2), max64);
    CHECK_EQ(SaturatingAdd(max64 - 2, 3), max64);
    CHECK_EQ(SaturatingAdd(max64, max64), max64);
}

LEEROY_TEST(Training_CloseAndResetLifecycle)
{
    auto t = Table();
    CHECK(EncodePull(*t, 0).empty());
    t->Member(1, 1, 0);
    t->SetTank(1, true);
    t->MarkAssisted();
    t->Sample(1, 0, 0, 0, 100, 100);
    t->Close(Outcome::Aborted, Header().startMs - 1, 5);
    auto before = EncodePull(*t, 0);
    CHECK_EQ(t->ActiveMs(*t->Find(1)), 0);
    CHECK_EQ(t->EndMs(), Header().startMs);
    CHECK_EQ(t->Member(2, 2, 0), -1);
    t->Damage({0, 1, 0, 0, true, 1, 100, 0});
    t->SetTank(1, false);
    t->Close(Outcome::Killed, Header().startMs + 1000, 5);
    CHECK(EncodePull(*t, 0) == before);
    auto next = Header();
    next.source = Source::Benchmark;
    t->Reset(next);
    CHECK_EQ(t->MemberCount(), 0);
    CHECK_EQ(t->DeathCount(), 0);
    CHECK(!t->Closed());
    CHECK(!t->Assisted());
    CHECK_EQ(t->BossEntry(), 0);
    CHECK(t->Live({0, 1, 1, 1, 0, 0, 0}));
    t->Close(Outcome::Killed, next.startMs, 0);
    CHECK_EQ(Encoded(*t).back().at("source"), "benchmark");
}

LEEROY_TEST(Training_EncodeLargestPullUnderLimit)
{
    auto t = Table();
    auto max = std::numeric_limits<std::uint32_t>::max();
    auto low = std::numeric_limits<std::int32_t>::min();
    auto high = std::numeric_limits<std::int32_t>::max();
    for (std::uint32_t guid = 1; guid <= kMaxMembers; ++guid)
    {
        t->Member(guid, 11, 2);
        for (std::uint32_t spell = 1; spell <= 40; ++spell)
            t->Damage({spell, guid, max - spell, guid, true, max - spell, max, max});
        for (std::uint32_t spell = 1; spell <= 30; ++spell)
            t->Aura(guid, max - spell, 255);
        for (std::uint32_t spell = 1; spell <= 55; ++spell)
            t->Cast(guid, max - spell, true);
        t->Death(guid, 100, low, high);
        t->Death(guid, 200, low, high);
        for (std::uint16_t s = 0; s < 1201; ++s)
            t->Sample(guid, s, low, high, 100, 100);
    }
    for (std::uint16_t s = 0; s < 1201; ++s)
        t->BossSample(s, max, low, high, -32768, 100, max);
    for (std::uint16_t s = 0; s < 70; ++s)
        t->BossCast(max - s, s);
    t->Close(Outcome::Killed, Header().startMs + 1200000, max);
    auto lines = EncodePull(*t, 0);
    for (auto const& line : lines)
        CHECK(line.size() < kLineLimit);
    auto decoded = Encoded(*t);
    auto tracks = Select(decoded, "pull_track");
    CHECK_EQ(tracks.size(), 82);
    CHECK_EQ(decoded.back().at("members"), 40);
    CHECK_EQ(decoded.back().at("deaths"), 80);
    CHECK_EQ(decoded.back().at("tracks"), 82);
    CHECK_EQ(t->BossCastCount(), 64);
    CHECK_EQ(t->Find(1)->debuffCount, 24);
    CHECK_EQ(t->Find(1)->castCount, 48);
    CHECK_EQ(t->Find(1)->interrupts, 55);
    CHECK_EQ(t->ActiveMs(*t->Find(1)), 1200000);
    for (std::size_t i = 0; i < tracks.size(); ++i)
    {
        CHECK_EQ(tracks[i].at("from_s"), i % 2 ? 600 : 0);
        CHECK_EQ(tracks[i].at("xs").size(), 600);
        CHECK_EQ(tracks[i].at("ys").size(), 600);
    }
    CHECK_EQ(tracks[80].at("casts").size(), 64);
    CHECK(tracks[81].at("casts").empty());
}

LEEROY_TEST(Training_LiveEncodersMatchWireExamples)
{
    std::ifstream file(LEEROY_TEST_FIXTURES "/training_protocol.jsonl");
    CHECK(file.good());
    auto header = Header();
    header.bossId = 12;
    for (std::string line; std::getline(file, line);)
    {
        auto fixture = json::parse(line);
        if (fixture.at("name") == "boss_cast")
            CHECK(json::parse(EncodeBossCast(header, {0, 36597, 4012, 72762, 1, 2000, 0}, 0))
                == fixture.at("msg"));
        if (fixture.at("name") == "pull_pulse")
            CHECK(json::parse(EncodePulse(header, 45000, 88, 25, 0)) == fixture.at("msg"));
    }
}

LEEROY_TEST(Training_EncoderSortsTotalsWithStableNumericTies)
{
    auto t = Table();
    t->Member(9, 1, 0);
    t->Member(1, 1, 0);
    t->Damage({0, 9, 0, 0, false, 30, 10, 0});
    t->Damage({0, 9, 0, 0, false, 20, 10, 0});
    t->Damage({0, 9, 0, 0, false, 40, 20, 0});
    t->Aura(9, 30, 2);
    t->Aura(9, 20, 3);
    t->Aura(9, 40, 1);
    t->Aura(9, 40, 4);
    t->Cast(9, 30, false);
    t->Cast(9, 20, false);
    t->Close(Outcome::Killed, Header().startMs + 1000, 1);
    auto rows = Select(Encoded(*t), "pull_member");
    CHECK_EQ(rows[0].at("guid"), 1);
    CHECK_EQ(rows[1].at("guid"), 9);
    CHECK_EQ(rows[1].at("done")[0].at("spell"), 40);
    CHECK_EQ(rows[1].at("done")[1].at("spell"), 20);
    CHECK_EQ(rows[1].at("done")[2].at("spell"), 30);
    CHECK_EQ(rows[1].at("debuffs")[0].at("spell"), 40);
    CHECK_EQ(rows[1].at("debuffs")[0].at("max_stack"), 4);
    CHECK_EQ(rows[1].at("casts")[0].at("spell"), 20);
    CHECK(EncodePull(*t, 0) == EncodePull(*t, 0));
}
