// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_TRAINING_COMMANDS_H
#define MOD_LEEROY_TRAINING_COMMANDS_H
#include "LeeroyCommands.h"
#include "LeeroyBotJournal.h"

class Player;

namespace Leeroy::Training
{
LeeroyCommandResult HandleTelemetry(nlohmann::json const&);
LeeroyCommandResult HandleGetStats(nlohmann::json const&);
LeeroyCommandResult HandleGetVocab(nlohmann::json const&);
LeeroyCommandResult HandleCustomStrategy(nlohmann::json const&);
LeeroyCommandResult HandleDcSetting(nlohmann::json const&);
LeeroyCommandResult HandleGetRivals(nlohmann::json const&);
LeeroyCommandResult HandleStartBenchmark(nlohmann::json const&);
// Implemented by Task5; covers pending, active and unresolved cleanup bodies.
bool DummyTrialOwns(std::uint32_t guid);
// World-thread, read-only exclusion. Includes original and current original-group
// members while DC override/hold cleanup or an exact job receipt is retained.
// Unknown legacy membership is conservatively busy; the DC0 variant returns false.
bool TrainingDcOwns(std::uint32_t guid);
void TickVocab();
void TickDcHolds(std::uint32_t diff);
bool IsPrimed(std::string const& qualifier);
bool RepairPrimedForRestore(std::string const& qualifier);
bool TrainingAllowed();
// Why TrainingAllowed() is false ("guild_off", "training_off", "cleanup_pending"); empty when allowed.
std::string_view TrainingBlockReason();
// The global fence over every body: true while a cleanup is unproved, for at most
// kTrainingFenceReleaseMs. After that only per-body fences (owners, journals) hold.
bool TrainingCleanupPending();
// True until a cleanup pass proves every adapter clean; new training stays refused meanwhile.
bool TrainingCleanupUnresolved();
void RequireTrainingCleanup();
std::size_t TrainingJournalCount();
BotJournal& JournalOf(std::uint32_t guid);
void JournalReset(std::uint32_t guid, std::vector<std::string_view> const& values = {});
void JournalForget(std::uint32_t guid);
bool HasTrainingJournal(std::uint32_t guid);
void JournalValueReplaced(std::uint32_t guid, std::string_view name);
// False keeps failed restoration entries for retry. The optional count includes only verified changes.
bool RestoreJournaled(Player*, std::string_view name, std::uint32_t* changes = nullptr);
std::string_view ApplyTrainingStrategies(Player*, std::string const& param, Engine, bool journalAll = false);
std::string_view ApplyTrainingValue(Player*, std::string const& param);
bool RestoreTrainingBots();
// Job teardown MUST pass its returned token. A stale job cannot erase a newer run for the same leader.
std::uint64_t MarkJobRun(std::uint32_t leader);
bool ClearTrainingRun(std::uint32_t leader, std::uint64_t token);
bool ClearTrainingRuns();
void ResetTrainingVocabulary();
void ForgetVocabularyProbe(std::uint32_t guid);
void TrainingOffCleanup();
}
#endif
