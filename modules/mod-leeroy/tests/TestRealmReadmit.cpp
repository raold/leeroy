// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmReadmit.h"
#include "TestHarness.h"

#include <algorithm>
#include <cstdint>
#include <fstream>
#include <functional>
#include <iterator>
#include <stdexcept>
#include <string>
#include <vector>

using namespace Leeroy::Realm;

namespace
{
constexpr std::uint32_t GuildId = 41, Human = 9, Kicked = 102;
constexpr std::uint64_t KickTs = 1700000000000;

// The adapter's order: the guard learns the member, he kicks, the next pass
// publishes his_kick at KickTs and the receipt records that exact event.
GuardState Kickedby(std::uint8_t rank = 3)
{
    GuardState guard(GuildId, Human);
    guard.Pass({{{101, 2}, {Kicked, rank}}, {}, 1000});
    guard.NoteKick(Kicked, 2000);
    auto const steps = guard.Pass({{{101, 2}}, {{Kicked, {true, 0, false}}}, 3000});
    CHECK(steps.size() == 1 && steps[0].action == GuardAction::HisKick && steps[0].rank == rank);
    CHECK(guard.NoteKickReceipt(Kicked, KickTs, rank));
    return guard;
}

GuildReadmitCmd Request(std::uint32_t rank = 3, std::uint64_t kickTs = KickTs)
{
    return {"readmit-1", Kicked, kickTs, rank};
}

ReadmitFacts Facts()
{
    ReadmitFacts facts;
    facts.guardAvailable = true;
    facts.exists = facts.randomAccount = facts.factionMatches = facts.rankExists = true;
    facts.nowUnixMs = KickTs + 3600000;
    return facts;
}

std::string Source(char const* file)
{
    auto const path = std::string(LEEROY_TEST_FIXTURES) + "/../../src/Realm/" + file;
    std::ifstream stream(path);
    if (!stream) throw std::runtime_error("cannot open native source contract: " + path);
    std::string source{std::istreambuf_iterator<char>(stream), std::istreambuf_iterator<char>()};
    source.erase(std::remove(source.begin(), source.end(), '\r'), source.end());
    return source;
}

std::string Section(std::string const& source, std::string const& start, std::string const& end)
{
    auto const first = source.find(start);
    auto const last = first == std::string::npos ? first : source.find(end, first + start.size());
    if (first == std::string::npos || last == std::string::npos)
        throw std::runtime_error("native source contract section missing: " + start);
    return source.substr(first, last - first);
}

std::string Reason(ReadmitDecision const& decision)
{
    return decision.verdict == ReadmitVerdict::Refuse ? decision.reason
        : decision.verdict == ReadmitVerdict::Admit ? "admit" : "done";
}
}

LEEROY_TEST(RealmReadmit_OnlyHisPublishedKickGetsAReceipt)
{
    GuardState guard(GuildId, Human);
    guard.Pass({{{101, 2}, {Kicked, 3}}, {}, 1000});
    CHECK(!guard.NoteKickReceipt(Kicked, KickTs, 3)); // Still a member: no kick at all.
    guard.NoteKick(Kicked, 2000);
    CHECK(guard.KickPending(Kicked));
    CHECK(!guard.NoteKickReceipt(Kicked, KickTs, 3)); // Not yet published as his_kick.
    guard.Pass({{{101, 2}}, {{Kicked, {true, 0, false}}}, 3000});
    CHECK(!guard.KickPending(Kicked));
    CHECK(!guard.NoteKickReceipt(Kicked, 0, 3));
    CHECK(!guard.NoteKickReceipt(Human, KickTs, 0));
    CHECK(!guard.NoteKickReceipt(101, KickTs, 2)); // A guard re-add or a leave is never his kick.
    CHECK(guard.NoteKickReceipt(Kicked, KickTs, 3));
    auto const* receipt = guard.Receipt(Kicked);
    CHECK(receipt && receipt->kickTs == KickTs && receipt->rank == 3 && receipt->state == KickReceipt::State::Open);
}

LEEROY_TEST(RealmReadmit_AdmitsExactlyTheRecordedKickAtItsRank)
{
    auto guard = Kickedby(3);
    CHECK_EQ(Reason(DecideReadmit(guard.Receipt(Kicked), Request(3), Facts())), "admit");
    CHECK_EQ(Reason(DecideReadmit(guard.Receipt(Kicked), Request(3, KickTs + 1), Facts())), "not_kicked");
    CHECK_EQ(Reason(DecideReadmit(guard.Receipt(Kicked), Request(4), Facts())), "rank_mismatch");
    CHECK_EQ(Reason(DecideReadmit(nullptr, Request(3), Facts())), "not_kicked"); // Never stored, or not restored.
    CHECK(guard.RecruitmentProtected(Kicked)); // guild_admit keeps refusing him.
    CHECK(guard.CompleteReadmit(Kicked, KickTs));
    CHECK(!guard.RecruitmentProtected(Kicked)); // His exclusion is lifted for the readmitted member only.
    CHECK(guard.Receipt(Kicked)->state == KickReceipt::State::Readmitted);
    CHECK(!guard.CompleteReadmit(Kicked, KickTs)); // Once.
    auto steps = guard.Pass({{{101, 2}, {Kicked, 3}}, {}, 5000});
    CHECK(steps.empty());
    CHECK_EQ(guard.Known().at(Kicked), std::uint8_t{3}); // The roster, not the receipt, restores protection.
}

LEEROY_TEST(RealmReadmit_IsIdempotentWithoutASecondAdd)
{
    auto guard = Kickedby(3);
    CHECK(guard.CompleteReadmit(Kicked, KickTs));
    auto facts = Facts(); facts.member = true; facts.guild = GuildId;
    CHECK_EQ(Reason(DecideReadmit(guard.Receipt(Kicked), Request(3), facts)), "done");
    CHECK_EQ(Reason(DecideReadmit(guard.Receipt(Kicked), Request(3), Facts())), "readmit_consumed"); // Left again.
    auto unknown = Kickedby(3);
    unknown.ReadmitUnknown(Kicked, KickTs);
    CHECK_EQ(Reason(DecideReadmit(unknown.Receipt(Kicked), Request(3), Facts())), "readmit_unproved");
    CHECK(!unknown.CompleteReadmit(Kicked, KickTs));
    CHECK(unknown.RecruitmentProtected(Kicked));
}

LEEROY_TEST(RealmReadmit_ANewKickReplacesTheOldReceipt)
{
    auto guard = Kickedby(3);
    CHECK(guard.CompleteReadmit(Kicked, KickTs));
    guard.Pass({{{101, 2}, {Kicked, 3}}, {}, 5000});
    guard.NoteKick(Kicked, 6000);
    auto facts = Facts(); facts.member = true; facts.guild = GuildId; facts.kickPending = true;
    CHECK_EQ(Reason(DecideReadmit(guard.Receipt(Kicked), Request(3), facts)), "readmit_consumed");
    guard.Pass({{{101, 2}}, {{Kicked, {true, 0, false}}}, 7000});
    CHECK(guard.NoteKickReceipt(Kicked, KickTs + 10000, 3));
    CHECK_EQ(Reason(DecideReadmit(guard.Receipt(Kicked), Request(3), Facts())), "not_kicked"); // The old consent.
    auto later = Facts(); later.nowUnixMs = KickTs + 20000;
    CHECK_EQ(Reason(DecideReadmit(guard.Receipt(Kicked), Request(3, KickTs + 10000), later)), "admit");
}

LEEROY_TEST(RealmReadmit_RanksFollowGuildAdmitsBounds)
{
    CHECK(ReadmitRank(0) == std::nullopt);
    CHECK(ReadmitRank(1) == std::optional<std::uint8_t>{2});
    for (std::uint8_t rank = 2; rank <= 9; ++rank) CHECK(ReadmitRank(rank) == std::optional<std::uint8_t>{rank});
    auto officer = Kickedby(1);
    CHECK_EQ(Reason(DecideReadmit(officer.Receipt(Kicked), Request(2), Facts())), "admit");
    CHECK_EQ(Reason(DecideReadmit(officer.Receipt(Kicked), Request(1), Facts())), "rank_mismatch");
    GuardState unlearned(GuildId, Human); // Kicked before any pass learned a rank.
    unlearned.NoteKick(Kicked, 1000);
    unlearned.Pass({{{101, 2}}, {{Kicked, {true, 0, false}}}, 2000});
    CHECK(unlearned.NoteKickReceipt(Kicked, KickTs, 0));
    CHECK_EQ(Reason(DecideReadmit(unlearned.Receipt(Kicked), Request(2), Facts())), "rank_unknown");
    auto facts = Facts(); facts.rankExists = false;
    CHECK_EQ(Reason(DecideReadmit(Kickedby(3).Receipt(Kicked), Request(3), facts)), "no_rank");
}

LEEROY_TEST(RealmReadmit_RefusesEveryOtherRejoinCondition)
{
    auto guard = Kickedby(3);
    auto const* receipt = guard.Receipt(Kicked);
    std::vector<std::pair<std::function<void(ReadmitFacts&)>, std::string>> const cases{
        {[](auto& f) { f.guardAvailable = false; }, "guard_unknown"},
        {[](auto& f) { f.human = true; }, "cast"},
        {[](auto& f) { f.kickPending = true; }, "kick_pending"},
        {[](auto& f) { f.nowUnixMs = KickTs - 1; }, "clock_changed"},
        {[](auto& f) { f.nowUnixMs = KickTs + kReadmitWindowMs + 1; }, "kick_expired"},
        {[](auto& f) { f.actor = true; }, "actor"},
        {[](auto& f) { f.exists = false; }, "gone"},
        {[](auto& f) { f.randomAccount = false; }, "not_bot"},
        {[](auto& f) { f.member = true; f.guild = GuildId; }, "already_member"},
        {[](auto& f) { f.guild = 77; }, "not_guildless"},
        {[](auto& f) { f.factionMatches = false; }, "faction"},
    };
    for (auto const& [change, reason] : cases)
    {
        auto facts = Facts(); change(facts);
        CHECK_EQ(Reason(DecideReadmit(receipt, Request(3), facts)), reason);
    }
    auto edge = Facts(); edge.nowUnixMs = KickTs + kReadmitWindowMs;
    CHECK_EQ(Reason(DecideReadmit(receipt, Request(3), edge)), "admit");
    CHECK(guard.Receipt(Kicked)->state == KickReceipt::State::Open); // A refusal changes nothing.
}

LEEROY_TEST(RealmReadmit_ReceiptCapacityNeverDisplacesAnOpenConsent)
{
    GuardState guard(GuildId, Human);
    std::map<std::uint32_t, std::uint8_t> members;
    for (std::uint32_t guid = 1000; guid < 2026; ++guid) members.emplace(guid, 4);
    guard.Pass({members, {}, 1000});
    for (std::uint32_t guid = 1000; guid < 2026; ++guid) guard.NoteKick(guid, 2000);
    std::map<std::uint32_t, CharState> gone;
    for (std::uint32_t guid = 1000; guid < 2026; ++guid) gone.emplace(guid, CharState{true, 0, false});
    guard.Pass({{}, gone, 3000});
    for (std::uint32_t guid = 1000; guid < 2024; ++guid) CHECK(guard.NoteKickReceipt(guid, KickTs, 4));
    CHECK(!guard.NoteKickReceipt(2024, KickTs, 4)); // 1024 open consents: the next is not recorded.
    CHECK(guard.CompleteReadmit(1000, KickTs));
    CHECK(guard.NoteKickReceipt(2024, KickTs, 4)); // A settled receipt makes room.
    CHECK(!guard.Receipt(1000));
    CHECK(guard.Receipt(1001) && guard.Receipt(2024));
}

namespace
{
constexpr std::uint64_t Now = KickTs + 3600000;

// What the store holds for a receipt, as the adapter writes it.
StoredKickReceipt Stored(KickReceipt const& receipt, std::uint32_t guid = Kicked)
{
    return {guid, receipt.kickTs, receipt.rank, KickReceiptCode(receipt.state)};
}

StoredKickReceipt Row(std::uint8_t state, std::uint64_t kickTs = KickTs, std::uint8_t rank = 3,
    std::uint32_t guid = Kicked)
{
    return {guid, kickTs, rank, state};
}
}

LEEROY_TEST(RealmReadmit_StoredStatesRoundTripAndUnknownCodesNeverOpen)
{
    for (auto state : {KickReceipt::State::Open, KickReceipt::State::Readmitted, KickReceipt::State::Unknown})
        CHECK(KickReceiptFromCode(KickReceiptCode(state)) == state);
    CHECK_EQ(int(KickReceiptCode(KickReceipt::State::Open)), 0);
    CHECK_EQ(int(KickReceiptCode(KickReceipt::State::Readmitted)), 1);
    CHECK_EQ(int(KickReceiptCode(KickReceipt::State::Unknown)), 2);
    for (int code = 3; code <= 255; ++code)
        CHECK(KickReceiptFromCode(static_cast<std::uint8_t>(code)) == KickReceipt::State::Unknown);
    KickReceipt const receipt{KickTs, 3, KickReceipt::State::Open};
    CHECK(StoredReceiptIs(Stored(receipt), Kicked, receipt));
    CHECK(!StoredReceiptIs(Stored(receipt), Kicked + 1, receipt));
    CHECK(!StoredReceiptIs(Row(0, KickTs + 1), Kicked, receipt));
    CHECK(!StoredReceiptIs(Row(0, KickTs, 4), Kicked, receipt));
    CHECK(!StoredReceiptIs(Row(2), Kicked, receipt)); // A write-ahead unknown is not the open receipt.
}

LEEROY_TEST(RealmReadmit_ARestartRestoresAnOpenConsentExactly)
{
    auto const before = Kickedby(3);
    GuardState restarted(GuildId, Human);
    CHECK_EQ(restarted.RestoreReceipts({Stored(*before.Receipt(Kicked))}, Now, kReadmitWindowMs), std::size_t{1});
    auto const* receipt = restarted.Receipt(Kicked);
    CHECK(receipt && receipt->kickTs == KickTs && receipt->rank == 3 && receipt->state == KickReceipt::State::Open);
    CHECK(restarted.RecruitmentProtected(Kicked)); // guild_admit refuses him again after the restart.
    CHECK_EQ(Reason(DecideReadmit(receipt, Request(3), Facts())), "admit");
    CHECK_EQ(Reason(DecideReadmit(receipt, Request(3, KickTs + 1), Facts())), "not_kicked");
    auto steps = restarted.Pass({{{101, 2}}, {}, 5000});
    CHECK(steps.empty()); // A restored kick is never re-added by the guard.
    CHECK(restarted.CompleteReadmit(Kicked, KickTs)); // His exclusion came back with it.
    CHECK(!restarted.RecruitmentProtected(Kicked));
    CHECK(StoredReceiptIs(Row(1), Kicked, *restarted.Receipt(Kicked)));
    CHECK(!restarted.CompleteReadmit(Kicked, KickTs));
}

LEEROY_TEST(RealmReadmit_AStoredReceiptPastTheWindowIsNotRestored)
{
    GuardState edge(GuildId, Human);
    CHECK_EQ(edge.RestoreReceipts({Row(0)}, KickTs + kReadmitWindowMs, kReadmitWindowMs), std::size_t{1});
    auto at = Facts(); at.nowUnixMs = KickTs + kReadmitWindowMs;
    CHECK_EQ(Reason(DecideReadmit(edge.Receipt(Kicked), Request(3), at)), "admit");
    GuardState expired(GuildId, Human);
    CHECK_EQ(expired.RestoreReceipts({Row(0), Row(2, KickTs, 3, 103), Row(1, KickTs, 3, 104)},
        KickTs + kReadmitWindowMs + 1, kReadmitWindowMs), std::size_t{0});
    CHECK(!expired.Receipt(Kicked) && !expired.Receipt(103) && !expired.Receipt(104));
    CHECK(!expired.RecruitmentProtected(Kicked));
    CHECK_EQ(Reason(DecideReadmit(expired.Receipt(Kicked), Request(3), Facts())), "not_kicked");
    GuardState future(GuildId, Human); // A clock that went back keeps it, and the decision refuses.
    CHECK_EQ(future.RestoreReceipts({Row(0, Now + 1)}, Now, kReadmitWindowMs), std::size_t{1});
    auto facts = Facts(); facts.nowUnixMs = Now;
    CHECK_EQ(Reason(DecideReadmit(future.Receipt(Kicked), Request(3, Now + 1), facts)), "clock_changed");
    GuardState noClock(GuildId, Human);
    CHECK_EQ(noClock.RestoreReceipts({Row(0)}, 0, kReadmitWindowMs), std::size_t{0});
    CHECK(!noClock.Receipt(Kicked));
}

LEEROY_TEST(RealmReadmit_ARestoredConsumedReceiptNeverAdmitsAgain)
{
    auto before = Kickedby(3);
    CHECK(before.CompleteReadmit(Kicked, KickTs));
    GuardState restarted(GuildId, Human);
    CHECK_EQ(restarted.RestoreReceipts({Stored(*before.Receipt(Kicked))}, Now, kReadmitWindowMs), std::size_t{1});
    CHECK(restarted.Receipt(Kicked)->state == KickReceipt::State::Readmitted);
    CHECK(!restarted.RecruitmentProtected(Kicked)); // Readmission already lifted his exclusion.
    auto member = Facts(); member.member = true; member.guild = GuildId;
    CHECK_EQ(Reason(DecideReadmit(restarted.Receipt(Kicked), Request(3), member)), "done");
    CHECK_EQ(Reason(DecideReadmit(restarted.Receipt(Kicked), Request(3), Facts())), "readmit_consumed");
    CHECK(!restarted.CompleteReadmit(Kicked, KickTs));
}

LEEROY_TEST(RealmReadmit_UnknownAndMalformedStoredReceiptsNeverAuthorize)
{
    // 2: an unproved add, or the write-ahead of one a crash interrupted.
    for (auto const& row : {Row(2), Row(3), Row(255), Row(0, KickTs, 10), Row(1, KickTs, 200)})
    {
        GuardState restarted(GuildId, Human);
        CHECK_EQ(restarted.RestoreReceipts({row}, Now, kReadmitWindowMs), std::size_t{1});
        CHECK(restarted.Receipt(Kicked)->state == KickReceipt::State::Unknown);
        CHECK(restarted.RecruitmentProtected(Kicked));
        CHECK_EQ(Reason(DecideReadmit(restarted.Receipt(Kicked), Request(3), Facts())), "readmit_unproved");
        CHECK(!restarted.CompleteReadmit(Kicked, KickTs));
    }
    GuardState none(GuildId, Human); // No guid, no ts, or the human himself: nothing to restore.
    CHECK_EQ(none.RestoreReceipts({Row(0, KickTs, 3, 0), Row(0, 0), Row(0, KickTs, 3, Human)}, Now,
        kReadmitWindowMs), std::size_t{0});
    CHECK(!none.Receipt(Kicked) && !none.Receipt(Human) && !none.RecruitmentProtected(Kicked));
}

LEEROY_TEST(RealmReadmit_WhatThisProcessLearnedOutranksTheStore)
{
    auto guard = Kickedby(3); // A receipt from this process.
    CHECK_EQ(guard.RestoreReceipts({Row(0, KickTs - 1000)}, Now, kReadmitWindowMs), std::size_t{0});
    CHECK(guard.Receipt(Kicked)->kickTs == KickTs);
    GuardState pending(GuildId, Human); // A kick this process confirmed but has not yet published.
    pending.Pass({{{101, 2}, {Kicked, 3}}, {}, 1000});
    pending.NoteKick(Kicked, 2000);
    CHECK_EQ(pending.RestoreReceipts({Row(0)}, Now, kReadmitWindowMs), std::size_t{0});
    CHECK(!pending.Receipt(Kicked) && pending.KickPending(Kicked));
    GuardState member(GuildId, Human); // The roster already shows him as a member.
    member.Pass({{{101, 2}, {Kicked, 3}}, {}, 1000});
    CHECK_EQ(member.RestoreReceipts({Row(0)}, Now, kReadmitWindowMs), std::size_t{0});
    CHECK(!member.Receipt(Kicked) && member.Known().contains(Kicked));
    GuardState duplicate(GuildId, Human); // The newest row of a guid wins.
    CHECK_EQ(duplicate.RestoreReceipts({Row(0, KickTs - 5), Row(2, KickTs)}, Now, kReadmitWindowMs), std::size_t{1});
    CHECK(duplicate.Receipt(Kicked)->kickTs == KickTs && duplicate.Receipt(Kicked)->state == KickReceipt::State::Unknown);
}

LEEROY_TEST(RealmReadmit_RestoreKeepsTheReceiptBoundNewestFirst)
{
    std::vector<StoredKickReceipt> rows;
    for (std::uint32_t guid = 1000; guid < 1000 + kMaxKickReceipts + 76; ++guid)
        rows.push_back(Row(0, KickTs + guid, 4, guid));
    GuardState restarted(GuildId, Human);
    CHECK_EQ(restarted.RestoreReceipts(rows, Now + 10000, kReadmitWindowMs), kMaxKickReceipts);
    CHECK(!restarted.Receipt(1000) && !restarted.Receipt(1075));
    CHECK(restarted.Receipt(1076) && restarted.Receipt(1000 + kMaxKickReceipts + 75));
}

LEEROY_TEST(RealmReadmit_SourceContractStoresReceiptsAheadOfTheirEffects)
{
    // Source-placement regressions only, NOT runtime/core integration evidence.
    auto const guild = Source("RealmGuild.cpp");
    auto const readmit = Section(guild, "LeeroyCommandResult Readmit(", "LeeroyCommandResult Note(");
    CHECK(readmit.find("EnsureKickReceiptStore(*guard)") < readmit.find("DecideReadmit("));
    auto const ahead = readmit.find(
        "StoreKickReceipt(*guard, command->guid, {open.kickTs, open.rank, KickReceipt::State::Unknown})");
    CHECK(ahead != std::string::npos && ahead < readmit.find("guild->AddMember("));
    CHECK(ahead < readmit.find("return {false, \"receipt_unstored\"};"));
    CHECK(readmit.find("guard->CompleteReadmit(command->guid, command->kickTs)")
        < readmit.find("settle(KickReceipt::State::Readmitted)"));
    CHECK(readmit.find("settle(KickReceipt::State::Open)") < readmit.find("return {false, \"refused\"};"));
    auto const guard = Source("RealmCastGuard.cpp");
    auto const tick = Section(guard, "void TickCastGuard(", "void AddCastGuardScripts()");
    CHECK(tick.find("EnsureKickReceiptStore(*guard)") < tick.find("guard->Pass(input)"));
    CHECK(tick.find("guard->NoteKickReceipt(step.guid") < tick.find("StoreKickReceipt(*guard, step.guid"));
    auto const store = Source("RealmKickReceipts.cpp");
    CHECK(store.find("return Config().enable && (Config().castGuard || RealmModule::Instance().On());")
        != std::string::npos); // With both off, nothing touches the characters DB.
    auto const ensure = Section(store, "bool EnsureKickReceiptStore(", "bool StoreKickReceipt(");
    CHECK(ensure.find("if (!Active())") < ensure.find("CharacterDatabase."));
    CHECK(ensure.find("guard.RestoreReceipts(rows, now, kReadmitWindowMs)") != std::string::npos);
    auto const write = Section(store, "bool StoreKickReceipt(", "\n}\n}");
    CHECK(write.find("if (!EnsureKickReceiptStore(guard))") < write.find("CharacterDatabase."));
    CHECK(write.find("StoredReceiptIs(") != std::string::npos); // Proved by reading it back.
}

LEEROY_TEST(RealmReadmit_SourceContractAddsOnlyUnderAnExactReceipt)
{
    // Source-placement regressions only, NOT runtime/core integration evidence.
    auto const guild = Source("RealmGuild.cpp");
    auto const readmit = Section(guild, "LeeroyCommandResult Readmit(", "LeeroyCommandResult Note(");
    CHECK(readmit.find("if (!module.On()) return {false, \"realm_off\"};") < readmit.find("DecideReadmit("));
    CHECK(readmit.find("DecideReadmit(") < readmit.find("guild->AddMember("));
    CHECK(readmit.find("module.MutationReason(command->guid)") < readmit.find("guild->AddMember("));
    CHECK(readmit.find("guild->AddMember(") < readmit.find("guard->CompleteReadmit(command->guid, command->kickTs)"));
    CHECK(readmit.find("guard->ReadmitUnknown(command->guid, command->kickTs)") != std::string::npos);
    CHECK(readmit.find("AllowRejoin(") == std::string::npos); // Only through the exact receipt.
    auto const admit = Section(guild, "LeeroyCommandResult Admit(", "LeeroyCommandResult Readmit(");
    CHECK(admit.find("CompleteReadmit") == std::string::npos && admit.find("AllowRejoin(") == std::string::npos);
    CHECK(admit.find("RecruitReason(command->guid, *guild)") != std::string::npos); // Rejoins still refused.
    CHECK(guild.find("RegisterCommand(\"guild_readmit\", Readmit);") != std::string::npos);
    auto const guard = Source("RealmCastGuard.cpp");
    auto const publish = Section(guard, "for (auto step : guard->Pass(input))", "void AddCastGuardScripts()");
    CHECK(publish.find("Publish(EncodeEvent(ts, MakeGuard(") < publish.find("guard->NoteKickReceipt(step.guid"));
    CHECK(publish.find("step.action == GuardAction::HisKick && ts > 0") != std::string::npos);
}
