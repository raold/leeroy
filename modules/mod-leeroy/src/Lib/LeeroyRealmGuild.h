// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_GUILD_POLICY_H
#define MOD_LEEROY_REALM_GUILD_POLICY_H

#include "LeeroyRealmIdentity.h"
#include "LeeroyRealmCommands.h"
#include <algorithm>
#include <map>
#include <set>
#include <string>
#include <string_view>

namespace Leeroy::Realm
{
struct GuildRecruitFacts
{
    bool human = false, cast = false, configuredMember = false, protectedFormer = false;
    bool actor = false, randomAccount = false, exists = false;
    std::uint32_t guild = 0, level = 0;
    bool factionMatches = false;
};

// Native admission policy shared by initial validation and each deferred add.
// A remembered owner kick never becomes rejoin authority through this command.
inline std::string GuildRecruitReason(GuildRecruitFacts const& facts)
{
    if (facts.human || facts.cast || facts.configuredMember || facts.protectedFormer) return "cast";
    if (facts.actor) return "actor";
    if (!facts.randomAccount || !facts.exists) return "not_bot";
    if (facts.guild) return "not_guildless";
    if (facts.level != 80) return "level";
    if (!facts.factionMatches) return "faction";
    return {};
}

struct GuildJoinReceipt
{
    std::string operation;
    std::uint32_t guid = 0, destination = 0, configuredGuild = 0, human = 0;
    std::uint64_t epoch = 0, generation = 0;
    BodyIdentity original;
    std::uint8_t rank = 0;
};

// Only the active native guild_grow adapter can install a receipt. These pure
// checks do not create one from a later roster snapshot or mutate its original
// lease. Every other operation continues to use the strict CheckBodyLease.
inline LeaseCheck CheckGuildJoinLease(GuildJoinReceipt const& receipt, std::string_view operation,
    std::uint32_t guid, std::uint32_t configuredGuild, std::uint32_t human, std::uint64_t epoch,
    BodyIdentity const& saved, std::uint64_t generation, std::optional<BodyIdentity> const& current,
    std::uint64_t currentGeneration, std::vector<BodyScope> const& scopes)
{
    if (operation.empty() || receipt.operation != operation || !guid || receipt.guid != guid
        || !configuredGuild || receipt.configuredGuild != configuredGuild || !human || receipt.human != human
        || !epoch || receipt.epoch != epoch || receipt.original != saved || saved.guild != 0
        || receipt.generation != generation || !receipt.destination || receipt.destination == configuredGuild)
        return LeaseCheck::LifeChanged;
    auto expected = saved;
    expected.guild = receipt.destination;
    return CheckBodyLease(expected, generation, current, currentGeneration, scopes);
}

// Exact readback of this synchronous AddMember, including the character cache
// and member identity. Success means native memory acceptance, not DB durability.
inline bool ProveGuildJoin(GuildJoinReceipt const& intended, bool nativeAdded, bool rosterPresent,
    std::uint32_t rosterAccount, std::uint8_t rosterRank, std::uint32_t cacheGuild,
    std::optional<BodyIdentity> const& current, std::uint64_t currentGeneration)
{
    return nativeAdded && rosterPresent && rosterAccount == intended.original.account
        && rosterRank == intended.rank && cacheGuild == intended.destination
        && CheckGuildJoinLease(intended, intended.operation, intended.guid, intended.configuredGuild,
            intended.human, intended.epoch, intended.original, intended.generation, current,
            currentGeneration, {}) == LeaseCheck::Exact;
}

template<class Verify>
bool GuildJoinManifestClean(std::vector<std::uint32_t> const& members,
    std::set<std::uint32_t> const& attempts, std::map<std::uint32_t, GuildJoinReceipt> const& receipts,
    Verify&& verify)
{
    if (members.empty() || members.size() > 30 || attempts.size() != receipts.size()) return false;
    std::set<std::uint32_t> unique(members.begin(), members.end());
    if (unique.size() != members.size() || unique.contains(0)) return false;
    for (auto guid : attempts)
    {
        auto receipt = receipts.find(guid);
        if (!unique.contains(guid) || receipt == receipts.end() || receipt->second.guid != guid) return false;
    }
    for (auto guid : members)
        if (!verify(guid)) return false;
    return true;
}

// Snapshot exact partial effects before a release can erase the reservation.
// A completed or unproved native join keeps the original whole manifest held;
// configuration cancellation is never a new membership handback receipt.
template<class Retain, class Release>
RealmJob CancelGuildJoinReservation(RealmJob outcome, bool otherOutstanding,
    std::set<std::uint32_t> const& attempts, std::map<std::uint32_t, GuildJoinReceipt> const& receipts,
    Retain&& retain, Release&& release)
{
    outcome.value = receipts.size();
    if (otherOutstanding || !attempts.empty()) retain();
    else release();
    return outcome;
}

template<class Complete, class HasAchieved>
std::string ApplyGrantAchievement(std::uint32_t id, bool gmMode, bool already,
    Complete&& complete, HasAchieved&& achieved)
{
    if (std::find(kGrantable.begin(), kGrantable.end(), id) == kGrantable.end())
        return "bad field: achievement_id";
    if (gmMode) return "gm_mode";
    if (already) return "has_it";
    complete(); // The native caller supplies LeeroyQuietScope around this call.
    return achieved() ? "" : "refused";
}
}
#endif
