"""Hand-written 3.3.5 summaries that stand in for the wiki pages the library leaves out because
their text describes a later system (wiki.LATER_PAGES): the Darkmoon Faire, the Dungeon Finder
and heirlooms. Each states only what this server's world database or 3.3.5 client data shows:
game events and their NPCs, quest titles, rewards and money, vendor prices, item and spell
texts. At load the source checks that the items and quests a summary cites are still in the
item and quest tables the world source wrote, and logs any that are not."""
from leeroy_mind.library.chunks import ChunkDraft, SourceResult

SECTION = "3.3.5 summary"

_FAIRE = (
    "The Darkmoon Faire travels between Elwynn Forest, Mulgore and Terokkar Forest. Each visit "
    "is a world event of about a week, after a three-day building event that sets the faire up. "
    "Sayge's fortunes give a Sayge's Dark Fortune buff (Strength, Agility, Stamina, Spirit, "
    "Intelligence, Armor, Damage or Resistance); Selina Dourman gives information, Stamp "
    "Thunderhorn and Sylannia sell food and drink, Lhara sells exotic goods and Maxima "
    "Blastenheimer runs the cannon.\n"
    "Darkmoon Faire Prize Tickets come from turn-in quests: Yebb Neblegear takes skins and "
    "monster parts, Kerri Hicks smithing goods, Rinling engineering parts and Chronos "
    "leatherwork, from level 1, 10, 20, 30 and 40 for 1, 4, 8, 12 and 20 tickets. The barkers "
    "Melnan Darkstone (Elwynn Forest) and Kruban Darkblade (Mulgore) offer The Darkmoon Faire, "
    "which turns a Free Ticket Voucher into 5. Gelvas Grimegate redeems tickets: Darkmoon "
    "Flower or Minor Darkmoon Prize for 5, Last Month's Mutton for 10, Lesser Darkmoon Prize for "
    "12, Greater Darkmoon Prize or Schematic: Steam Tonk Controller for 40, Darkmoon Storage Box "
    "or Last Year's Mutton for 50, Orb of the Darkmoon or Amulet of the Darkmoon for 1,200.\n"
    "Professor Thaddeus Paleo trades a full deck for its Darkmoon Card: Beasts (Blue Dragon), "
    "Portals (Twisting Nether), Warlords (Heroism), Elementals (Maelstrom), Blessings (Crusade), "
    "Storms (Wrath), Furies (Vengeance), Lunacy (Madness), Prisms (Illusion), Chaos (Berserker!), "
    "Nobles (Greatness) and Undeath (Death). Scribes make Darkmoon Card, Greater Darkmoon Card "
    "and Darkmoon Card of the North, each a random card toward a set.")

_FINDER = (
    "The Dungeon Finder queues a player or a group for a random dungeon by role (tank, healer, "
    "damage) and teleports the group in. Its random queues: Random Classic Dungeon (levels "
    "15-58), Random Burning Crusade Dungeon (59-68), Random Burning Crusade Heroic (70), Random "
    "Lich King Dungeon (69-80) and Random Lich King Heroic (80), and in season the holiday "
    "bosses The Headless Horseman, The Frost Lord Ahune, Coren Direbrew and The Crown Chemical "
    "Co.\n"
    "The first random heroic of the day pays 2 Emblems of Frost and 22 gold 20 silver, later "
    "ones 2 Emblems of Triumph and 7 gold 40 silver. The first random normal dungeon pays 2 "
    "Emblems of Triumph and 14 gold 80 silver, later ones 7 gold 40 silver. Random Classic and "
    "Burning Crusade dungeons pay money and a Satchel of Helpful Goods (the Burning Crusade "
    "heroic only money). A holiday boss gives its box once a day: the Loot-Filled Pumpkin, the "
    "Keg-Shaped Treasure Chest, the Satchel of Chilled Goods or the Heart-Shaped Box.\n"
    "A group the tool puts together gets Luck of the Draw: 5% more damage, healing and health. "
    "Abandoning a dungeon brings Dungeon Deserter, and a random queue brings Dungeon Cooldown; "
    "both make a player wait before using the Dungeon Finder again.")

_HEIRLOOMS = (
    "Heirlooms are heirloom-quality items, bound to the account, whose stats scale with the "
    "wearer's level up to 80. The 13 shoulders and 6 chest pieces each add 10% to experience "
    "from kills and quests (Heirloom Experience Bonus), and the Dread Pirate Ring, the reward "
    "of the Kalu'ak Fishing Derby, adds 5%. Swift Hand of Justice heals 2% of maximum health and "
    "Discerning Eye of the Beast restores 2% of maximum mana on each kill that yields experience "
    "or honor; the Inherited Insignia of the Alliance and of the Horde remove movement-impairing "
    "and loss-of-control effects.\n"
    "Emblems of Heroism buy them from Brammold Deepmine, Magistrix Lambriesse and the heirloom "
    "vendors Enchanter Erodin and Enchanter Isian: 40 for shoulders, chests and one-handed "
    "weapons, 50 for the trinkets and the Devout Aurastone Hammer, 65 for two-handed weapons and "
    "the bow; only the two enchanters sell the chests. Dame Evniki Kapsalis, the Crusader's "
    "Quartermaster, sells the enchanters' range for Champion's Seals (60, 75 or 95). The "
    "Wintergrasp Quartermasters, Knight Dameron and Stone Guard Mukar, sell a set of their own "
    "for Stone Keeper's Shards (200 to 325): seven shoulders, six weapons and their faction's "
    "Inherited Insignia.")

_FAIRE_REFS = (
    "item:19182", "item:19338", "item:19295", "item:19298", "item:19297", "item:19296",
    "item:22729", "item:19291", "item:19292", "item:19293", "item:19426", "item:19491",
    "item:44316", "item:44317", "item:44318",
    "quest:7905", "quest:7926", "quest:7881", "quest:7889", "quest:7894", "quest:7899",
    "quest:7907", "quest:7927", "quest:7928", "quest:7929", "quest:10938", "quest:10939",
    "quest:10940", "quest:10941", "quest:13324", "quest:13325", "quest:13326", "quest:13327",
    "npc:14822", "npc:10445", "npc:14845", "npc:14844", "npc:14846", "npc:15303", "npc:14829",
    "npc:14832", "npc:14841", "npc:14833", "npc:14842", "npc:14843", "npc:14828", "npc:14847")
_FINDER_REFS = (
    "quest:24788", "quest:24789", "quest:24790", "quest:24791", "quest:24881", "quest:24922",
    "quest:25482", "quest:25483", "quest:25484", "quest:25485",
    "item:49426", "item:47241", "item:51999", "item:54516", "item:54535", "item:54536",
    "item:54537")
_HEIRLOOM_REFS = (
    "item:50255", "item:42991", "item:42992", "item:44097", "item:44098", "item:42948",
    "item:40752", "item:44990", "item:43228", "quest:24803",
    "npc:32509", "npc:31302", "npc:31582", "npc:35508", "npc:35507", "npc:34885", "npc:32294",
    "npc:32296")

SUMMARIES = (
    ChunkDraft("curated:darkmoon-faire", "event", "Darkmoon Faire", _FAIRE, section=SECTION,
               ref_ids=_FAIRE_REFS),
    ChunkDraft("curated:dungeon-finder", "event", "Dungeon Finder", _FINDER, section=SECTION,
               ref_ids=_FINDER_REFS),
    ChunkDraft("curated:heirloom", "item", "Heirloom", _HEIRLOOMS, section=SECTION,
               ref_ids=_HEIRLOOM_REFS),
)


class CuratedSource:
    name = "curated"

    def load(self, library=None) -> SourceResult:
        res = SourceResult(self.name, chunks=list(SUMMARIES))
        cited = sorted({r for c in SUMMARIES for r in c.ref_ids
                        if r.startswith(("item:", "quest:"))})
        known = library.known_refs(cited) if library is not None else set()
        if not known:
            res.log.append("cited items and quests not checked: no world tables to check against")
        else:
            res.log += [f"cited but not in the world tables: {r}" for r in cited if r not in known]
            res.log.append(f"{len(known)} of {len(cited)} cited items and quests are in the "
                           "world tables")
        res.log.append(f"{len(SUMMARIES)} curated summaries")
        return res
