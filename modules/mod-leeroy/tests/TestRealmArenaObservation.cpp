// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmArenaObservation.h"
#include "TestHarness.h"
using namespace Leeroy::Realm;
namespace
{
ObservedArenaQueue Queue(std::uint64_t insertion = 1, std::uint32_t first = 101)
{
    ObservedArenaQueue queue;
    queue.insertion = insertion; queue.epoch = 7; queue.group = insertion + 1000;
    queue.groupObject = static_cast<std::uintptr_t>(insertion + 2000); queue.queue = 10; queue.row = 20;
    queue.captain = first; queue.team = first + 5000; queue.type = 2; queue.bgType = 6; queue.bracket = 8; queue.joined = 0;
    queue.members = {{first, 30, 40}, {first + 1, 31, 41}};
    return queue;
}
ObservedArenaInstance Match(std::uint64_t generation = 2, std::uint32_t id = 11)
{
    return {generation, 7, 50, id, 559, 2, false};
}
ObservedArenaEnd Entered(ArenaObservationBook& book)
{
    CHECK(book.Add(Queue())); CHECK(book.Create(Match()));
    book.records.at(1).verified = true;
    CHECK(book.Bind(1, Match(), 0, 12345));
    ObservedArenaEnd end;
    end.match = Match(); end.teams = {5101, 7001}; end.winner = 0;
    for (auto life : Queue().members)
    {
        ObservedArenaEntry entry{life, 3000, 0}; book.Enter(Match(), entry); end.members.push_back(entry);
    }
    return end;
}
}
LEEROY_TEST(RealmArenaObservation_InsertionIsCandidateAndNeedsLaterVerifiedRow)
{
    ArenaObservationBook book; book.Fence(true, 7);
    CHECK(book.Add(Queue())); CHECK(book.Create(Match()));
    CHECK(!book.Bind(1, Match(), 0, 12345));
    CHECK(!book.records.at(1).verified); CHECK(!book.records.at(1).baselineAvailable);
    book.records.at(1).verified = true;
    CHECK(book.Bind(1, Match(), 0, 12345));
    CHECK(!book.Bind(1, Match(), 1, 12345)); CHECK(!book.Bind(1, Match(), 0, 12346));
}
LEEROY_TEST(RealmArenaObservation_SameTimestampReinsertCannotReuseOldEntryOrRating)
{
    ArenaObservationBook book; book.Fence(true, 7);
    auto end = Entered(book); CHECK(ArenaObservationBook::Matches(book.records.at(1), end));
    auto next = Queue(3); CHECK(book.Add(next));
    CHECK(!book.records.contains(1)); CHECK_EQ(book.records.size(), std::size_t{1});
    CHECK(!ArenaObservationBook::Matches(book.records.at(3), end));
    CHECK(!book.records.at(3).baselineAvailable); CHECK(!book.records.at(3).match);
}
LEEROY_TEST(RealmArenaObservation_AnyNativeInsertionRetiresOverlappingLineage)
{
    ArenaObservationBook book; book.Fence(true, 7); CHECK(book.Add(Queue()));
    book.Reinsert({102, 999}); CHECK(book.records.empty());
    CHECK(book.Add(Queue())); book.Reinsert({999}); CHECK_EQ(book.records.size(), std::size_t{1});
}
LEEROY_TEST(RealmArenaObservation_ExactInstanceGenerationObjectTypeAndExpiry)
{
    ArenaObservationBook book; book.Fence(true, 7); CHECK(book.Add(Queue())); CHECK(book.Create(Match()));
    book.records.at(1).verified = true;
    auto wrong = Match(); wrong.object++;
    CHECK(!book.Bind(1, wrong, 0, 12345)); wrong = Match(); wrong.generation++;
    CHECK(!book.Bind(1, wrong, 0, 12345)); wrong = Match(); wrong.type = 3;
    CHECK(!book.Bind(1, wrong, 0, 12345)); CHECK(!book.Bind(1, Match(), 0, 0));
    CHECK(book.Bind(1, Match(), 0, 12345));
    book.Destroy(Match()); CHECK(book.records.empty());
    CHECK(book.Create(Match(3))); CHECK(!book.Bind(1, Match(3), 0, 12345));
}
LEEROY_TEST(RealmArenaObservation_EndRequiresEveryExactEntryLifeSideAndGroup)
{
    ArenaObservationBook book; book.Fence(true, 7); auto end = Entered(book);
    CHECK(ArenaObservationBook::Matches(book.records.at(1), end));
    auto wrong = end; wrong.members.pop_back(); CHECK(!ArenaObservationBook::Matches(book.records.at(1), wrong));
    wrong = end; wrong.members[0].life.session++; CHECK(!ArenaObservationBook::Matches(book.records.at(1), wrong));
    wrong = end; wrong.members[0].group++; CHECK(!ArenaObservationBook::Matches(book.records.at(1), wrong));
    wrong = end; wrong.members[0].side = 1; CHECK(!ArenaObservationBook::Matches(book.records.at(1), wrong));
    wrong = end; wrong.teams[0]++; CHECK(!ArenaObservationBook::Matches(book.records.at(1), wrong));
}
LEEROY_TEST(RealmArenaObservation_LifecycleRetiresEvenReusedPlayerAndSessionAddresses)
{
    ArenaObservationBook book; book.Fence(true, 7); auto end = Entered(book);
    book.Lifecycle(102); CHECK(book.records.empty());
    CHECK(book.Add(Queue(3))); CHECK(!ArenaObservationBook::Matches(book.records.at(3), end));
    book.GroupChanged(1003); CHECK(book.records.empty());
}
LEEROY_TEST(RealmArenaObservation_DisableAndEpochFenceAllObservationState)
{
    ArenaObservationBook book; book.Fence(true, 7); auto end = Entered(book);
    book.Fence(false, 7); CHECK_EQ(book.Size(), std::size_t{0}); CHECK(!book.End(end));
    CHECK(!book.Add(Queue())); CHECK(!book.Create(Match()));
    book.Fence(true, 8); CHECK(!book.Add(Queue())); CHECK(!book.Create(Match()));
    auto queue = Queue(); queue.epoch = 8; CHECK(book.Add(queue));
    book.Fence(true, 9); CHECK_EQ(book.Size(), std::size_t{0});
}
LEEROY_TEST(RealmArenaObservation_EndDedupeIsBoundToInstanceLifetime)
{
    ArenaObservationBook book; book.Fence(true, 7); auto end = Entered(book);
    CHECK(book.End(end)); CHECK(!book.End(end));
    auto repeated = end; repeated.match.ended = true; CHECK(!book.End(repeated));
    book.Destroy(Match());
    CHECK(ArenaObservationBook::Matches(book.records.at(1), end)); // copied fact survives native destruction
    CHECK(book.Create(Match(3))); CHECK(!book.End(end));
    repeated = end; repeated.match = Match(3); CHECK(book.End(repeated));
}
LEEROY_TEST(RealmArenaObservation_OneSharedFiveHundredRecordBound)
{
    ArenaObservationBook book; book.Fence(true, 7); CHECK(book.Create(Match()));
    for (std::uint32_t index = 0; index < 499; ++index) CHECK(book.Add(Queue(index + 10, index * 2 + 1000)));
    CHECK_EQ(book.Size(), ArenaObservationBook::Capacity);
    CHECK(!book.Add(Queue(1000, 9000))); CHECK(!book.Create(Match(1001, 12)));
    CHECK_EQ(book.Size(), ArenaObservationBook::Capacity);
    book.Lifecycle(1000); CHECK(book.Add(Queue(1000, 9000)));
    CHECK_EQ(book.Size(), ArenaObservationBook::Capacity);
}
LEEROY_TEST(RealmArenaObservation_DisjointSameTeamInvalidatesPreviousBaseline)
{
    ArenaObservationBook book; book.Fence(true, 7); CHECK(book.Add(Queue()));
    book.records.at(1).baselineAvailable = true;
    auto disjoint = Queue(3, 201); disjoint.team = Queue().team;
    CHECK(book.Add(disjoint)); CHECK(!book.records.at(1).baselineAvailable);
    CHECK_EQ(book.records.size(), std::size_t{2});
}
LEEROY_TEST(RealmArenaObservation_EntryGroupDiscontinuityCannotBeReverified)
{
    ArenaObservationBook book; book.Fence(true, 7); auto end = Entered(book);
    auto replacement = end.members[0]; replacement.group++;
    book.Enter(Match(), replacement);
    CHECK(book.records.at(1).poisoned);
    CHECK(!ArenaObservationBook::Matches(book.records.at(1), end));
    book.records.at(1).verified = true;
    CHECK(!book.Bind(1, Match(), 0, 12345));
    CHECK(!ArenaObservationBook::Matches(book.records.at(1), end));
}
LEEROY_TEST(RealmArenaObservation_ImmutableOwnerSourceSuppressesDelayedEnd)
{
    ArenaObservationBook book; book.Fence(true, 7); auto end = Entered(book);
    CHECK(ArenaObservationBook::Matches(book.records.at(1), end));
    end.ownerSourced = true;
    // No current holder lookup can clear a copied result's owner provenance.
    CHECK(!ArenaObservationBook::Matches(book.records.at(1), end));
    CHECK(book.End(end)); CHECK(!book.End(end));
}
LEEROY_TEST(RealmArenaObservation_OwnerEntryProvenanceSurvivesUntilExactGenerationEnd)
{
    ArenaObservationBook book; book.Fence(true, 7); auto end = Entered(book);
    book.MarkOwnerSource(Match());
    // The original native watch can be retired before end. Provenance belongs
    // to the exact instance lifetime rather than any current holder lookup.
    end.match = book.instances.at(11);
    CHECK(end.match.ownerSourced); CHECK(!ArenaObservationBook::Matches(book.records.at(1), end));
    book.Destroy(Match()); CHECK(book.Create(Match(3)));
    CHECK(!book.instances.at(11).ownerSourced);
}
LEEROY_TEST(RealmArenaObservation_ExactPortCandidateRetainsAwaitingButNeverProvesEntry)
{
    ArenaObservationBook book; book.Fence(true, 7); CHECK(book.Add(Queue())); CHECK(book.Create(Match()));
    auto& record = book.records.at(1); record.verified = true;
    CHECK(book.Bind(1, Match(), 0, 12345)); record.destination = {559, 1, 2, 3, 4};
    auto life = Queue().members[0];
    book.Travel(life, 11, 0, record.destination);
    CHECK(ArenaObservationBook::AwaitingEntry(record, life)); CHECK(record.entries.empty());
    auto replacement = life; replacement.session++;
    CHECK(!ArenaObservationBook::AwaitingEntry(record, replacement));
    book.Enter(Match(), {life, 3000, 0});
    CHECK(!ArenaObservationBook::AwaitingEntry(record, life)); CHECK_EQ(record.entries.size(), std::size_t{1});
    auto wrong = record.destination; wrong.map++;
    book.Travel(Queue().members[1], 11, 0, wrong);
    CHECK(record.poisoned); CHECK(!ArenaObservationBook::AwaitingEntry(record, Queue().members[1]));
}
LEEROY_TEST(RealmArenaObservation_CopiedEndSurvivesNormalDepartureButNotLifecycle)
{
    ArenaObservationBook book; book.Fence(true, 7); auto end = Entered(book);
    CHECK(book.End(end)); book.Leave(101); book.GroupChanged(1001); book.Destroy(Match());
    book.Travel(Queue().members[0], 0, 0, {1, 10, 20, 30, 4});
    CHECK(book.records.contains(1)); CHECK(ArenaObservationBook::Matches(book.records.at(1), end));
    book.Lifecycle(101); CHECK(book.records.empty());
}
