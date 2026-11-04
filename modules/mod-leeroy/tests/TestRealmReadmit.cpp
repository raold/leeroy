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
    CHECK_EQ(Reason(DecideReadmit(nullptr, Request(3), Facts())), "not_kicked"); // Includes after a restart.
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
