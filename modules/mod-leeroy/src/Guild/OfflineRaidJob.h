// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_OFFLINE_RAID_JOB_H
#define MOD_LEEROY_OFFLINE_RAID_JOB_H

#include "LeeroyCommands.h"
#include "LeeroyGuildCommands.h"
#include <cstdint>

namespace Leeroy::Realm { class DayOwnerCheck; }
namespace Leeroy::Guild
{
enum class RaidJobMode { Offline, Benchmark, MemberDungeon };
std::optional<LeeroyCommandResult> MemberDungeonStartReplay(std::string const& id, OfflineRaidCmd const&);
LeeroyCommandResult StartRaidJob(std::string const& id, OfflineRaidCmd const&, RaidJobMode,
    std::vector<std::uint32_t> const& bosses = {}, Realm::DayOwnerCheck const* check = nullptr);
LeeroyCommandResult HandleStartOfflineRaid(nlohmann::json const& message);
LeeroyCommandResult HandleStopOfflineRaid(nlohmann::json const& message);
// Reservations include requested members before grouping and survive unresolved teardown.
bool OfflineRaidOwns(std::uint32_t guid);
// The members a running offline raid (never a benchmark) holds in its group inside its instance, and its leader
// (whose training DC job covers them): the mechanic responder's offline bodies. Empty in every other stage.
struct OfflineFighters
{
    std::uint32_t leader = 0;
    std::vector<std::uint32_t> guids;
};
OfflineFighters OfflineRaidFighters();
std::string AdmitTrainingRun(std::string const& handle, std::vector<std::uint32_t> const& guids,
    std::uint32_t leader = 0);
LeeroyCommandResult HandleGetTrainingCleanup(nlohmann::json const&);
bool RestoreOfflineTraining();
bool OfflineTrainingCleanupPending();
bool OfflineRaidCanClearTraining(std::uint32_t leader, std::uint64_t token);
bool BenchmarkRunning();
bool BenchmarkOwnsRun(std::uint32_t leader);
bool BenchmarkCanClearRun(std::uint32_t leader, std::uint64_t token);
void BenchmarkPlayerLogout(std::uint32_t guid);
void BenchmarkPlayerLogin(std::uint32_t guid);
void EndBenchmark(std::string const& reason);
void TickOfflineRaid(std::uint32_t diffMs);
void SweepLeftoverGroups();
void RegisterOfflineRaidHandler();
#if defined(DOJO_OFFLINE_ACCEPTANCE) && DOJO_OFFLINE_ACCEPTANCE
// Observes the real job; only compiled into the isolated acceptance variant.
nlohmann::json DojoOfflineSnapshot();
bool DojoStopOffline(std::string const& id);
// Naxx route fixture only: configure the negative control before any job tick,
// then exclude explicitly prepared prerequisites from earned kill accounting.
bool DojoConfigureNaxxPass(std::string const& id, bool suppress);
bool DojoPrimeNaxxStates(std::string const& id);
bool DojoPrimeTocStates(std::string const& id);
#endif
}

#endif
