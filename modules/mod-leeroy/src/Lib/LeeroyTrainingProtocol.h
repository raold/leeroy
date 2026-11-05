// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_TRAINING_PROTOCOL_H
#define MOD_LEEROY_TRAINING_PROTOCOL_H

#include <array>
#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>
#include <nlohmann/json.hpp>
#include "LeeroyTrainingCleanup.h"

namespace Leeroy::Training
{
inline constexpr std::array<std::string_view, 9> kTrainingCommandTypes = {
    "telemetry", "get_stats", "get_vocab", "custom_strategy", "dc_setting", "dummy_trial",
    "get_rivals", "start_benchmark", "get_training_cleanup"
};
inline constexpr std::array<std::string_view, 18> kDcKeys = {
    "PartyMaxSpread", "PullCombatSpread", "CombatRegroupDistance", "BossEngageRangeFloor",
    "BossEngageRangeCap", "HealRepositionMaxRange", "HealRepositionHpFloor", "PullPlayerReleaseDelay",
    "PullPetReleaseDelay", "PullThreatLeadPanicHp", "PullSafetyHpPct", "SmartRestHealthPct",
    "SmartRestDpsManaPct", "SmartRestHealerManaPct", "RaidMusterRestTimeoutSecs",
    "RaidMusterRebuffTimeoutSecs", "RaidMusterTotalTimeoutSecs", "WaitAtBoss"
};
inline constexpr std::array<std::string_view, 5> kTierTwoStrategies = {
    "threat", "dps aoe", "focus heal targets", "wait for attack", "save mana"
};
inline constexpr std::array<std::string_view, 4> kTrainingValues = {
    "mana save level", "wait for attack time", "focus heal targets", "formation"
};

struct TelemetryCmd { bool record = false; std::vector<std::uint32_t> watch; };
struct GetStatsCmd { };
struct GetTrainingCleanupCmd { std::string trainingRun; };
struct GetVocabCmd
{
    std::vector<std::uint32_t> classes, probes;
    std::vector<std::string> triggers;
};
struct CustomStrategyCmd
{
    std::string name;
    std::uint32_t cls = 0, probe = 0;
    std::vector<std::string> lines;
};
enum class DcOp { Set, Scale, Reset, Clear };
struct DcSettingCmd
{
    std::uint32_t leaderGuid = 0;
    DcOp op = DcOp::Set;
    std::string key;
    std::int32_t centi = 0;
    std::string trainingRun;
};
struct DummyTrialCmd
{
    std::vector<std::uint32_t> guids;
    std::vector<std::string> co;
    std::uint32_t place = 0, seconds = 0;
};
struct GetRivalsCmd { std::uint32_t level = 80, max = 200; };
struct RivalState
{
    bool online = false, isBot = false, isRandomBot = false, isCast = false, isHuman = false,
        grouped = false, inInstance = false, inBattleground = false, inLfg = false, dead = false,
        inFlight = false;
    std::uint8_t level = 0, team = 0;
};
bool RivalUsable(RivalState const&, std::uint32_t requiredLevel);
bool FrontierReached(std::vector<std::uint8_t> const& bossStates, std::vector<std::uint32_t> const& allowed);
struct StartBenchmarkCmd
{
    std::uint32_t mapId = 0, size = 0;
    bool heroic = false;
    std::uint32_t leaderGuid = 0;
    std::vector<std::uint32_t> members, bosses;
    std::uint32_t maxWipes = 6, maxWipesPerBoss = 3, timeoutMin = 180, minMembers = 0;
};

// Envelope validation stays in DecodeCommand. These parsers validate command fields only.
std::optional<TelemetryCmd> ParseTelemetry(nlohmann::json const&, std::string& error);
std::optional<GetStatsCmd> ParseGetStats(nlohmann::json const&, std::string& error);
std::optional<GetTrainingCleanupCmd> ParseGetTrainingCleanup(nlohmann::json const&, std::string& error);
bool TrainingRunFieldValid(std::string_view, bool optional = true);
nlohmann::json MakeTrainingCleanup(std::string const& id, std::string const& handle, CleanupReceipt const*);
std::optional<GetVocabCmd> ParseGetVocab(nlohmann::json const&, std::string& error);
std::optional<CustomStrategyCmd> ParseCustomStrategy(nlohmann::json const&, std::string& error);
std::optional<DcSettingCmd> ParseDcSetting(nlohmann::json const&, std::string& error);
std::optional<DummyTrialCmd> ParseDummyTrial(nlohmann::json const&, std::string& error);
std::optional<GetRivalsCmd> ParseGetRivals(nlohmann::json const&, std::string& error);
std::optional<StartBenchmarkCmd> ParseStartBenchmark(nlohmann::json const&, std::string& error);
void RegisterTrainingCommandTypes();
bool DcKeyAllowed(std::string_view);
bool CustomLineValid(std::string_view);
std::string FormatCacheEntry(std::string_view name, std::vector<std::string> const& lines);
bool IsTierTwo(std::string_view action, std::string_view param);
bool TierTwoAllowed(bool trainingOn, std::string_view action, std::string_view param);

inline constexpr std::array<std::pair<std::uint32_t, std::uint32_t>, 5> kNonFightSteps = {
    {{631, 13}, {631, 14}, {724, 3}, {724, 4}, {724, 5}}
};
inline constexpr std::array<std::uint32_t, 6> kAidSpells = {36886, 69910, 70115, 70227, 71188, 71638};
struct ArmInputs
{
    bool isRaid = false, telemetryOn = false, mindArmed = false, trackedOnMap = false,
        registered = false, bossAllowed = false;
    std::uint32_t map = 0, bossId = 0;
};
bool ShouldArm(ArmInputs const&);
bool ShouldAbort(std::uint64_t startMs, std::uint64_t nowMs);
enum class BossRecordDecision { Retain, Confirm, Discard, Killed, Wiped };
// BeforeSetBossState is an intent hook: DONE can still be rejected. Only the committed post-map state closes a pull.
BossRecordDecision ReviewBossState(bool awaitingStart, bool terminalIntent, std::uint8_t actualState);
bool RecordingStillCurrent(bool enabled, std::uint64_t openingEpoch, std::uint64_t currentEpoch);

struct HoldState
{
    bool paused = false, atBoss = false;
    std::uint64_t pausedSinceMs = 0;
    bool resumed = false;
};
bool HoldShouldResume(HoldState const&, std::uint64_t nowMs);
enum class PrimeDecision { Write, Same, Taken };
PrimeDecision PrimeEntry(std::string_view existing, std::string_view wanted);
PrimeDecision PrimeOwnedEntry(std::string_view existing, std::string_view wanted, std::string_view recorded);
// Restore-only repair from a previously validated immutable receipt; never creates a new definition.
bool RestorePrimedEntry(std::map<std::string, std::string>& cache,
    std::map<std::string, std::string, std::less<>> const& recorded, std::string const& name);
struct RunWatch
{
    bool leaderOnline = false, sameMap = false, jobOwned = false, jobEnded = false, dcEnabled = false;
    std::uint64_t dcOffSinceMs = 0;
};
bool ShouldClearRun(RunWatch const&, std::uint64_t nowMs);
enum class VocabKind { Class, Probe, Server };
struct VocabJob { VocabKind kind; std::uint8_t cls = 0; std::uint32_t probe = 0; bool wantSpells = false; };
std::vector<VocabJob> PlanVocab(GetVocabCmd const&, bool humanOnline);

struct DummyPlace { std::uint32_t map; float x, y, z; };
inline constexpr std::array<DummyPlace, 2> kDummyPlaces = {
    {{1, 2015.18f, -4842.30f, 51.74f}, {1, 2157.80f, -4629.35f, 53.08f}}
};
inline constexpr std::uint32_t kDummyEntry = 31146;
struct DummyBotState
{
    bool isCast = false, online = false, alive = false, inCombat = false, grouped = false,
        onInstanceMap = false, inBattleground = false, inOfflineRaid = false, nonPermanentBound = false;
};
bool DummyBotUsable(DummyBotState const&);
}
#endif
