// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

#include "LeeroyCastGuard.h"
#include "TestHarness.h"

#include <limits>
#include <stdexcept>
#include <utility>

using namespace Leeroy::Realm;

namespace
{
constexpr std::uint32_t GuildId = 41;
constexpr std::uint32_t Human = 9;

GuardInput Roster(std::uint64_t now = 1000)
{
    return {{{101, 2}, {102, 3}}, {}, now};
}

GuardInput Missing(std::uint64_t now = 1000)
{
    return {{{101, 2}}, {{102, {true, 0, false}}}, now};
}

void Step(std::vector<GuardStep> const& steps, std::uint32_t guid, GuardAction action, std::uint8_t rank)
{
    CHECK_EQ(steps.size(), std::size_t{1});
    if (steps.size() == 1)
    {
        CHECK_EQ(steps[0].guid, guid);
        CHECK(steps[0].action == action);
        CHECK_EQ(steps[0].rank, rank);
    }
}
}

LEEROY_TEST(CastGuard_RequiresConfiguredIdentities)
{
    for (auto ids : {std::pair{0u, Human}, std::pair{GuildId, 0u}, std::pair{0u, 0u}})
    {
        bool rejected = false;
        try { GuardState guard(ids.first, ids.second); }
        catch (std::invalid_argument const&) { rejected = true; }
        CHECK(rejected);
    }
}

LEEROY_TEST(CastGuard_ReaddsAtCurrentKnownRank)
{
    GuardState guard(GuildId, Human);
    CHECK(guard.Pass(Roster()).empty());
    auto roster = Roster();
    roster.members[102] = 4;
    CHECK(guard.Pass(roster).empty());
    Step(guard.Pass(Missing()), 102, GuardAction::Readd, 4);
    CHECK_EQ(guard.Known().at(102), std::uint8_t{4});
}

LEEROY_TEST(CastGuard_LearnsEveryMemberButHumanAndZero)
{
    GuardState guard(GuildId, Human);
    auto input = Roster();
    input.members[Human] = 0;
    input.members[0] = 0;
    input.members[106] = 6; // No level, persona or online filter belongs in the guard.
    guard.Pass(input);
    CHECK_EQ(guard.Known().size(), std::size_t{3});
    CHECK(guard.Known().contains(106));
    CHECK(!guard.Known().contains(Human));
    CHECK(!guard.Known().contains(0));
    guard.NoteKick(Human, 0);
    guard.NoteKick(0, 0);
    CHECK(guard.Pass(input).empty());
}

LEEROY_TEST(CastGuard_ClassifiesActualActorAndConfiguredGuild)
{
    CHECK(UninviteEventIsHis(true, GuildId, Human, GuildId, Human));
    CHECK(!UninviteEventIsHis(true, GuildId, 101, GuildId, Human));
    CHECK(!UninviteEventIsHis(false, GuildId, Human, GuildId, Human));
    CHECK(!UninviteEventIsHis(true, 42, Human, GuildId, Human));
    CHECK(!UninviteEventIsHis(true, 0, Human, 0, Human));
    CHECK(!UninviteEventIsHis(true, GuildId, 0, GuildId, 0));
    CHECK(CommandIsHis(true, 0, Human));
    CHECK(CommandIsHis(false, Human, Human));
    CHECK(!CommandIsHis(false, 101, Human));
    CHECK(!CommandIsHis(false, 0, 0));
}

LEEROY_TEST(CastGuard_ConfirmedKickSurvivesLongDisabledGapAndStaleRoster)
{
    GuardState guard(GuildId, Human);
    guard.Pass(Roster());
    guard.NoteKick(102, 1000);
    Step(guard.Pass(Roster(3600000)), 102, GuardAction::HisKick, 3);
    CHECK(!guard.Known().contains(102));
    CHECK(guard.Pass(Roster(7200000)).empty()); // Generic refresh cannot cancel a confirmed kick.
    CHECK(!guard.Known().contains(102));
    CHECK(guard.Pass(Missing(10800000)).empty());
    guard.NoteKick(102, 10800000);
    CHECK(guard.Pass(Missing(10800000)).empty()); // Repeated event is consumed once.
}

LEEROY_TEST(CastGuard_ExplicitOwnerAdmissionMustBeObservedBeforeProtectionResumes)
{
    GuardState guard(GuildId, Human);
    guard.Pass(Roster());
    guard.NoteKick(102, 1000);
    guard.Pass(Missing());
    CHECK(guard.AllowRejoin(102));
    CHECK(!guard.AllowRejoin(102));
    CHECK(guard.Pass(Missing(2000)).empty()); // Admission permission does not prove successful join.
    auto input = Roster(3000);
    input.members[102] = 6;
    guard.Pass(input);
    Step(guard.Pass(Missing(4000)), 102, GuardAction::Readd, 6);
    guard.NoteKick(102, 5000);
    Step(guard.Pass(Missing(5000)), 102, GuardAction::HisKick, 6);
    CHECK(guard.Pass(Roster(6000)).empty());
    CHECK(!guard.Known().contains(102));
}

LEEROY_TEST(CastGuard_RejoinBeforePendingKickPassDoesNotReuseOldRank)
{
    GuardState guard(GuildId, Human);
    guard.Pass(Roster());
    guard.NoteKick(102, 1000);
    CHECK(guard.AllowRejoin(102));
    CHECK(!guard.Known().contains(102));
    CHECK(guard.Pass(Missing()).empty());
    auto input = Roster();
    input.members[102] = 5;
    guard.Pass(input);
    Step(guard.Pass(Missing()), 102, GuardAction::Readd, 5);
}

LEEROY_TEST(CastGuard_ConfirmedRemovalBeforeFirstRosterCannotBeRelearned)
{
    GuardState guard(GuildId, Human);
    guard.NoteKick(102, 0);
    Step(guard.Pass(Roster()), 102, GuardAction::HisKick, 0); // Unknown old rank is diagnostic only.
    CHECK(!guard.Known().contains(102));
    CHECK(guard.Pass(Missing()).empty());
}

LEEROY_TEST(CastGuard_CommandIntentNeverRemovesKnown)
{
    GuardState guard(GuildId, Human);
    guard.Pass(Roster());
    guard.NoteCommandKick(102, 1000);
    CHECK(guard.PendingCommandKick(102, 1000));
    Step(guard.Pass(Missing()), 102, GuardAction::Readd, 3);
    CHECK(guard.Known().contains(102));
    guard.NoteKick(102, 1001); // Separate actual removal confirmation supplied by the adapter.
    Step(guard.Pass(Missing(1001)), 102, GuardAction::HisKick, 3);
}

LEEROY_TEST(CastGuard_FailedCommandIntentIsDroppedWhileStillMember)
{
    GuardState guard(GuildId, Human);
    guard.Pass(Roster());
    guard.NoteCommandKick(102, 1000);
    CHECK(guard.Pass(Roster()).empty());
    CHECK(!guard.PendingCommandKick(102, 1000));
    Step(guard.Pass(Missing(2000)), 102, GuardAction::Readd, 3);
}

LEEROY_TEST(CastGuard_CommandIntentHasExactExpiryAndRollbackRefusal)
{
    GuardState guard(GuildId, Human);
    guard.NoteCommandKick(102, 1000);
    CHECK(!guard.PendingCommandKick(102, 999));
    CHECK(guard.PendingCommandKick(102, 120999));
    CHECK(!guard.PendingCommandKick(102, 121000));
    guard.NoteCommandKick(103, std::numeric_limits<std::uint64_t>::max() - 100);
    CHECK(guard.PendingCommandKick(103, std::numeric_limits<std::uint64_t>::max()));
    CHECK(!guard.PendingCommandKick(103, 10));
    guard.NoteCommandKick(103, 10); // Rollback does not rewind the original timestamp.
    CHECK(!guard.PendingCommandKick(103, 10));
    guard.NoteCommandKick(0, 1);
    guard.NoteCommandKick(Human, 1);
    CHECK(!guard.PendingCommandKick(0, 1));
    CHECK(!guard.PendingCommandKick(Human, 1));
}

LEEROY_TEST(CastGuard_CommandIntentStorageIsBoundedAndReclaimedAfterExpiry)
{
    GuardState guard(GuildId, Human);
    for (std::uint32_t guid = 1000; guid < 2024; ++guid)
        guard.NoteCommandKick(guid, 0);
    guard.NoteCommandKick(9999, 1);
    CHECK(!guard.PendingCommandKick(9999, 1));
    guard.NoteCommandKick(9999, 120000);
    CHECK(guard.PendingCommandKick(9999, 120000));
}

LEEROY_TEST(CastGuard_MissingCharacterLookupIsUnknownNotGone)
{
    GuardState guard(GuildId, Human);
    guard.Pass(Roster());
    GuardInput unknown{{}, {}, 1000};
    CHECK(guard.Pass(unknown).empty());
    CHECK_EQ(guard.Known().size(), std::size_t{2});
    unknown.chars[102] = {true, GuildId, false}; // A partial roster conflicts with the character cache.
    CHECK(guard.Pass(unknown).empty());
    CHECK_EQ(guard.Known().size(), std::size_t{2});
    Step(guard.Pass(Missing(2000)), 102, GuardAction::Readd, 3);
}

LEEROY_TEST(CastGuard_OnlyExplicitCharacterDeletionIsGone)
{
    GuardState guard(GuildId, Human);
    guard.Pass(Roster());
    auto input = Missing();
    input.chars[102] = {false, 0, false};
    Step(guard.Pass(input), 102, GuardAction::Gone, 3);
    CHECK(!guard.Known().contains(102));
    CHECK(guard.Pass(Missing(100000)).empty());
}

LEEROY_TEST(CastGuard_OtherBotGuildMustBeLeftButRealGuildIsNeverTouched)
{
    GuardState guard(GuildId, Human);
    guard.Pass(Roster());
    auto input = Missing();
    input.chars[102] = {true, 50, true};
    Step(guard.Pass(input), 102, GuardAction::LeaveBotGuildThenReadd, 3);
    guard.NoteFailed(102, input.nowMs);
    input.nowMs += 59999;
    CHECK(guard.Pass(input).empty());
    ++input.nowMs;
    Step(guard.Pass(input), 102, GuardAction::LeaveBotGuildThenReadd, 3);
    input.chars[102] = {true, 60, false};
    Step(guard.Pass(input), 102, GuardAction::Blocked, 3);
    CHECK(guard.Pass(input).empty());
    input.chars[102].guildId = 61;
    Step(guard.Pass(input), 102, GuardAction::Blocked, 3);
    CHECK(guard.Known().contains(102));
    input.chars[102] = {true, 0, false};
    input.nowMs += 60000;
    Step(guard.Pass(input), 102, GuardAction::Readd, 3);
    input.chars[102] = {true, 61, false};
    Step(guard.Pass(input), 102, GuardAction::Blocked, 3); // A real intervening change permits a new diagnostic.
}

LEEROY_TEST(CastGuard_CapsAttemptsAndDoesNotStarveNextBatchWithoutResults)
{
    GuardState guard(GuildId, Human);
    GuardInput input{{}, {}, 1000};
    for (std::uint32_t guid = 101; guid <= 108; ++guid)
        input.members[guid] = static_cast<std::uint8_t>(guid - 100);
    guard.Pass(input);
    input.members.clear();
    for (std::uint32_t guid = 101; guid <= 108; ++guid)
        input.chars[guid] = {true, 0, false};
    auto first = guard.Pass(input, 1000); // Caller cannot lift the five-mutation ceiling.
    CHECK_EQ(first.size(), std::size_t{5});
    CHECK_EQ(first.front().guid, 101u);
    CHECK_EQ(first.back().guid, 105u);
    auto second = guard.Pass(input);
    CHECK_EQ(second.size(), std::size_t{3});
    CHECK_EQ(second.front().guid, 106u);
    CHECK_EQ(second.back().guid, 108u);
    CHECK(guard.Pass(input).empty());
    CHECK_EQ(guard.Known().size(), std::size_t{8});
}

LEEROY_TEST(CastGuard_KnownSurvivesPartialRefreshAndRestoresRanks)
{
    GuardState guard(GuildId, Human);
    GuardInput input{{}, {}, 1000};
    for (std::uint32_t guid = 101; guid <= 110; ++guid)
        input.members[guid] = static_cast<std::uint8_t>(guid - 100);
    guard.Pass(input);
    for (std::uint32_t guid = 103; guid <= 110; ++guid)
    {
        input.members.erase(guid);
        input.chars[guid] = {true, 0, false};
    }
    auto first = guard.Pass(input);
    CHECK_EQ(first.size(), std::size_t{5});
    for (auto const& step : first)
        input.members[step.guid] = step.rank;
    auto second = guard.Pass(input);
    CHECK_EQ(second.size(), std::size_t{3});
    for (auto const& step : second)
    {
        CHECK_EQ(step.rank, static_cast<std::uint8_t>(step.guid - 100));
        input.members[step.guid] = step.rank;
    }
    CHECK(guard.Pass(input).empty());
    CHECK_EQ(guard.Known().size(), std::size_t{10});
}

LEEROY_TEST(CastGuard_ZeroMutationBudgetStillProcessesConfirmedRemovals)
{
    GuardState guard(GuildId, Human);
    guard.Pass(Roster());
    CHECK(guard.Pass(Missing(), 0).empty());
    guard.NoteKick(102, 1000);
    Step(guard.Pass(Missing(), 0), 102, GuardAction::HisKick, 3);
}

LEEROY_TEST(CastGuard_FailureCooldownDoesNotUnderflowOrRewind)
{
    GuardState guard(GuildId, Human);
    guard.Pass(Roster());
    guard.NoteFailed(102, 100000);
    guard.NoteFailed(102, 1);
    CHECK(guard.Pass(Missing(1)).empty());
    CHECK(guard.Pass(Missing(159999)).empty());
    Step(guard.Pass(Missing(160000)), 102, GuardAction::Readd, 3);
    guard.NoteFailed(102, std::numeric_limits<std::uint64_t>::max() - 100);
    CHECK(guard.Pass(Missing(std::numeric_limits<std::uint64_t>::max())).empty());
    CHECK(guard.Pass(Missing(60000)).empty());
    guard.Pass(Roster(60000)); // Observed return clears the old attempt.
    Step(guard.Pass(Missing(60001)), 102, GuardAction::Readd, 3);
}

LEEROY_TEST(CastGuard_AllDecisionKindsRemainInGuidOrder)
{
    GuardState guard(GuildId, Human);
    GuardInput input{{{101, 1}, {102, 2}, {103, 3}, {104, 4}}, {}, 1000};
    guard.Pass(input);
    guard.NoteKick(104, 1000);
    input.members.clear();
    input.chars = {{101, {true, 0, false}}, {102, {false, 0, false}}, {103, {true, 60, false}}};
    auto steps = guard.Pass(input);
    CHECK_EQ(steps.size(), std::size_t{4});
    for (std::size_t i = 0; i < steps.size(); ++i)
        CHECK_EQ(steps[i].guid, 101u + static_cast<std::uint32_t>(i));
}

LEEROY_TEST(CastGuard_ParsesExactNamedAndTargetedGmUninvite)
{
    for (auto command : {".guild uninvite Coros", "  GUILD UNINVITE Coros  ", ".gu un Coros",
        ".guild uni Coros", "gu u Coros", "\t.guild\tuninvite\tCoros\t"})
    {
        auto parsed = ParseGuildUninvite(command);
        CHECK(parsed.has_value());
        if (parsed)
            CHECK(parsed->name == std::optional<std::string>{"Coros"});
    }
    for (auto command : {".guild uninvite", " gu u  ", ".GUILD U\t"})
    {
        auto parsed = ParseGuildUninvite(command);
        CHECK(parsed.has_value());
        if (parsed)
            CHECK(!parsed->name.has_value());
    }
}

LEEROY_TEST(CastGuard_RejectsExtraTokensControlsAndCommandLookalikes)
{
    for (auto command : {"", ".", "guild", ".guild invite Coros", ".guildx uninvite Coros", ".g uninvite Coros",
        ".guild uninvited Coros", "..guild uninvite Coros", "/guild uninvite Coros", ".guild uninvite Coros Extra",
        ".guild uninvite \"Coros\"", ".guild uninvite Coros;", ".guild uninvite Coros\n",
        ".guild uninvite |Hplayer:Coros|h[Coros]|h", ".guild uninvite Coros123", ".guild uninvite Coros/Other"})
        CHECK(!ParseGuildUninvite(command));
    CHECK(!ParseGuildUninvite(std::string{'.', 'g', 'u', ' ', 'u', ' ', 'A', '\0', 'B'}));
    CHECK(!ParseGuildUninvite(".gu u " + std::string(49, 'A')));
    CHECK(!ParseGuildUninvite(std::string(257, ' ')));
}

LEEROY_TEST(CastGuard_NameEncodingIsBoundedAndPreservedForCoreResolution)
{
    std::string const name = "M\xc3\xa1ge";
    auto parsed = ParseGuildUninvite(".guild uninvite " + name);
    CHECK(parsed.has_value());
    if (parsed)
        CHECK(parsed->name == std::optional<std::string>{name});
    for (auto bytes : {std::string{"\xc0\xaf"}, std::string{"\xe2\x82"}, std::string{"\xed\xa0\x80"},
        std::string{"\xf4\x90\x80\x80"}, std::string{"\xc2\x80"}})
        CHECK(!ParseGuildUninvite(".gu u " + bytes));
}
