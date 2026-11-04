// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#include "LeeroyRealmGuild.h"
#include "LeeroyRealmQueue.h"
#include "LeeroyCastGuard.h"
#include "TestHarness.h"
#include <array>

using namespace Leeroy::Realm;
namespace
{
GuildRecruitFacts Recruit()
{
    GuildRecruitFacts facts;
    facts.randomAccount = facts.exists = facts.factionMatches = true;
    facts.level = 80;
    return facts;
}
GuildJoinReceipt Receipt()
{
    BodyIdentity body{20, 0, 571, 0, 0, 8, 1, 0};
    return {"grow-one", 301, 62, 41, 9, 5, 7, body, 4};
}
BodyIdentity Joined(GuildJoinReceipt const& receipt)
{
    auto body = receipt.original; body.guild = receipt.destination; return body;
}
LeaseCheck Check(GuildJoinReceipt const& receipt, std::optional<BodyIdentity> const& current,
    std::uint64_t generation = 7, std::vector<BodyScope> const& scopes = {})
{
    auto const original = Receipt();
    return CheckGuildJoinLease(receipt, "grow-one", 301, 41, 9, 5, original.original, 7,
        current, generation, scopes);
}
}

LEEROY_TEST(RealmGuild_RecruitmentRefusesEveryProtectedCategory)
{
    CHECK(GuildRecruitReason(Recruit()).empty());
    for (auto field : {&GuildRecruitFacts::human, &GuildRecruitFacts::cast,
        &GuildRecruitFacts::configuredMember, &GuildRecruitFacts::protectedFormer})
    {
        auto facts = Recruit(); facts.*field = true;
        CHECK_EQ(GuildRecruitReason(facts), "cast");
    }
    auto facts = Recruit(); facts.actor = true; CHECK_EQ(GuildRecruitReason(facts), "actor");
    facts = Recruit(); facts.randomAccount = false; CHECK_EQ(GuildRecruitReason(facts), "not_bot");
    facts = Recruit(); facts.exists = false; CHECK_EQ(GuildRecruitReason(facts), "not_bot");
    facts = Recruit(); facts.guild = 63; CHECK_EQ(GuildRecruitReason(facts), "not_guildless");
    for (auto level : {0u, 1u, 79u, 81u})
    { facts = Recruit(); facts.level = level; CHECK_EQ(GuildRecruitReason(facts), "level"); }
    facts = Recruit(); facts.factionMatches = false; CHECK_EQ(GuildRecruitReason(facts), "faction");
}

LEEROY_TEST(RealmGuild_JoinProofRequiresNativeReceiptAndAllReadbacks)
{
    auto const receipt = Receipt();
    auto const current = Joined(receipt);
    auto prove = [&](bool added, bool roster, std::uint32_t account, std::uint8_t rank,
        std::uint32_t cacheGuild, std::optional<BodyIdentity> const& body, std::uint64_t generation)
    { return ProveGuildJoin(receipt, added, roster, account, rank, cacheGuild, body, generation); };
    CHECK(prove(true, true, 20, 4, 62, current, 7));
    CHECK(!prove(false, true, 20, 4, 62, current, 7)); // Later roster alone cannot prove this add.
    CHECK(!prove(true, false, 20, 4, 62, current, 7));
    CHECK(!prove(true, true, 21, 4, 62, current, 7));
    CHECK(!prove(true, true, 20, 3, 62, current, 7));
    CHECK(!prove(true, true, 20, 4, 0, current, 7));
    CHECK(!prove(true, true, 20, 4, 62, std::nullopt, 7));
    CHECK(!prove(true, true, 20, 4, 62, current, 8));
    CHECK(!prove(true, true, 20, 4, 62, receipt.original, 7));
    CHECK(receipt.original.guild == 0);
    CHECK(!SameLife(receipt.original, current)); // Global identity still refuses a guild change.
    CHECK(CheckBodyLease(receipt.original, 7, current, 7, {}) == LeaseCheck::LifeChanged);
}

LEEROY_TEST(RealmGuild_JoinReceiptCannotTransferOperationOrConfiguredAuthority)
{
    auto const receipt = Receipt();
    auto const current = Joined(receipt);
    CHECK(Check(receipt, current) == LeaseCheck::Exact);
    for (unsigned change = 0; change < 10; ++change)
    {
        auto forged = receipt;
        if (change == 0) forged.operation = "grow-two";
        if (change == 1) forged.guid = 302;
        if (change == 2) forged.destination = 63;
        if (change == 3) forged.destination = 41;
        if (change == 4) forged.configuredGuild = 42;
        if (change == 5) forged.human = 10;
        if (change == 6) forged.epoch = 6;
        if (change == 7) forged.generation = 8;
        if (change == 8) forged.original.guild = 64;
        if (change == 9) forged.original.account = 21;
        CHECK(Check(forged, current) == LeaseCheck::LifeChanged);
    }
}

LEEROY_TEST(RealmGuild_JoinReceiptPreservesEveryLifeAndScopeFence)
{
    auto const receipt = Receipt();
    auto const joined = Joined(receipt);
    CHECK(Check(receipt, joined, 8) == LeaseCheck::LifeChanged);
    CHECK(Check(receipt, std::nullopt) == LeaseCheck::LifeChanged);
    for (unsigned change = 0; change < 9; ++change)
    {
        auto body = joined;
        if (change == 0) ++body.account;
        if (change == 1) ++body.guild;
        if (change == 2) ++body.classId;
        if (change == 3) ++body.race;
        if (change == 4) ++body.sex;
        if (change == 5) body.player = reinterpret_cast<Player*>(std::uintptr_t{0x1000});
        if (change == 6) body.ai = reinterpret_cast<PlayerbotAI*>(std::uintptr_t{0x2000});
        if (change == 7) body.session = reinterpret_cast<WorldSession*>(std::uintptr_t{0x3000});
        if (change == 8) body.guild = 0;
        CHECK(Check(receipt, body) == LeaseCheck::LifeChanged);
    }
    for (unsigned change = 0; change < 3; ++change)
    {
        auto body = joined;
        if (change == 0) ++body.map;
        if (change == 1) ++body.instance;
        if (change == 2) ++body.group;
        CHECK(Check(receipt, body) == LeaseCheck::ScopeChanged);
        CHECK(Check(receipt, body, 7, {Scope(body)}) == LeaseCheck::Exact);
    }
    CHECK(Check(receipt, joined, 7, std::vector<BodyScope>(5)) == LeaseCheck::ScopeChanged);
}

LEEROY_TEST(RealmGuild_WholeManifestIncludesCompletedAndUnstartedMembers)
{
    auto const receipt = Receipt();
    std::vector<std::uint32_t> const members{301, 302, 303};
    std::set<std::uint32_t> attempts{301};
    std::map<std::uint32_t, GuildJoinReceipt> receipts{{301, receipt}};
    std::vector<std::uint32_t> checked;
    CHECK(GuildJoinManifestClean(members, attempts, receipts, [&](std::uint32_t guid)
    { checked.push_back(guid); return true; }));
    CHECK(checked == members);
    for (auto failing : members)
        CHECK(!GuildJoinManifestClean(members, attempts, receipts,
            [&](std::uint32_t guid) { return guid != failing; }));
    attempts.insert(302); // An entered call without proof retains the entire manifest.
    CHECK(!GuildJoinManifestClean(members, attempts, receipts, [](auto) { return true; }));
    attempts = {304};
    receipts.clear(); auto other = receipt; other.guid = 304; receipts.emplace(304, other);
    CHECK(!GuildJoinManifestClean(members, attempts, receipts, [](auto) { return true; }));
    for (auto bad : {std::vector<std::uint32_t>{}, std::vector<std::uint32_t>{301,301},
        std::vector<std::uint32_t>{0}, std::vector<std::uint32_t>(31, 301)})
        CHECK(!GuildJoinManifestClean(bad, {}, {}, [](auto) { return true; }));
}

LEEROY_TEST(RealmGuild_GenericRecruitmentCannotClearOwnerKickOrFormerProtection)
{
    GuardState guard(41, 9);
    CHECK(!guard.RecruitmentProtected(301));
    guard.Pass({{{301, 4}}, {}, 1000});
    CHECK(guard.RecruitmentProtected(301));
    guard.Pass({{}, {{301, {true, 0, false}}}, 2000});
    CHECK(guard.RecruitmentProtected(301));
    guard.NoteKick(301, 3000);
    guard.Pass({{}, {{301, {true, 0, false}}}, 4000});
    CHECK(guard.Known().empty());
    CHECK(guard.RecruitmentProtected(301));
    auto facts = Recruit(); facts.protectedFormer = guard.RecruitmentProtected(301);
    CHECK_EQ(GuildRecruitReason(facts), "cast");
    guard.Pass({{{301, 4}}, {}, 5000}); // Unattributed membership is no authorization.
    CHECK(guard.RecruitmentProtected(301));
    CHECK(guard.Known().empty());
    CHECK(guard.AllowRejoin(301)); // Only a separate verified owner path may do this.
    CHECK(!guard.RecruitmentProtected(301));
}

LEEROY_TEST(RealmGuild_EnabledIdentitySwitchKeepsPartialCountAndWholeManifest)
{
    auto const receipt = Receipt();
    std::vector<std::uint32_t> const members{301, 302, 303};
    ActionQueue queue;
    CHECK(queue.Push({"grow-one", "guild_grow", members, 0, {{"guild_id", 62}}}));
    std::map<std::uint32_t, std::string> holders;
    for (auto guid : members) holders.emplace(guid, "grow-one");
    auto const admittedHolders = holders;
    std::set<std::uint32_t> attempts;
    std::map<std::uint32_t, GuildJoinReceipt> receipts;
    TickBudget budget(1);
    queue.Drain(budget, [&](ActionQueue::Entry& entry)
    {
        CHECK_EQ(entry.guids.at(entry.cursor), receipt.guid);
        attempts.insert(receipt.guid);
        CHECK(ProveGuildJoin(receipt, true, true, 20, 4, 62, Joined(receipt), 7));
        receipts.emplace(receipt.guid, receipt);
        ++entry.cursor;
        return false;
    });
    CHECK_EQ(receipts.size(), std::size_t{1});
    bool retained = false;
    unsigned releases = 0;
    std::optional<RealmJob> cancelled;
    // An identity boundary fences the old queue even when the new Realm stays
    // enabled. Publication still goes through the module's existing On gate.
    queue.DropIf([](auto const& entry) { return entry.domain == ActivityDomain::Realm; },
        "configuration_changed", [&](ActionQueue::Entry const& entry, std::string const&)
    {
        CHECK_EQ(entry.cursor, std::size_t{1});
        cancelled = CancelGuildJoinReservation({entry.id, entry.kind, false, "realm_off"},
            false, attempts, receipts, [&] { retained = true; }, [&]
            {
                ++releases;
                holders.clear();
                receipts.clear();
            });
    });
    CHECK_EQ(queue.Size(), std::size_t{0});
    CHECK(cancelled.has_value());
    if (cancelled)
    {
        CHECK_EQ(cancelled->id, "grow-one"); CHECK_EQ(cancelled->kind, "guild_grow");
        CHECK(!cancelled->ok); CHECK_EQ(cancelled->reason, "realm_off");
        CHECK_EQ(cancelled->value, std::uint64_t{1});
        auto const wire = MakeRealmJob(*cancelled);
        CHECK_EQ(wire.at("state").get<std::string>(), "failed");
        CHECK_EQ(wire.at("value").get<std::uint64_t>(), std::uint64_t{1});
    }
    CHECK(retained); CHECK_EQ(releases, 0u);
    CHECK(holders == admittedHolders); // Completed, current and unstarted bodies stay held.
    CHECK_EQ(receipts.size(), std::size_t{1});

    attempts.insert(302); // An additional unproved call is retained but never counted as proved.
    auto unknown = CancelGuildJoinReservation({"grow-one", "guild_grow", false, "realm_off"},
        false, attempts, receipts, [&] { retained = true; }, [&] { ++releases; });
    CHECK_EQ(unknown.value, std::uint64_t{1}); CHECK_EQ(releases, 0u);
    CHECK(holders == admittedHolders);

    unsigned emptyRetains = 0, emptyReleases = 0;
    auto unstarted = CancelGuildJoinReservation({"grow-two", "guild_grow", false, "realm_off"},
        false, {}, {}, [&] { ++emptyRetains; }, [&] { ++emptyReleases; });
    CHECK_EQ(unstarted.value, std::uint64_t{0});
    CHECK_EQ(emptyRetains, 0u); CHECK_EQ(emptyReleases, 1u);
    auto pending = CancelGuildJoinReservation({"other-one", "realm_move", false, "realm_off"},
        true, {}, {}, [&] { ++emptyRetains; }, [&] { ++emptyReleases; });
    CHECK_EQ(pending.value, std::uint64_t{0});
    CHECK_EQ(emptyRetains, 1u); CHECK_EQ(emptyReleases, 1u);
}

LEEROY_TEST(RealmGuild_GrantRefusesBeforeNativeEntryAndRequiresHasAchieved)
{
    unsigned calls = 0, reads = 0;
    bool achieved = false;
    auto complete = [&] { ++calls; };
    auto read = [&] { ++reads; return achieved; };
    CHECK_EQ(ApplyGrantAchievement(1402, false, false, complete, read), "bad field: achievement_id");
    CHECK_EQ(ApplyGrantAchievement(3117, true, false, complete, read), "gm_mode");
    CHECK_EQ(ApplyGrantAchievement(3117, true, true, complete, read), "gm_mode");
    CHECK_EQ(ApplyGrantAchievement(3117, false, true, complete, read), "has_it");
    CHECK_EQ(calls, 0u); CHECK_EQ(reads, 0u);
    CHECK_EQ(ApplyGrantAchievement(3117, false, false, complete, read), "refused");
    CHECK_EQ(calls, 1u); CHECK_EQ(reads, 1u);
    achieved = true;
    for (auto id : kGrantable)
        CHECK(ApplyGrantAchievement(id, false, false, complete, read).empty());
    CHECK_EQ(calls, 8u); CHECK_EQ(reads, 8u);
}
