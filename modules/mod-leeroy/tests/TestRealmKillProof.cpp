// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmCommands.h"
#include "LeeroyProtocol.h"
#include "TestHarness.h"

#include <algorithm>
#include <fstream>
#include <functional>
#include <iterator>
#include <limits>
#include <set>
#include <stdexcept>

using namespace Leeroy::Realm;
using json = nlohmann::json;

namespace
{
// Lib fixture only. This does not simulate or establish an InstanceScript DONE
// receipt; the real verifier remains in RealmSpeech's map-update callback.
Kill Proof(std::uint64_t serial = 1, std::uint64_t steady = 1000)
{
    Kill kill;
    kill.nativeEpoch = 7; kill.guildId = 21; kill.humanGuid = 1;
    kill.map = 631; kill.instanceId = 500; kill.bossId = 0;
    kill.difficulty = 1; kill.size = 25; kill.heroic = false;
    kill.killId = "0123456789abcdef0123456789abcdef:" + std::to_string(serial);
    kill.atMs = steady; kill.verifiedMs = steady;
    kill.killedAtMs = 1700000000000; kill.verifiedAtMs = 1700000000001;
    kill.publishedAtMs = 1700000000002;
    kill.humanPresent = true; kill.castMembers = {2, 3};
    return kill;
}

std::string Source(char const* file)
{
    auto const path = std::string(LEEROY_TEST_FIXTURES) + "/../../src/Realm/" + file;
    std::ifstream stream(path);
    if (!stream) throw std::runtime_error("cannot open native kill source contract: " + path);
    std::string source{std::istreambuf_iterator<char>(stream), std::istreambuf_iterator<char>()};
    source.erase(std::remove(source.begin(), source.end(), '\r'), source.end());
    return source;
}

std::string Section(std::string const& source, std::string const& start, std::string const& end)
{
    auto const first = source.find(start);
    auto const last = first == std::string::npos ? first : source.find(end, first + start.size());
    if (first == std::string::npos || last == std::string::npos)
        throw std::runtime_error("native kill source contract section missing: " + start);
    return source.substr(first, last - first);
}
}

LEEROY_TEST(RealmKillProof_IdIsExactCanonicalBootAndPositiveUint64Serial)
{
    CHECK(ValidKillId(Proof().killId));
    CHECK(ValidKillId(Proof(std::numeric_limits<std::uint64_t>::max()).killId));
    for (auto const& serial : {"0", "01", "-1", "+1", "1 ", "1:2", "1.0", "18446744073709551616"})
        CHECK(!ValidKillId(std::string("0123456789abcdef0123456789abcdef:") + serial));
    CHECK(!ValidKillId("0123456789ABCDEF0123456789abcdef:1"));
    CHECK(!ValidKillId("0123456789abcdef0123456789abcdef-1"));
    CHECK(!ValidKillId("0123456789abcdef0123456789abcde:1"));
    CHECK(!ValidKillId("0123456789abcdef0123456789abcdef:"));
}

LEEROY_TEST(RealmKillProof_ExactAIsNeverSatisfiedByNewerSameTupleB)
{
    KillRing ring;
    auto const a = Proof(1, 1000), b = Proof(2, 2000);
    CHECK(!ring.MatchProof(a.killId, a, 1000));
    CHECK(ring.Add(a)); CHECK(ring.Add(b));
    auto found = ring.MatchProof(a.killId, a, 3000);
    CHECK(found && *found == a);
    found = ring.MatchProof(b.killId, b, 3000);
    CHECK(found && *found == b);
    CHECK(!ring.MatchProof(Proof(3).killId, a, 3000));
    CHECK(!ring.MatchProof(a.killId, a, 601001));
    CHECK(ring.MatchProof(b.killId, b, 601001));
    for (auto const& change : std::vector<std::function<void(KillIdentity&)>>{
        [](auto& k) { ++k.nativeEpoch; }, [](auto& k) { ++k.guildId; }, [](auto& k) { ++k.humanGuid; },
        [](auto& k) { ++k.map; }, [](auto& k) { ++k.instanceId; }, [](auto& k) { ++k.bossId; },
        [](auto& k) { k.difficulty = -1; }, [](auto& k) { k.size = 10; }, [](auto& k) { k.heroic = true; }})
    {
        KillIdentity expected = a; change(expected);
        CHECK(!ring.MatchProof(a.killId, expected, 3000));
    }
}

LEEROY_TEST(RealmKillProof_SteadyRetentionIsBoundedWithoutClockConversionOrUnderflow)
{
    KillRing ring; auto kill = Proof(); CHECK(ring.Add(kill));
    CHECK(ring.MatchProof(kill.killId, kill, 601000));
    CHECK(!ring.MatchProof(kill.killId, kill, 601001));
    CHECK(!ring.MatchProof(kill.killId, kill, 999));
    CHECK(ring.MatchProof(kill.killId, kill, 1000, 0));
    CHECK(!ring.MatchProof(kill.killId, kill, 1001, 0));
    kill = Proof(2, std::numeric_limits<std::uint64_t>::max() - 5);
    CHECK(ring.Add(kill));
    CHECK(ring.MatchProof(kill.killId, kill, std::numeric_limits<std::uint64_t>::max(), 5));
    CHECK(!ring.MatchProof(kill.killId, kill, std::numeric_limits<std::uint64_t>::max(), 4));
    CHECK(!ring.MatchProof(kill.killId, kill, 3, std::numeric_limits<std::uint64_t>::max()));
}

LEEROY_TEST(RealmKillProof_RingEvictionAndDuplicateIdentityCannotSubstituteAnotherProof)
{
    KillRing ring;
    for (std::uint64_t serial = 1; serial <= KillRing::Capacity; ++serial) CHECK(ring.Add(Proof(serial)));
    CHECK(ring.Add(Proof())); // Exact replay consumes no new ring position.
    auto conflict = Proof(); ++conflict.verifiedAtMs;
    CHECK(!ring.Add(conflict)); // Same id cannot acquire another immutable body.
    auto found = ring.MatchProof(Proof().killId, Proof(), 1000);
    CHECK(found && *found == Proof());
    CHECK(ring.Add(Proof(33)));
    CHECK(!ring.MatchProof(Proof().killId, Proof(), 1000));
    CHECK(ring.MatchProof(Proof(2).killId, Proof(2), 1000));
    for (std::uint64_t serial = 34; serial <= 96; ++serial) CHECK(ring.Add(Proof(serial)));
    CHECK(!ring.MatchProof(Proof(64).killId, Proof(64), 1000));
    CHECK(ring.MatchProof(Proof(65).killId, Proof(65), 1000));
    CHECK(ring.MatchProof(Proof(96).killId, Proof(96), 1000));
}

LEEROY_TEST(RealmKillProof_IndependentUnixOrderAndSteadyVerificationBothMustHold)
{
    auto kill = Proof(); CHECK(ValidKillObservation(kill)); CHECK(ValidKillProof(kill));
    kill.verifiedMs = kill.atMs + 5000; CHECK(ValidKillProof(kill));
    ++kill.verifiedMs; CHECK(!ValidKillObservation(kill));
    kill = Proof(); --kill.verifiedMs; CHECK(!ValidKillObservation(kill));
    kill = Proof(); kill.atMs = 0; kill.verifiedMs = 0; CHECK(ValidKillProof(kill)); // Steady zero is valid.
    kill = Proof(); kill.verifiedAtMs = kill.killedAtMs; kill.publishedAtMs = kill.killedAtMs;
    CHECK(ValidKillProof(kill)); // Same millisecond is valid, without a tolerance.
    for (auto const& change : std::vector<std::function<void(Kill&)>>{
        [](auto& k) { k.nativeEpoch = 0; }, [](auto& k) { k.guildId = 0; },
        [](auto& k) { k.humanGuid = 0; }, [](auto& k) { k.instanceId = 0; },
        [](auto& k) { k.killedAtMs = 0; }, [](auto& k) { k.verifiedAtMs = k.killedAtMs - 1; },
        [](auto& k) { k.verifiedAtMs = std::numeric_limits<std::uint64_t>::max(); },
        [](auto& k) { k.size = 10; }, [](auto& k) { k.difficulty = 3; }})
    { kill = Proof(); change(kill); CHECK(!ValidKillObservation(kill)); }
    kill = Proof(); kill.publishedAtMs = kill.verifiedAtMs - 1; CHECK(!ValidKillProof(kill));
    kill = Proof(); kill.publishedAtMs = std::numeric_limits<std::uint64_t>::max(); CHECK(!ValidKillProof(kill));
    kill = Proof(); kill.killId.clear(); CHECK(ValidKillObservation(kill)); CHECK(!ValidKillProof(kill));
    KillRing ring; CHECK(!ring.Add(kill));
}

LEEROY_TEST(RealmKillProof_ParticipantsAreCompletePresentNativeIntersectionAndActualGuild)
{
    std::unordered_set<std::uint32_t> const capturedCast{2, 3, 4, 5, 6};
    auto found = QualifyKillParticipants({{1, 21}}, capturedCast, 21, 1);
    CHECK(found && found->humanPresent && found->castMembers.empty());
    auto humanOnly = Proof(); humanOnly.castMembers.clear();
    CHECK(MakeRealmKill(humanOnly).at("cast_members").is_array());
    CHECK(MakeRealmKill(humanOnly).at("cast_members").empty());
    found = QualifyKillParticipants({{6, 21}, {4, 22}, {1, 21}, {2, 21}, {9, 21}}, capturedCast, 21, 1);
    CHECK(found && found->humanPresent && found->castMembers == (std::vector<std::uint32_t>{2, 6}));
    // Offline native cast, present non-cast guild members, and another guild's
    // stale cast entry do not enter the output. Five-person persona policy is a
    // separate Python intersection, never invented by this native qualifier.
    found = QualifyKillParticipants({{1, 22}, {2, 21}}, capturedCast, 21, 1);
    CHECK(found && !found->humanPresent && found->castMembers == (std::vector<std::uint32_t>{2}));
    found = QualifyKillParticipants({{6, 21}, {4, 21}, {2, 21}, {5, 21}, {3, 21}}, capturedCast, 21, 1);
    CHECK(found && !found->humanPresent && found->castMembers == (std::vector<std::uint32_t>{2, 3, 4, 5, 6}));
    CHECK(!QualifyKillParticipants({{1, 22}, {9, 21}}, capturedCast, 21, 1));
    CHECK(!QualifyKillParticipants({}, capturedCast, 21, 1));
    CHECK(!QualifyKillParticipants({{1, 21}}, capturedCast, 0, 1));
    CHECK(!QualifyKillParticipants({{1, 21}}, capturedCast, 21, 0));
    CHECK(!QualifyKillParticipants({{1, 21}, {1, 22}}, capturedCast, 21, 1));
    CHECK(!QualifyKillParticipants({{1, 21}, {0, 21}}, capturedCast, 21, 1));
    std::vector<PresentKillPlayer> present;
    for (std::uint32_t guid = 1; guid <= kMaxKillPlayers; ++guid) present.push_back({guid, 21});
    CHECK(QualifyKillParticipants(present, capturedCast, 21, 1));
    present.push_back({129, 21});
    CHECK(!QualifyKillParticipants(present, capturedCast, 21, 1)); // Human in prefix cannot hide overflow.
}

LEEROY_TEST(RealmKillProof_MalformedParticipantProofNeverEntersRing)
{
    KillRing ring;
    for (auto const& members : std::vector<std::vector<std::uint32_t>>{{2, 2}, {3, 2}, {0, 2}, {1, 2}})
    { auto kill = Proof(); kill.castMembers = members; CHECK(!ring.Add(kill)); }
    auto kill = Proof(); kill.castMembers.clear(); CHECK(ring.Add(kill)); // Human alone is enough native evidence.
    kill = Proof(2); kill.castMembers.clear(); kill.humanPresent = false; CHECK(!ring.Add(kill));
    kill = Proof(3); kill.castMembers.clear();
    for (std::uint32_t guid = 2; guid <= 129; ++guid) kill.castMembers.push_back(guid);
    CHECK(!ring.Add(kill)); // Human plus 128 cast cannot fit a complete 128-player scan.
    kill.humanPresent = false; CHECK(ring.Add(kill));
    kill.killId = Proof(4).killId; kill.castMembers.push_back(130); CHECK(!ring.Add(kill));
}

LEEROY_TEST(RealmKillProof_EventHasExactFrozenFieldsAndNoSteadyClockLeak)
{
    auto const kill = Proof();
    auto const encoded = Leeroy::EncodeEvent(static_cast<std::int64_t>(kill.publishedAtMs), MakeRealmKill(kill));
    auto const event = json::parse(encoded);
    std::set<std::string> keys;
    for (auto const& [key, value] : event.items()) { (void)value; keys.insert(key); }
    CHECK((keys == std::set<std::string>{"v", "type", "ts", "kill_id", "native_epoch", "guild_id", "human_guid",
        "killed_at_ms", "verified_at_ms", "map", "instance_id", "boss_id", "difficulty", "size", "heroic",
        "human_present", "cast_members"}));
    CHECK_EQ(event.at("v"), 1); CHECK_EQ(event.at("type"), "realm_kill");
    CHECK_EQ(event.at("ts"), kill.publishedAtMs); CHECK_EQ(event.at("kill_id"), kill.killId);
    CHECK_EQ(event.at("killed_at_ms"), kill.killedAtMs); CHECK_EQ(event.at("verified_at_ms"), kill.verifiedAtMs);
    CHECK_EQ(event.at("cast_members"), json(kill.castMembers));
    CHECK_EQ(encoded, Leeroy::EncodeEvent(static_cast<std::int64_t>(kill.publishedAtMs), MakeRealmKill(kill)));
}

LEEROY_TEST(RealmKillProof_SourceContractKeepsDoneVerificationOnMapThreadAndWorldDrainOnce)
{
    // Source-placement regressions only, NOT runtime/core integration evidence.
    auto const speech = Source("RealmSpeech.cpp");
    auto const candidate = Section(speech, "void OnBeforeSetBossState", "class RealmKillMapScript");
    CHECK(candidate.find("RecordKill(") == std::string::npos);
    CHECK(candidate.find("old != IN_PROGRESS || next != DONE") != std::string::npos);
    CHECK(candidate.find("map->IsRaid()") != std::string::npos);
    CHECK(candidate.find("CastMembers()") < candidate.find("map->GetPlayers()"));
    CHECK(candidate.find("present.size() == kMaxKillPlayers) return") != std::string::npos);
    CHECK(candidate.find("NowMs()") != std::string::npos && candidate.find("NowUnixMs()") != std::string::npos);
    auto const verifier = Section(speech, "void OnMapUpdate(Map* map", "void RegisterRealmSpeechHandlers");
    CHECK(verifier.find("script->GetBossState(kill.bossId) == DONE") < verifier.find("module.RecordKill("));
    CHECK(verifier.find("map->GetSpawnMode() != pending.spawnMode") != std::string::npos);
    CHECK(verifier.find("life->second == lifecycle") != std::string::npos);
    CHECK(verifier.find("ValidKillObservation(kill)") < verifier.find("module.RecordKill("));
    auto const module = Source("RealmModule.cpp");
    auto const drain = Section(module, "void DrainKills()", "void ApplyPins()");
    CHECK(drain.find("auto observations = std::move(killInbox)") < drain.find("kills.Add(kill)"));
    CHECK(drain.find("killInbox.clear()") < drain.find("kills.Add(kill)"));
    CHECK(drain.find("kills.Add(kill)") < drain.find("Publish(std::move(line), false)"));
    CHECK(drain.find("}\n        for (auto& line : lines)") != std::string::npos); // Lock scope closed.
    CHECK(drain.find("NowUnixMs()") != std::string::npos);
    CHECK(drain.find("killSerial == std::numeric_limits<std::uint64_t>::max()") != std::string::npos);
    auto const matching = Section(module, "std::optional<Kill> RealmModule::MatchProof", "void RealmModule::NotifyBodyChanged");
    CHECK(matching.find("request.nativeEpoch != Epoch()") != std::string::npos);
    CHECK(matching.find("request.guildId != GuildId()") != std::string::npos);
    CHECK(matching.find("request.humanGuid != HumanGuid()") != std::string::npos);
    CHECK(speech.find("module.MatchKill(") == std::string::npos);
}
