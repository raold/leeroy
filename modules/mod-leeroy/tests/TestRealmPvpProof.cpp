// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmPvpProof.h"
#include "TestHarness.h"

using namespace Leeroy::Realm;
namespace
{
ArenaQueueIdentity Queue() { return {7, 81, 3, 9, 21, 2, 8, 100}; }
ArenaMatchIdentity Match() { return {11, 51, 559, 300}; }
ArenaLineage Bound() { ArenaLineage proof; CHECK(proof.Bind(Queue(), {101, 102})); return proof; }
}
LEEROY_TEST(RealmPvp_SameTimestampReinsertionCannotContinueAnOldQueue)
{
    auto proof = Bound(); auto replacement = Queue(); ++replacement.insertion;
    CHECK(!proof.ObserveQueue(replacement, {101, 102}));
    CHECK(!proof.ObserveQueue(Queue(), {101, 102}));
}
LEEROY_TEST(RealmPvp_QueueProjectionRequiresEachMembersOwnEntryIntent)
{
    auto proof = Bound(); CHECK(proof.Invite(Match()));
    CHECK(proof.BeginEnter(101, Match()));
    CHECK(proof.ObserveQueue(Queue(), {102}));
    CHECK(proof.Entered(101, Match(), true));
    CHECK(proof.Members() == std::vector<std::uint32_t>({101, 102}));
    CHECK(!proof.ObserveQueue(Queue(), {}));
}
LEEROY_TEST(RealmPvp_NoExtraMemberOrGenericActiveStateAdoption)
{
    auto proof = Bound(); CHECK(!proof.ObserveQueue(Queue(), {101, 102, 103}));
    proof = Bound(); CHECK(proof.Invite(Match())); CHECK(!proof.Entered(101, Match(), true));
}
LEEROY_TEST(RealmPvp_ReusedInstanceIdCannotSatisfyEntryReceipt)
{
    auto proof = Bound(); CHECK(proof.Invite(Match())); CHECK(proof.BeginEnter(101, Match()));
    auto other = Match(); ++other.generation;
    CHECK(!proof.Entered(101, other, true));
}
LEEROY_TEST(RealmPvp_PreHooksAndEmptySourcesDoNotProveClean)
{
    auto proof = Bound(); CHECK(proof.Invite(Match())); CHECK(proof.BeginEnter(101, Match()));
    CHECK(!proof.CanClean(true, true, 2, 2));
    CHECK(!proof.Entered(101, Match(), false));
    proof = Bound(); CHECK(!proof.Removed(101, false, true));
}
LEEROY_TEST(RealmPvp_CleanupRequiresFullManifestGroupAndStableRevision)
{
    auto proof = Bound(); CHECK(proof.Removed(101, true, true));
    CHECK(!proof.CanClean(true, true, 2, 2)); CHECK(proof.Removed(102, true, true));
    CHECK(!proof.CanClean(false, true, 2, 2)); CHECK(!proof.CanClean(true, false, 2, 2));
    CHECK(!proof.CanClean(true, true, 2, 3)); CHECK(proof.CanClean(true, true, 3, 3));
}
LEEROY_TEST(RealmPvp_AdmissionManifestIsExactAndBounded)
{
    ArenaLineage proof; CHECK(!proof.Bind(Queue(), {102, 101}));
    proof = {}; CHECK(!proof.Bind(Queue(), {101, 101}));
    proof = {}; CHECK(!proof.Bind(Queue(), {101}));
    proof = {}; auto identity = Queue(); identity.insertion = 0;
    CHECK(!proof.Bind(identity, {101, 102}));
}
LEEROY_TEST(RealmPvp_WgQueueOnlyCleanupRequiresNativeRemoval)
{
    WintergraspCleanupEvidence evidence{true, true, true, false, false, true, true, true, false, 2, 2};
    CHECK(ProvesWintergraspClean(evidence)); evidence.queueRemovalReceipt = false;
    CHECK(!ProvesWintergraspClean(evidence));
}
LEEROY_TEST(RealmPvp_WgOutsideInvitationCannotUseEmptyPublicSetsAsCleanup)
{
    WintergraspCleanupEvidence evidence{true, true, true, true, false, true, true, true, false, 2, 2};
    CHECK(!ProvesWintergraspClean(evidence)); evidence.postZoneDepartureReceipt = true;
    CHECK(ProvesWintergraspClean(evidence)); evidence.outsideZone = false;
    CHECK(!ProvesWintergraspClean(evidence));
}
LEEROY_TEST(RealmPvp_MixedArenaEntryCannotRegressAggregateStage)
{
    auto proof = Bound(); CHECK(proof.Progress(false) == ArenaProgressStage::Queued);
    CHECK(proof.Invite(Match())); CHECK(proof.Progress(true) == ArenaProgressStage::Invited);
    CHECK(proof.BeginEnter(101, Match())); CHECK(proof.Progress(true) == ArenaProgressStage::Entering);
    CHECK(proof.Entered(101, Match(), true));
    CHECK(proof.Progress(false) == ArenaProgressStage::Entering);
    CHECK(proof.Progress(true) == ArenaProgressStage::Entering);
    CHECK(proof.BeginEnter(102, Match())); CHECK(proof.Entered(102, Match(), true));
    CHECK(proof.Progress(true) == ArenaProgressStage::Active);
    CHECK(proof.Removed(101, true, true)); CHECK(proof.Progress(true) == ArenaProgressStage::Ending);
}
LEEROY_TEST(RealmPvp_WgNativeWarDestinationsUseEffectiveTeam)
{
    auto defender = WintergraspWarDestination(0, 0);
    CHECK(defender == WintergraspWarDestination(1, 1));
    CHECK(defender.map == 571 && defender.x == 5345 && defender.y == 2842);
    auto horde = WintergraspWarDestination(1, 0), alliance = WintergraspWarDestination(0, 1);
    CHECK(horde.map == 571 && horde.x == 5025.857422f);
    CHECK(alliance.map == 571 && alliance.x == 5101.284f);
    CHECK(!(horde == alliance));
}
LEEROY_TEST(RealmPvp_WgNativeEnrollmentDoesNotProvePlayableArrival)
{
    WintergraspEntryEvidence evidence{true, true, true, true, true, false, true, true, false};
    CHECK(!ProvesWintergraspEntry(evidence)); evidence.teleportFinished = true;
    CHECK(!ProvesWintergraspEntry(evidence)); evidence.arrivalReceipt = true;
    CHECK(ProvesWintergraspEntry(evidence)); evidence.scopedTeleport = false;
    CHECK(!ProvesWintergraspEntry(evidence)); evidence.scopedTeleport = true; evidence.expectedDestination = false;
    CHECK(!ProvesWintergraspEntry(evidence));
}
LEEROY_TEST(RealmPvp_WgLeavePrehookAndEmptySetsCannotProvePostCleanup)
{
    WintergraspCleanupEvidence evidence{true, true, true, true, false, true, true, true, false, 5, 5};
    CHECK(!ProvesWintergraspClean(evidence)); evidence.postZoneDepartureReceipt = true;
    CHECK(ProvesWintergraspClean(evidence)); evidence.teleportFinished = false;
    CHECK(!ProvesWintergraspClean(evidence));
}
LEEROY_TEST(RealmPvp_ForeignWgQueueRetainsPrivateKickBaselineWithoutInviteHook)
{
    WintergraspHistory history; CHECK(history.Allows(101, 7001));
    CHECK(history.TaintSession(7001));
    // StartBattle may clear the public queue and create a private kick before
    // the invitation hook. Neither absence nor later war end clears history.
    CHECK(!history.Allows(101, 7001)); CHECK(history.Allows(102, 7002));
    CHECK(history.TaintBody(101)); CHECK(!history.Allows(101, 7003));
}
LEEROY_TEST(RealmPvp_ForeignWgLogoutTransfersSessionHistoryToGuid)
{
    WintergraspHistory history; CHECK(history.TaintSession(7001));
    CHECK(history.Logout(101, 7001)); CHECK(!history.HasSession(7001));
    CHECK(!history.Allows(101, 7002)); CHECK(history.Allows(102, 7001));
}
LEEROY_TEST(RealmPvp_WgHistoryOverflowFencesAdmissionAndPreservesFailedConversion)
{
    WintergraspHistory history;
    for (std::uint32_t guid = 1; guid <= WintergraspHistory::Capacity; ++guid) CHECK(history.TaintBody(guid));
    CHECK(history.Allows(9001, 7001)); CHECK(history.TaintSession(7001));
    CHECK(!history.Logout(9001, 7001)); CHECK(history.HasSession(7001));
    CHECK(!history.Allows(9002, 7002));
    WintergraspHistory sessions;
    for (std::uintptr_t session = 1; session <= WintergraspHistory::Capacity; ++session) CHECK(sessions.TaintSession(session));
    CHECK(!sessions.TaintSession(9001)); CHECK(!sessions.Allows(101, 9002));
}
LEEROY_TEST(RealmPvp_WgLogoutGraceNeverExpiresIntoProof)
{
    WintergraspCleanupEvidence evidence{true, true, true, true, true, true, true, true, true, 2, 2};
    CHECK(!ProvesWintergraspClean(evidence)); evidence.discontinuity = false; evidence.sameLife = false;
    CHECK(!ProvesWintergraspClean(evidence)); evidence.sameLife = true; ++evidence.currentRevision;
    CHECK(!ProvesWintergraspClean(evidence));
}
