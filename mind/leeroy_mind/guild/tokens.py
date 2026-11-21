"""Tier tokens (3.3.5a) and the tier pieces they buy, so the guild's bids can value a token.

A token is no gear itself (item level 80, no slot), so it never read as an upgrade and every one
went unclaimed. Each token here buys its classes' tier piece of one item level, for one slot (the
Naxxramas and Ulduar tokens) or for any of the five tier slots (the Crusade's trophy and regalia,
the Marks of Sanctification: each buys or upgrades the piece of the slot the member chooses).

Derived on 2026-11-21 from the game's own data, read-only: the ItemExtendedCost.dbc rows that
require each token, the world database's npc_vendor items sold for those costs and their
item_template item level and inventory type. The classes are the token's own AllowableClass."""

HEAD, SHOULDERS, CHEST, LEGS, HANDS = 1, 3, 5, 7, 10  # item_template InventoryType
TIER_SLOTS = (HEAD, SHOULDERS, CHEST, LEGS, HANDS)


def _three(first, item_level, slots):
    """A Conqueror's, a Protector's and a Vanquisher's token: three ids in a row."""
    return {first + offset: (item_level, slots) for offset in range(3)}


# token item id -> (the item level of the tier piece it buys, the InventoryTypes it buys for)
TIER_TOKENS = {
    # Naxxramas (tier 7): 10-player tokens buy 200, 25-player tokens 213
    **_three(40610, 200, (CHEST,)), **_three(40613, 200, (HANDS,)), **_three(40616, 200, (HEAD,)),
    **_three(40619, 200, (LEGS,)), **_three(40622, 200, (SHOULDERS,)),
    **_three(40625, 213, (CHEST,)), **_three(40628, 213, (HANDS,)), **_three(40631, 213, (HEAD,)),
    **_three(40634, 213, (LEGS,)), **_three(40637, 213, (SHOULDERS,)),
    # Ulduar (tier 8): 10-player tokens buy 219, 25-player tokens 226
    **_three(45632, 226, (CHEST,)), **_three(45635, 219, (CHEST,)), **_three(45638, 226, (HEAD,)),
    **_three(45641, 226, (HANDS,)), **_three(45644, 219, (HANDS,)), **_three(45647, 219, (HEAD,)),
    **_three(45650, 219, (LEGS,)), **_three(45653, 226, (LEGS,)), **_three(45656, 226, (SHOULDERS,)),
    **_three(45659, 219, (SHOULDERS,)),
    # Trial of the Crusade (tier 9): the Trophy of the Crusade (every class) 245, the regalia 258
    47242: (245, TIER_SLOTS), **_three(47557, 258, TIER_SLOTS),
    # Icecrown Citadel (tier 10): Marks of Sanctification 264, their heroic marks 277
    **_three(52025, 264, TIER_SLOTS), **_three(52028, 277, TIER_SLOTS),
}


def tier_target(item_id):
    """(item level, InventoryTypes) of the tier piece the token `item_id` buys; None for others."""
    return TIER_TOKENS.get(item_id)
