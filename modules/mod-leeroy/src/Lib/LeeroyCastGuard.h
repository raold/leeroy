// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_CAST_GUARD_H
#define MOD_LEEROY_CAST_GUARD_H

#include <cstddef>
#include <cstdint>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <vector>

namespace Leeroy::Realm
{
struct CharState
{
    bool exists;
    std::uint32_t guildId;
    bool botGuild;
};

enum class GuardAction { Readd, LeaveBotGuildThenReadd, HisKick, Gone, Blocked, Failed };

struct GuardStep
{
    std::uint32_t guid;
    GuardAction action;
    std::uint8_t rank;
};

// The guard's exact record of one published his_kick step: the Unix-ms ts of
// that guard event and the rank it reported (0 when the guard never learned one).
// It binds a consented re-admission to that one kick. Memory only: a worldserver
// restart forgets it, and a re-admission then has no native authority.
struct KickReceipt
{
    enum class State { Open, Readmitted, Unknown };
    std::uint64_t kickTs = 0;
    std::uint8_t rank = 0;
    State state = State::Open;
};

struct GuardInput
{
    std::map<std::uint32_t, std::uint8_t> members;
    // Missing entries mean unknown. Only an explicit exists=false proves deletion.
    std::map<std::uint32_t, CharState> chars;
    std::uint64_t nowMs;
};

// World-thread state only. This pure class makes decisions; it performs no guild mutations.
class GuardState
{
public:
    GuardState(std::uint32_t guildId, std::uint32_t humanLow);

    // Pre-execution command intent is bounded to 120s and never grants removal authority.
    // The adapter must resolve the exact target and confirm the actual removal separately.
    void NoteCommandKick(std::uint32_t guid, std::uint64_t nowMs);
    bool PendingCommandKick(std::uint32_t guid, std::uint64_t nowMs) const;

    // Requires an actual human-attributed removal event, or a verified successful
    // owner command removing its exact target. It must not be called on mere intent.
    // Exclusion survives guard disablement and stale roster refreshes, without a TTL.
    void NoteKick(std::uint32_t guid, std::uint64_t nowMs);

    // Only for a verified owner-authorized admission. Clears the exclusion, but
    // does not learn a rank or schedule an add: a current roster must show the rejoin.
    bool AllowRejoin(std::uint32_t guid);

    // After the adapter published this his_kick step at kickTs (Unix ms). Replaces
    // an older receipt for the guid; refused for a guid not removed by him.
    bool NoteKickReceipt(std::uint32_t guid, std::uint64_t kickTs, std::uint8_t rank);
    KickReceipt const* Receipt(std::uint32_t guid) const;
    // A confirmed kick of the guid that no published his_kick step reports yet.
    bool KickPending(std::uint32_t guid) const { return _pendingKicks.contains(guid); }
    // A proved native re-admission under this exact open receipt: AllowRejoin,
    // and the receipt stays as Readmitted so a repeat answers without a second add.
    bool CompleteReadmit(std::uint32_t guid, std::uint64_t kickTs);
    // An add whose outcome is unproved: this receipt never authorizes another.
    void ReadmitUnknown(std::uint32_t guid, std::uint64_t kickTs);

    void NoteFailed(std::uint32_t guid, std::uint64_t nowMs);
    std::vector<GuardStep> Pass(GuardInput const& input, std::size_t maxReadds = 5);
    std::map<std::uint32_t, std::uint8_t> const& Known() const { return _known; }
    bool RecruitmentProtected(std::uint32_t guid) const
    { return _known.contains(guid) || _removedByHuman.contains(guid); }

private:
    bool Protectable(std::uint32_t guid) const;
    void PruneIntents(std::uint64_t nowMs);

    std::uint32_t _guildId;
    std::uint32_t _humanLow;
    std::map<std::uint32_t, std::uint8_t> _known;
    std::set<std::uint32_t> _removedByHuman;
    std::map<std::uint32_t, std::uint8_t> _pendingKicks;
    std::map<std::uint32_t, std::uint64_t> _commandIntents;
    std::map<std::uint32_t, std::uint64_t> _attemptedAt;
    std::map<std::uint32_t, std::uint32_t> _blockedGuild;
    std::map<std::uint32_t, KickReceipt> _receipts;
};

bool UninviteEventIsHis(bool isUninvite, std::uint32_t guildId, std::uint32_t kickerLow,
    std::uint32_t protectedGuild, std::uint32_t humanLow);
bool CommandIsHis(bool fromConsole, std::uint32_t invokerLow, std::uint32_t humanLow);

struct UninviteTarget
{
    std::optional<std::string> name;
};

// At most 256 bytes, two command tokens and at most one plain name (48 UTF-8 bytes).
// ASCII command matching is case-insensitive. No links, quotes, extra arguments,
// newlines, control bytes or command separators. Target resolution is the adapter's job.
std::optional<UninviteTarget> ParseGuildUninvite(std::string_view command);
}

#endif
