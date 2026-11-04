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

FightContext Fight(std::uint32_t boss = 0)
{
    FightContext context;
    static_cast<KillIdentity&>(context) = Proof();
    context.bossId = boss; context.mapIdentity = 123; context.lifecycle = 9; context.spawnMode = 1;
    return context;
}
void Start(FightStarts& starts, FightContext const& context, std::uint64_t at = 0, bool human = true)
{
    starts.Intent(context, FightState::Resting, FightState::Progress, FightState::Resting,
        at, 1699999999000, human);
}
FightDuration End(FightStarts& starts, FightContext const& context, std::uint64_t at,
    bool human = true, std::uint64_t unixMs = 1700000000000)
{
    auto const generation = starts.Intent(context, FightState::Progress, FightState::Done, FightState::Progress,
        at, unixMs, human);
    auto terminal = starts.CaptureTerminal(context, generation, at, unixMs, human);
    return starts.Finish(terminal, context, FightState::Done, at);
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
        "human_present", "cast_members", "fight_started_at_ms", "fight_ms"}));
    CHECK_EQ(event.at("v"), 1); CHECK_EQ(event.at("type"), "realm_kill");
    CHECK_EQ(event.at("ts"), kill.publishedAtMs); CHECK_EQ(event.at("kill_id"), kill.killId);
    CHECK_EQ(event.at("killed_at_ms"), kill.killedAtMs); CHECK_EQ(event.at("verified_at_ms"), kill.verifiedAtMs);
    CHECK_EQ(event.at("fight_started_at_ms"), 0); CHECK_EQ(event.at("fight_ms"), -1);
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
    auto const scan = Section(speech, "std::optional<KillParticipants> PresentParticipants", "class RealmBossScript");
    CHECK(scan.find("CastMembers()") < scan.find("map->GetPlayers()"));
    CHECK(scan.find("present.size() == kMaxKillPlayers) return std::nullopt") != std::string::npos);
    CHECK(candidate.find("script->GetBossState(id)") < candidate.find("fightStarts.Intent("));
    CHECK(candidate.find("old == NOT_STARTED || old == FAIL") != std::string::npos);
    CHECK(candidate.find("pending.identity == map && pending.kill.bossId == id") < candidate.find("fightStarts.CaptureTerminal("));
    CHECK(candidate.find("generation, kill.atMs, kill.killedAtMs, kill.humanPresent") != std::string::npos);
    CHECK(candidate.find("NowMs()") != std::string::npos && candidate.find("NowUnixMs()") != std::string::npos);
    auto const verifier = Section(speech, "void OnMapUpdate(Map* map", "void RegisterRealmSpeechHandlers");
    CHECK(verifier.find("script->GetBossState(kill.bossId)") < verifier.find("module.RecordKill("));
    CHECK(verifier.find("fightStarts.Finish(") < verifier.find("kill.fightMs = duration.elapsedMs"));
    CHECK(verifier.find("Finish(pending.fight, context, ObservedFightState(actual), now)") != std::string::npos);
    CHECK(verifier.find("fightStarts.ObserveActual(context, actual, now)") != std::string::npos);
    CHECK(verifier.find("map->GetSpawnMode() != pending.spawnMode") != std::string::npos);
    CHECK(verifier.find("life->second != lifecycle") != std::string::npos);
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
    auto const announce = Section(speech, "LeeroyCommandResult Announce(", "class RealmChannelScript");
    CHECK(announce.find("module.MatchProof(") < announce.find("ValidatedAnnouncementFightSeconds("));
    CHECK(announce.find("ValidatedAnnouncementFightSeconds(") < announce.find("SendServerMessage("));
    CHECK(announce.find("0, *fightSeconds, cmd->first") != std::string::npos);
    CHECK(speech.find("module.MatchKill(") == std::string::npos);
}

LEEROY_TEST(RealmFight_StartIntentNeedsActualAcceptanceIncludingFastTerminalConfirmation)
{
    auto const context = Fight();
    FightStarts vetoed; Start(vetoed, context, 100);
    vetoed.ObserveActual(context, FightState::Resting, 101);
    CHECK(End(vetoed, context, 150) == FightDuration{});
    FightStarts accepted; Start(accepted, context, 100);
    accepted.ObserveActual(context, FightState::Progress, 101);
    CHECK(End(accepted, context, 150) == (FightDuration{1699999999000, 50}));
    FightStarts fast; Start(fast, context, 100);
    CHECK(End(fast, context, 100) == (FightDuration{1699999999000, 0}));
    CHECK_EQ(fast.Size(), 0u);
    CHECK(End(fast, context, 101) == FightDuration{}); // Consumed once.
    FightStarts missing;
    CHECK(End(missing, context, 100) == FightDuration{}); // Old Progress cannot invent a start.
}

LEEROY_TEST(RealmFight_PendingConfirmationHasIndependentFiveSecondBound)
{
    for (auto delay : {5000u, 5001u})
    {
        FightStarts starts; auto const context = Fight(); Start(starts, context);
        starts.ObserveActual(context, FightState::Progress, delay);
        auto const duration = End(starts, context, delay);
        CHECK(duration == (delay == 5000 ? FightDuration{1699999999000, 5000} : FightDuration{}));
        FightStarts fast; Start(fast, context);
        CHECK(End(fast, context, delay) == duration); // Same bound for same-thread terminal confirmation.
    }
}

LEEROY_TEST(RealmFight_TerminalTimeIsImmutableAcrossVerifierDelayAndDistinctMapSamples)
{
    auto const context = Fight(); FightStarts starts; Start(starts, context, 100);
    starts.ObserveActual(context, FightState::Progress, 101);
    auto const token = starts.Intent(context, FightState::Progress, FightState::Done, FightState::Progress,
        1100, 1700000000000, true);
    auto terminal = starts.CaptureTerminal(context, token, 1100, 1700000000000, true);
    CHECK(starts.Finish(terminal, context, FightState::Done, 6100)
        == (FightDuration{1699999999000, 1000})); // Verifier delay never enters elapsed time.
    CHECK(starts.Finish(terminal, context, FightState::Done, 6100) == FightDuration{});
    for (auto elapsed : {3600000u, 3600001u})
    {
        FightStarts boundary; Start(boundary, context); boundary.ObserveActual(context, FightState::Progress, 1);
        auto const generation = boundary.Intent(context, FightState::Progress, FightState::Done, FightState::Progress,
            elapsed, 1700000000000, true);
        auto snapshot = boundary.CaptureTerminal(context, generation, elapsed, 1700000000000, true);
        CHECK(boundary.Finish(snapshot, context, FightState::Done, elapsed + 1) == (elapsed == 3600000
            ? FightDuration{1699999999000, 3600000} : FightDuration{}));
        CHECK_EQ(boundary.Size(), 0u);
    }
    auto otherMap = context; ++otherMap.mapIdentity; ++otherMap.instanceId;
    FightStarts interleaved; Start(interleaved, context, 100); Start(interleaved, otherMap, 200);
    interleaved.ObserveActual(context, FightState::Progress, 150); // Earlier sample from another map thread.
    interleaved.ObserveActual(otherMap, FightState::Progress, 201);
    CHECK(End(interleaved, context, 300) == (FightDuration{1699999999000, 200}));
    CHECK(End(interleaved, otherMap, 301) == (FightDuration{1699999999000, 101}));
}

LEEROY_TEST(RealmFight_DuplicateProgressNeverResetsOrCreatesStartAndAmbiguityRevokes)
{
    auto const context = Fight(); FightStarts starts; Start(starts, context, 100);
    starts.ObserveActual(context, FightState::Progress, 101);
    starts.Intent(context, FightState::Progress, FightState::Progress, FightState::Progress, 200, 1700000000000, true);
    CHECK(End(starts, context, 1000) == (FightDuration{1699999999000, 900}));
    FightStarts missing;
    missing.Intent(context, FightState::Progress, FightState::Progress, FightState::Progress, 200, 1700000000000, true);
    CHECK(End(missing, context, 1000) == FightDuration{});
    FightStarts overlap; Start(overlap, context, 100); Start(overlap, context, 101);
    CHECK(End(overlap, context, 102) == FightDuration{});
    for (auto old : {FightState::Done, FightState::Other})
    {
        FightStarts ambiguous;
        ambiguous.Intent(context, old, FightState::Progress, old, 100, 1700000000000, true);
        CHECK(End(ambiguous, context, 101) == FightDuration{});
    }
}

LEEROY_TEST(RealmFight_VetoedDoneRetainsOnlyActuallyContinuingExactStart)
{
    auto const context = Fight();
    for (auto actual : {FightState::Progress, FightState::Resting, FightState::Other})
    {
        FightStarts starts; Start(starts, context, 100); starts.ObserveActual(context, FightState::Progress, 101);
        auto const token = starts.Intent(context, FightState::Progress, FightState::Done, FightState::Progress,
            200, 1700000000000, true);
        CHECK(token);
        auto terminal = starts.CaptureTerminal(context, token, 200, 1700000000000, true);
        CHECK(starts.Finish(terminal, context, actual, 201) == FightDuration{});
        CHECK(End(starts, context, 400) == (actual == FightState::Progress
            ? FightDuration{1699999999000, 300} : FightDuration{}));
    }
}

LEEROY_TEST(RealmFight_ResetWipeAndFreshGenerationCannotReuseAnEarlierTerminal)
{
    auto const context = Fight(); FightStarts starts; Start(starts, context);
    starts.ObserveActual(context, FightState::Progress, 1);
    auto const oldToken = starts.Intent(context, FightState::Progress, FightState::Done, FightState::Progress,
        10, 1700000000000, true);
    auto oldTerminal = starts.CaptureTerminal(context, oldToken, 10, 1700000000000, true);
    starts.Intent(context, FightState::Progress, FightState::Resting, FightState::Progress, 11, 1700000000000, true);
    CHECK(End(starts, context, 12) == FightDuration{});
    Start(starts, context, 20); starts.ObserveActual(context, FightState::Progress, 21);
    auto const freshToken = starts.Intent(context, FightState::Progress, FightState::Done, FightState::Progress,
        30, 1700000000000, true);
    CHECK(freshToken && freshToken != oldToken);
    CHECK(starts.Finish(oldTerminal, context, FightState::Done, 30) == FightDuration{});
    CHECK_EQ(starts.Size(), 0u); // The stale generation cannot attach to the fresh DONE.
    Start(starts, context, 40); starts.ObserveActual(context, FightState::Progress, 41);
    CHECK(End(starts, context, 50) == (FightDuration{1699999999000, 10}));
}

LEEROY_TEST(RealmFight_LifecyclePointerReuseEpochIdentityModeAndDisableRevoke)
{
    for (auto const& change : std::vector<std::function<void(FightContext&)>>{
        [](auto& c) { ++c.lifecycle; }, [](auto& c) { ++c.nativeEpoch; }, [](auto& c) { ++c.guildId; },
        [](auto& c) { ++c.humanGuid; }, [](auto& c) { ++c.map; }, [](auto& c) { ++c.instanceId; },
        [](auto& c) { ++c.spawnMode; }, [](auto& c) { c.difficulty = 3; c.heroic = true; },
        [](auto& c) { c.difficulty = 0; c.size = 10; }})
    {
        auto const original = Fight(); auto changed = original; change(changed);
        FightStarts starts; Start(starts, original); starts.ObserveActual(original, FightState::Progress, 1);
        CHECK(End(starts, changed, 10) == FightDuration{});
        CHECK(End(starts, original, 11) == FightDuration{}); // Switching back cannot recover it.
    }
    auto const context = Fight();
    for (auto clear : {0u, 1u, 2u, 3u, 4u})
    {
        FightStarts starts; Start(starts, context); starts.ObserveActual(context, FightState::Progress, 1);
        if (clear == 0) starts.Clear();
        else if (clear == 1) starts.ForgetMap(context.mapIdentity);
        else if (clear == 2) starts.Synchronize(context.nativeEpoch + 1, context.guildId, context.humanGuid, 2);
        else if (clear == 3) starts.Synchronize(context.nativeEpoch, context.guildId + 1, context.humanGuid, 2);
        else starts.Synchronize(context.nativeEpoch, context.guildId, context.humanGuid + 1, 2);
        CHECK(End(starts, context, 10) == FightDuration{});
    }
}

LEEROY_TEST(RealmFight_HumanAtBothEndpointsAndCompleteScanAreRequiredOnlyForDuration)
{
    auto const context = Fight();
    for (auto endpoints : {0u, 1u, 2u, 3u})
    {
        FightStarts starts; Start(starts, context, 0, (endpoints & 1) != 0);
        starts.ObserveActual(context, FightState::Progress, 1);
        auto const duration = End(starts, context, 10, (endpoints & 2) != 0);
        CHECK(duration == (endpoints == 3 ? FightDuration{1699999999000, 10} : FightDuration{}));
    }
    std::vector<PresentKillPlayer> overflow(kMaxKillPlayers + 1, {1, 21});
    CHECK(!QualifyKillParticipants(overflow, {}, 21, 1));
    FightStarts starts; Start(starts, context, 0, false); // Adapter maps failed complete scan to no start.
    auto kill = Proof(); auto const duration = End(starts, context, 10);
    kill.fightStartedAtMs = duration.startedAtMs; kill.fightMs = duration.elapsedMs;
    CHECK(ValidKillProof(kill)); // Missing duration never invalidates otherwise valid kill evidence.
}

LEEROY_TEST(RealmFight_CacheCapExpiryReversalAndElapsedBoundsAreCheckedWithoutClamping)
{
    auto const context = Fight(); FightStarts bounded;
    for (std::uint32_t boss = 0; boss < FightStarts::Capacity; ++boss)
    { auto c = Fight(boss); Start(bounded, c); bounded.ObserveActual(c, FightState::Progress, 0); }
    Start(bounded, Fight(128)); CHECK_EQ(bounded.Size(), FightStarts::Capacity);
    CHECK(End(bounded, Fight(128), 1) == FightDuration{});
    CHECK(End(bounded, context, 1) == (FightDuration{1699999999000, 1}));
    for (auto elapsed : {0u, 999u, 1000u, 3600000u, 3600001u})
    {
        FightStarts starts; Start(starts, context); starts.ObserveActual(context, FightState::Progress, 0);
        CHECK(End(starts, context, elapsed) == (elapsed <= kMaxFightMs
            ? FightDuration{1699999999000, elapsed} : FightDuration{}));
    }
    FightStarts reversal; Start(reversal, context, 100); reversal.ObserveActual(context, FightState::Progress, 200);
    reversal.ObserveActual(context, FightState::Progress, 150); // Even when still later than the start.
    CHECK(End(reversal, context, 201) == FightDuration{});
    FightStarts unixReversal; Start(unixReversal, context);
    CHECK(End(unixReversal, context, 1, true, 1699999998999) == FightDuration{});
    FightStarts invalidClock;
    invalidClock.Intent(context, FightState::Resting, FightState::Progress, FightState::Resting,
        0, std::numeric_limits<std::uint64_t>::max(), true);
    CHECK(End(invalidClock, context, 1) == FightDuration{});
}

LEEROY_TEST(RealmKillProof_DurationPairIsStrictImmutableAndHumanQualified)
{
    auto unknown = Proof(); CHECK(ValidKillProof(unknown));
    for (auto const& pair : std::vector<FightDuration>{{0, 0}, {0, 1000}, {1, -1}, {1, -2}, {1, 3600001},
        {1700000000001, 0}, {std::numeric_limits<std::uint64_t>::max(), 0}})
    {
        auto malformed = unknown; malformed.fightStartedAtMs = pair.startedAtMs; malformed.fightMs = pair.elapsedMs;
        CHECK(!ValidKillProof(malformed));
    }
    for (auto elapsed : {0, 999, 1000, 3600000})
    {
        auto measured = Proof(); measured.fightStartedAtMs = measured.killedAtMs; measured.fightMs = elapsed;
        CHECK(ValidKillProof(measured));
        KillRing ring; CHECK(ring.Add(measured)); CHECK(ring.Add(measured));
        CHECK(!ring.Add(unknown)); // Same kill_id cannot acquire or shed a duration.
        auto changed = measured; ++changed.fightMs; CHECK(!ring.Add(changed));
        changed = measured; --changed.fightStartedAtMs; CHECK(!ring.Add(changed));
        measured.humanPresent = false; CHECK(!ValidKillProof(measured));
    }
}

LEEROY_TEST(RealmKillProof_FightStartUnixWireBoundaryAppliesToPairProofAndAdmission)
{
    constexpr std::uint64_t sourceMaximum = 253402300799999ULL;
    CHECK(ValidFightDuration(1, 0, 1, true));
    CHECK(ValidFightDuration(sourceMaximum, 0, sourceMaximum, true));
    CHECK(ValidFightDuration(sourceMaximum, 0, sourceMaximum + 1, true));
    CHECK(!ValidFightDuration(sourceMaximum + 1, 0, sourceMaximum + 1, true));
    CHECK(ValidFightDuration(0, -1, sourceMaximum + 1, false));

    auto proof = Proof();
    proof.killedAtMs = sourceMaximum; proof.verifiedAtMs = sourceMaximum; proof.publishedAtMs = sourceMaximum;
    proof.fightStartedAtMs = sourceMaximum; proof.fightMs = 0;
    CHECK(ValidKillProof(proof));
    ++proof.fightStartedAtMs; ++proof.killedAtMs; ++proof.verifiedAtMs; ++proof.publishedAtMs;
    CHECK(!ValidKillProof(proof));
    proof.fightStartedAtMs = 0; proof.fightMs = -1;
    CHECK(ValidKillProof(proof)); // Existing B terminal chronology bounds remain unchanged.

    auto const context = Fight();
    for (auto unixMs : {sourceMaximum, sourceMaximum + 1})
    {
        FightStarts starts;
        starts.Intent(context, FightState::Resting, FightState::Progress, FightState::Resting,
            0, unixMs, true);
        CHECK_EQ(starts.Size(), unixMs == sourceMaximum ? 1u : 0u);
        starts.ObserveActual(context, FightState::Progress, 1);
        CHECK(End(starts, context, 1, true, sourceMaximum + 1) == (unixMs == sourceMaximum
            ? FightDuration{sourceMaximum, 1} : FightDuration{}));
    }
}

LEEROY_TEST(RealmKillProof_AnnouncementMetricsUseOnlyExactRequestedProofAndSeconds)
{
    auto a = Proof(1, 1000), b = Proof(2, 2000);
    a.fightStartedAtMs = 1699999999000; a.fightMs = 999;
    b.fightStartedAtMs = 1699999999000; b.fightMs = 1000;
    KillRing ring; CHECK(ring.Add(a)); CHECK(ring.Add(b));
    AnnounceCmd request; static_cast<KillIdentity&>(request) = a; request.killId = a.killId;
    auto proof = ring.MatchProof(request.killId, request, 3000); CHECK(proof && *proof == a);
    CHECK(ValidatedAnnouncementFightSeconds(request, *proof) == std::optional<std::uint32_t>{0});
    request.fightS = 1; CHECK(!ValidatedAnnouncementFightSeconds(request, *proof));
    CHECK(!ValidatedAnnouncementFightSeconds(request, b)); // Newer same tuple is never a substitute.
    request.killId = b.killId;
    CHECK(ValidatedAnnouncementFightSeconds(request, b) == std::optional<std::uint32_t>{1});
    request.fightS = 2; CHECK(!ValidatedAnnouncementFightSeconds(request, b));
    request.fightS = 0; request.attempts = 1; CHECK(!ValidatedAnnouncementFightSeconds(request, b));
    request.attempts = 0; auto unknown = b; unknown.fightStartedAtMs = 0; unknown.fightMs = -1;
    CHECK(ValidatedAnnouncementFightSeconds(request, unknown) == std::optional<std::uint32_t>{0});
    request.fightS = 1; CHECK(!ValidatedAnnouncementFightSeconds(request, unknown));
    auto noHuman = b; noHuman.humanPresent = false; CHECK(!ValidatedAnnouncementFightSeconds(request, noHuman));
    auto wrongMode = request; wrongMode.difficulty = -1;
    CHECK(!ValidatedAnnouncementFightSeconds(wrongMode, b));
    b.fightMs = 3600000; request.fightS = 3600;
    CHECK(ValidatedAnnouncementFightSeconds(request, b) == std::optional<std::uint32_t>{3600});
}
