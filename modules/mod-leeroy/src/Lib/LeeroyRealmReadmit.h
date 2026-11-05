// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_REALM_READMIT_H
#define MOD_LEEROY_REALM_READMIT_H

#include "LeeroyCastGuard.h"
#include "LeeroyRealmCommands.h"
#include <cstdint>
#include <optional>
#include <string>

namespace Leeroy::Realm
{
// A consent is honoured at most a week after his kick (the mind asks within 72 h
// and accepts an answer within 72 h of the question).
inline constexpr std::uint64_t kReadmitWindowMs = 7ull * 24 * 60 * 60 * 1000;

// His officers (rank 1) come back at rank 2, the highest rank guild_admit allows;
// P1d's rank sync restores the rest. Rank 0 means the guard never learned one.
inline std::optional<std::uint8_t> ReadmitRank(std::uint8_t oldRank)
{
    if (oldRank >= 2) return oldRank;
    if (oldRank == 1) return std::uint8_t{2};
    return std::nullopt;
}

struct ReadmitFacts
{
    bool guardAvailable = false; // The configured guard exists and none of its facts were lost.
    bool kickPending = false;    // A newer confirmed kick of this guid awaits its his_kick step.
    bool human = false, actor = false, exists = false, randomAccount = false, factionMatches = false;
    bool member = false;         // A member of the configured guild in any native view.
    std::uint32_t guild = 0;     // Current guild in any native view; 0 is guildless.
    bool rankExists = false;     // The requested rank exists in the configured guild.
    std::uint64_t nowUnixMs = 0;
};

enum class ReadmitVerdict { Admit, AlreadyDone, Refuse };
struct ReadmitDecision
{
    ReadmitVerdict verdict = ReadmitVerdict::Refuse;
    std::string reason;
};

// Admission policy for guild_readmit, before any mutation. Only an exact open
// receipt of his published kick grants it; every other rejoin keeps guild_admit's
// refusal. A repeat after a proved re-admission answers AlreadyDone, never a second add.
inline ReadmitDecision DecideReadmit(KickReceipt const* receipt, GuildReadmitCmd const& request,
    ReadmitFacts const& facts)
{
    auto const refuse = [](char const* why) { return ReadmitDecision{ReadmitVerdict::Refuse, why}; };
    if (!facts.guardAvailable) return refuse("guard_unknown");
    if (facts.human) return refuse("cast");
    if (!receipt || receipt->kickTs != request.kickTs) return refuse("not_kicked");
    if (receipt->state == KickReceipt::State::Unknown) return refuse("readmit_unproved");
    if (receipt->state == KickReceipt::State::Readmitted)
        return facts.member && !facts.kickPending ? ReadmitDecision{ReadmitVerdict::AlreadyDone, ""}
            : refuse("readmit_consumed");
    if (facts.kickPending) return refuse("kick_pending");
    if (facts.nowUnixMs < receipt->kickTs) return refuse("clock_changed");
    if (facts.nowUnixMs - receipt->kickTs > kReadmitWindowMs) return refuse("kick_expired");
    auto const rank = ReadmitRank(receipt->rank);
    if (!rank) return refuse("rank_unknown");
    if (request.rank != *rank) return refuse("rank_mismatch");
    if (!facts.rankExists) return refuse("no_rank");
    if (facts.actor) return refuse("actor");
    if (!facts.exists) return refuse("gone");
    if (!facts.randomAccount) return refuse("not_bot");
    if (facts.member) return refuse("already_member");
    if (facts.guild) return refuse("not_guildless");
    if (!facts.factionMatches) return refuse("faction");
    return {ReadmitVerdict::Admit, ""};
}
}
#endif
