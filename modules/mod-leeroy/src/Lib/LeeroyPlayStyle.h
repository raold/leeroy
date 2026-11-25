// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_PLAY_STYLE_H
#define MOD_LEEROY_PLAY_STYLE_H
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <map>
#include <optional>
#include <string_view>
#include <utility>
#include <vector>

namespace Leeroy::PlayStyle
{
struct Request
{
    std::uint8_t wait = 0;
    bool potions = false;
    bool operator==(Request const&) const = default;
};
inline std::optional<Request> Parse(std::string_view text)
{
    if (text.size() != 3 || text[1] != ':' || (text[2] != '0' && text[2] != '1')
        || (text[0] != '0' && (text[0] < '2' || text[0] > '6'))) return {};
    return Request{static_cast<std::uint8_t>(text[0] - '0'), text[2] == '1'};
}
struct Values
{
    bool wait = false;
    std::uint8_t seconds = 10;
    bool potions = false;
    bool operator==(Values const&) const = default;
};
inline Values Plan(Values current, Request request, bool damage)
{
    if (damage && request.wait >= 2 && request.wait <= 6 && (!current.wait || current.seconds <= 6))
    {
        // The stock value is 10 even while its strategy is absent: then effective delay is zero.
        current.seconds = current.wait ? std::max(current.seconds, request.wait) : request.wait;
        current.wait = true;
    }
    current.potions = current.potions || request.potions; // Never reduce stock consumable use.
    return current;
}
struct Facts
{
    bool enabled = false, owned = false, alive = false, quiet = false;
    bool instance = false, raid = false, foreign = false, pvp = false, busy = false;
};
inline std::string_view Refusal(Facts const& f)
{
    if (!f.enabled) return "playstyle_off";
    if (!f.owned) return "not_cast";
    if (!f.alive || !f.quiet) return "busy:body";
    if (f.instance || f.raid) return "busy:raid_owner";
    if (f.foreign) return "busy:controller";
    if (f.pvp) return "busy:pvp";
    if (f.busy) return "busy:owner";
    return {};
}
inline bool CleanupDue(Facts const& f, bool lifeChanged, bool roleChanged)
{
    // Combat alone is the use case; a PvP/instance/raid transition ends ownership even in combat.
    return lifeChanged || roleChanged || !f.enabled || !f.owned || f.instance || f.raid
        || f.foreign || f.busy || f.pvp;
}
struct Life { std::uintptr_t player = 0, ai = 0, session = 0; };
inline bool SameLife(Life const& a, Life const& b)
{
    return a.player && a.ai && a.session && a.player == b.player && a.ai == b.ai && a.session == b.session;
}
inline unsigned Mask(std::string_view name)
{
    if (name == "*" || name == "playstyle") return 7;
    if (name == "wait for attack") return 1;
    if (name == "wait for attack time") return 2;
    if (name == "potions") return 4;
    return 0;
}
class Journal
{
public:
    void Note(Values before, Values after)
    {
        baseline = before; applied = after;
        owned = (before.wait != after.wait ? 1u : 0u) | (before.seconds != after.seconds ? 2u : 0u)
            | (before.potions != after.potions ? 4u : 0u);
    }
    bool Owns(std::string_view name = "*") const { return (owned & Mask(name)) != 0; }
    bool OwnsOpeningHold() const { return (owned & 3u) != 0; }
    Values Restore(Values current, std::string_view name) const
    {
        unsigned mask = owned & Mask(name);
        // A later caller's replacement wins. Restore only values still equal to our write.
        bool const delayReplaced = (owned & 2) && current.seconds != applied.seconds;
        if ((mask & 1) && current.wait == applied.wait && !delayReplaced) current.wait = baseline.wait;
        if ((mask & 2) && current.seconds == applied.seconds) current.seconds = baseline.seconds;
        if ((mask & 4) && current.potions == applied.potions) current.potions = baseline.potions;
        return current;
    }
    void Restored(std::string_view name) { owned &= ~Mask(name); }
private:
    Values baseline{}, applied{};
    unsigned owned = 0;
};

// The play-style table (playstyle_table): the connected mind's whole desired style a cast member, held for that
// connection only. Native carries each entry out itself whenever the member is free and he is in game outside a raid
// group, and again once a cleanup has taken it back (a battleground, an instance, a role change, another owner). The
// mind used to send one bot_do a member at his login and at a few other events, and a member busy then (in a
// battleground, dead, held by the Realm) kept no style for the rest of his session.
inline constexpr std::size_t kTableMax = 512;         // the mind's own receipt cap (playstyle/service.py LIMIT)
inline constexpr std::int64_t kTableRetryMs = 15000;  // a tried entry is checked again after this, whatever came of it
inline constexpr std::size_t kTableTriesPerPass = 16; // bodies tried a pass (the cleanup tick, once a second)
struct TableEntry
{
    std::uint32_t guid = 0;
    Request request{};
};
struct Slot
{
    Request request{};
    std::int64_t dueMs = 0;
};
using Table = std::map<std::uint32_t, Slot>;

// A new table replaces the last one whole: an entry it repeats unchanged keeps its retry time, a new or changed
// entry is due at once, and a member it leaves out is no longer the table's (TableUndo).
inline Table ReplaceTable(Table const& held, std::vector<TableEntry> const& entries, std::int64_t nowMs)
{
    Table next;
    for (auto const& entry : entries)
    {
        auto const old = held.find(entry.guid);
        next[entry.guid] = (old != held.end() && old->second.request == entry.request)
            ? old->second : Slot{entry.request, nowMs};
    }
    return next;
}

// The table adds styles only with both switches on, a table held, and him in game outside a raid group (his raid is
// RaidHabits' to set, as the mind's own requests left it).
struct TableGate
{
    bool enabled = false, table = false, held = false, human = false, humanRaid = false;
};
inline std::string_view TableRefusal(TableGate const& g)
{
    if (!g.enabled) return "playstyle_off";
    if (!g.table) return "playstyle_table_off";
    if (!g.held) return "no_table";
    if (!g.human) return "human_offline";
    if (g.humanRaid) return "human_raid";
    return {};
}

// Whose style journal a member carries: nobody's, the table's (with the request it carried out), or another writer's
// (an explicit bot_do playstyle), which that writer keeps or undoes and the table leaves alone.
enum class Holder : std::uint8_t { None, Table, Other };
struct Held
{
    Holder holder = Holder::None;
    Request request{};
};
inline bool TableDue(Slot const& slot, Held const& held, std::int64_t nowMs)
{
    if (held.holder == Holder::Other || (held.holder == Holder::Table && held.request == slot.request))
        return false;
    return nowMs >= slot.dueMs;
}
// A journal the table made is undone once the table no longer lists its member: a new table without it, or no table
// at all (its mind disconnected, or a switch went 0).
inline bool TableUndo(Holder holder, bool listed) { return holder == Holder::Table && !listed; }

// This pass's tries, at most `budget`: the due entries that have waited longest first (then by guid). Each tried
// entry then waits kTableRetryMs (TableTried), so however many entries stay due (members offline, in a battleground)
// every one is reached in turn and none is starved by the lower guids.
template <typename Lookup>
std::vector<std::uint32_t> TableTries(Table const& table, Lookup const& heldOf, std::int64_t nowMs,
    std::size_t budget = kTableTriesPerPass)
{
    std::vector<std::pair<std::int64_t, std::uint32_t>> due;
    for (auto const& [guid, slot] : table)
        if (TableDue(slot, heldOf(guid), nowMs))
            due.emplace_back(slot.dueMs, guid);
    auto const take = std::min(budget, due.size());
    std::partial_sort(due.begin(), due.begin() + static_cast<std::ptrdiff_t>(take), due.end());
    std::vector<std::uint32_t> tries;
    for (std::size_t i = 0; i < take; ++i)
        tries.push_back(due[i].second);
    return tries;
}
inline void TableTried(Slot& slot, std::int64_t nowMs) { slot.dueMs = nowMs + kTableRetryMs; }
}
#endif
