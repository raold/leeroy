// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_PLAY_STYLE_H
#define MOD_LEEROY_PLAY_STYLE_H
#include <algorithm>
#include <cstdint>
#include <optional>
#include <string_view>

namespace Leeroy::PlayStyle
{
struct Request { std::uint8_t wait = 0; bool potions = false; };
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
}
#endif
