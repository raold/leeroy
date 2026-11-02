// This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.
#ifndef MOD_LEEROY_BOT_COMMAND_POLICY_H
#define MOD_LEEROY_BOT_COMMAND_POLICY_H

#include <array>
#include <cstdint>
#include <optional>
#include <string_view>
#include <vector>

namespace Leeroy::Guild
{
inline constexpr std::array<std::uint32_t, 4> kBotFlasks = {46377, 46376, 46379, 46378};

inline bool IsBotFlask(std::uint32_t item)
{
    for (auto flask : kBotFlasks)
        if (flask == item)
            return true;
    return false;
}

struct BotFlask
{
    std::uint32_t item = 0;
    bool carried = false, active = false;
};
enum class FlaskChoice { Missing, Ambiguous, AlreadyActive, Use };
struct BotFlaskChoice
{
    FlaskChoice choice = FlaskChoice::Missing;
    std::uint32_t item = 0;
};

inline BotFlaskChoice ChooseBotFlask(std::vector<BotFlask> const& flasks, std::uint32_t preferred)
{
    BotFlask const* selected = nullptr;
    // The last consumed flask can leave its aura active with no item left in the bags. A matching
    // preference, or one unambiguous approved aura without a preference, already satisfies the request.
    for (auto const& flask : flasks)
    {
        if (!IsBotFlask(flask.item) || !flask.active || (preferred && flask.item != preferred))
            continue;
        if (selected)
            return {FlaskChoice::Ambiguous, 0};
        selected = &flask;
    }
    if (selected)
        return {FlaskChoice::AlreadyActive, selected->item};
    for (auto const& flask : flasks)
    {
        if (!IsBotFlask(flask.item) || !flask.carried)
            continue;
        if (preferred)
        {
            if (flask.item == preferred)
            {
                selected = &flask;
                break;
            }
        }
        else if (selected)
            return {FlaskChoice::Ambiguous, 0};
        else
            selected = &flask;
    }
    // A depleted known preference must not silently switch to another role's leftover flask.
    if (!selected)
        return {};
    return {FlaskChoice::Use, selected->item};
}

// Group buffs must work while the shaman's combat selection is an enemy. An explicit target is never replaced.
inline bool BotSpellDefaultsToSelf(std::string_view spell, std::uint32_t target)
{
    return !target && (spell == "bloodlust" || spell == "heroism");
}

inline std::uint32_t StockMissing(std::uint32_t have, std::uint32_t wanted)
{
    return have < wanted ? wanted - have : 0;
}

// CanStoreNewItem may return an inventory-full error together with a useful partial destination vector.
// Validate its accounting before passing that vector to StoreNewItem, which uses the vector's counts.
inline std::optional<std::uint32_t> StockPlanCount(std::uint32_t missing, std::uint32_t noSpace,
    std::uint64_t planned, bool ok, bool inventoryFull)
{
    if ((!ok && !inventoryFull) || noSpace > missing || planned > missing
        || planned != missing - noSpace || (ok && noSpace) || (inventoryFull && !noSpace))
        return std::nullopt;
    return static_cast<std::uint32_t>(planned);
}
}

#endif
