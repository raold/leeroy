"""The talk engine's reactions to game events: greetings at the human's login, gz for
achievements and his loot, the kill celebration, the wipe debrief, the cancel when a fight
starts, unprompted whispers, and the follow-ups after a line is said (the men's jealousy
asides, a rival's flirt, a bit's bookkeeping). Every reaction plans its turns and spawns them;
nothing here waits, because the bridge hands events over one at a time. The public names are
imported from leeroy_mind.talk.engine.

His login (final fix wave, from the rehearsal's seven hellos in two minutes): 2-3 greetings,
then for LOGIN_QUIET_S only answers to him (his own hello gets what is left of HELLOS); no
social bursts, member gz, asides, rival flirts or unprompted whispers. The lead's call and the
reactions after a pull are given the facts (the boss, the pull's length, which wipe it is
tonight, who died first) and told not to make mechanics up. With the library (fix wave part
E), the lead's wipe debrief and kill line also get the boss's real mechanics from its tactics
page (facts.boss_mechanics) and are told to talk about both and invent nothing."""
import math
import random

from leeroy_mind.bridge import BridgeError
from leeroy_mind.clock import night_of
from leeroy_mind.discord.channels import is_discord, is_private
from leeroy_mind.personas.schema import Persona
from leeroy_mind.protocol import (Achievement, BgEnd, Boss, Death, Emote, Group, Login,
                                  LootDrop, LootWon, Logout, Resurrect, Zone)
from leeroy_mind.talk.prompt import open_topics
from leeroy_mind.talk.bits import BitScheduler
from leeroy_mind.talk.budget import AFTER_BURST_S
from leeroy_mind.talk.channels import choose_channel
from leeroy_mind.talk.filters import names_any
from leeroy_mind.talk.pipeline import Pipeline, TurnMeta, log, picked_why
from leeroy_mind.talk.raid import MAX_FIGHT_S, WIPE_SILENCE_S, RaidPhase
from leeroy_mind.talk.romance import (START, JealousyAsides, WhisperPlanner, forward,
                                      rivalry_reply, rivalry_text, romance_lift, whisper_kind)
from leeroy_mind.talk.seasons import holiday_topics
from leeroy_mind.talk.select import pick_speakers
from leeroy_mind.talk.states import substance_of
from leeroy_mind.talk.turns import Turn, TurnKind
from leeroy_mind.wow import BOSS_NAMES, RAID_INSTANCES, boss_name, is_raid_fight

GZ_TEMPLATES = ("gz", "grats", "gratz", "congrats", "gz!", "nice")
# A boss the guild kills every week gets these, not a "congrats" (prompts lens, minor 5).
KILL_CHEERS = ("gg", "gg!", "nice", "nice one", "ez")
KILL_EMOTES = ("cheer", "dance")
GREETERS = (2, 3)
LOGIN_QUIET_S = 180.0  # after his login, only answers to him
HELLOS = 3  # greetings plus answers to his own hello in that time, at most
# The wipe debrief and the kill call say only what their facts give (rehearsal 2, finding 6:
# "the spike free on pull 1 was slow" in a kill call that knew the time and nobody's death).
DEBRIEF = "the plan for the next pull on {boss}"
# The WIPE_REACT template offers "frustration, a joke or what went wrong": these settle it.
NO_JOKE = "Leave out the joke this time: only frustration or what went wrong."
JOKE_OK = "That's {n} wipes on it tonight: one joke is fine if it's a good one."
FACTS_RULE = "Go by what happened: no causes, no blame, and no mechanics you weren't given."
FACTS_LOGGED = "Go by what happened and the combat log that follows: blame nobody, and no mechanics you weren't given."
# The lead's wipe call with the boss's tactics page (fix wave part E); "the Northrend Beasts'".
MECHANICS = "{whose} real mechanics:"
WIPE_KNOWN = ("You know the time and who died first, not why: give the plan for the next pull "
              "from these mechanics.")
WIPE_LOGGED = "Give the plan for the next pull from these mechanics and the combat log that follows."
MECHANICS_RULE = "Never invent anything else, and blame nobody."
KILL_RULE = "Say only what these facts give: no causes, no blame."
# After a wipe the first reaction is offered to whoever died first (rehearsal 2, judge A 9).
DIED_FIRST = "You died first on that pull."
# He says goodnight from a formed raid, or a group he leads (judge A 9: nobody asked "we
# calling it?").
MID_RAID = "{name} is leaving mid-raid, with the raid still formed."
LEAD_CALL = ("{name} said goodnight and is leaving mid-raid. You lead the raid: say what "
             "happens now, calling it or carrying on without {them}.")
MID_GROUP = "{name} is leaving the group."
LEAD_GROUP_CALL = ("{name} said goodnight and is leaving the group. You lead it now: say what "
                   "happens next, calling it or carrying on without {them}.")
# His loot: one cheer is in a member's own words and names the item (judge A 9).
NAME_ITEM = "Say which item it is: {item}."
LATELY_S = 3 * 3600.0  # what a member did this long ago still counts as tonight
LATELY_ITEMS = 3
HOLIDAY_FIRST_P = 0.4  # on a holiday, this share of the members open the night with it
FIGHT_PHASES = frozenset({RaidPhase.COMBAT, RaidPhase.WIPE_SILENCE, RaidPhase.WIPE_DEBRIEF})
# A woman's attitude makes her likelier to cheer his loot: lust above drawn.
LOOT_CHEER = {"lust": 3.0, "drawn": 2.0}
WHISPER_WHY = {"after_kill": "The raid just killed {about}.",
               "idle": "Chat has been quiet for a while.", "late": "It's getting late.",
               "evening": "{about}", START: ""}
# Every unprompted whisper says it starts the conversation, and what to bring up by what the
# member feels for him (romance.whisper_kind), so the model has something to say (2026-11-07:
# 5 of 8 came back "(nothing)" with only the reason above to go on).
START_WHISPER = "You're the one starting this conversation with {them}."
START_WHAT = {
    "romantic": "Tell {them} what's on your mind about {them}, or bring up {topic}.",
    "casual": "Something casual: bring up {topic}, or ask how {their} night is going.",
}
# A whisper either way between him and a member this recent is a conversation going on: she is
# not the one to start one (controller ruling, 2026-11-07).
WHISPER_THREAD_S = 600.0
EVENING_S = (1800.0, 2700.0)  # the evening whisper, 30-45 minutes into his session
# Seconds: greetings, gz, the kill, the debrief, emotes, asides and whispers.
GREET_FIRST_S, GREET_NEXT_S = (5.0, 12.0), (5.0, 14.0)
GZ_FIRST_S, GZ_NEXT_S = (1.5, 4.0), (0.5, 3.0)
KILL_S = (1.5, 4.0)
DEBRIEF_S = (0.5, 2.5)  # after the wipe silence
REACT_S = (2.0, 6.0)
EMOTE_S = (2.0, 15.0)
ASIDE_S = (3.0, 10.0)
RIVAL_S = (4.0, 12.0)
WHISPER_S = {"after_kill": (20.0, 45.0), "idle": (2.0, 10.0), "late": (2.0, 10.0),
             "evening": (20.0, 60.0), START: (5.0, 20.0)}


class Reactions(Pipeline):
    """Event reactions on top of the line pipeline. TalkEngine.__init__ sets the collaborators
    declared here and calls _init_reactions for the bookkeeping this layer owns."""

    asides: JealousyAsides
    whispers: WhisperPlanner
    bits: BitScheduler

    def _init_reactions(self) -> None:
        self._greeted_login = False  # until his logout (or the session's end)
        self._greeters: set[int] = set()  # who greeted his last login
        self._hellos = 0  # hellos planned since it (greetings, answers to his hello)
        self._fight_deaths: list[int] = []  # the group's deaths in the current pull, in order
        self._lately: dict[int, dict[str, tuple[float, str]]] = {}  # guid -> what -> (at, text)
        self._opens: dict[tuple[int, str], int] = {}  # (guid, night) -> openers so far
        self._gz_seen: set[tuple[int, int]] = set()  # (guid, achievement) congratulated
        self._gz_waiting: dict[tuple[int, int], Achievement] = {}  # held through a fight
        self._kill_at = self._wipe_at = self._loot_at = -math.inf
        self._raid_fought = False
        # the evening whisper trigger: when it is due this session, and the night it fired
        self._evening_at, self._evening_done, self._evening_night = math.inf, False, ""
        # boss fights he is in outside his raid's (a 5-man's): (map, instance, boss) -> start
        self._group_fights: dict[tuple[int, int, int], float] = {}

    def set_human(self, guid: int) -> None:
        super().set_human(guid)
        self.asides.set_human(self.human)  # the asides name him; their hourly count stays

    async def on_event(self, ev, now: float) -> None:
        """React to a game event the app has already applied to the world and the raid state.
        Chat, emotes and anything unknown trigger nothing here (the human's lines go to
        on_human_chat; emotes at members are answered by talk/emotes.py)."""
        self._note_lately(ev, now)
        if isinstance(ev, (LootDrop, LootWon)) and getattr(self, "items", None) is not None:
            # the copy that dropped is the one a shared item name links to (talk/items.py)
            self.items.seen([i.item_id for i in ev.items] if isinstance(ev, LootDrop)
                            else [ev.item_id], now)
        if isinstance(ev, Login):
            self._dead.pop(ev.guid, None)
            if ev.human:
                if ev.guid != self.human.guid:
                    self.set_human(ev.guid)
                if not self._greeted_login:  # a replayed login greets once, session or not
                    self._greeted_login = True
                    self._greet(now)
        elif isinstance(ev, Logout):
            self._dead.pop(ev.guid, None)
            if ev.human:
                self._greeted_login = False
        elif isinstance(ev, Achievement):
            self._gz_achievement(ev, now)
        elif isinstance(ev, Boss):
            self._note_fight(ev, now)
            self._on_boss(ev, now)
        elif isinstance(ev, LootWon):
            self._gz_loot(ev, now)
        elif isinstance(ev, LootDrop):
            self._loot_at = now
        elif isinstance(ev, Death):
            self._dead[ev.guid] = now
            if self.raid.phase(now) is RaidPhase.COMBAT and self.world.in_group(ev.guid):
                self._fight_deaths.append(ev.guid)
        elif isinstance(ev, Resurrect):
            self._dead.pop(ev.guid, None)
        elif isinstance(ev, Group) and (self.world.group is None or not self.world.group.raid):
            self._raid_fought = False

    def _template(self) -> str:
        return self.rng.choice(GZ_TEMPLATES)

    # -- tonight so far, and what openers bring up -----------------------------------------

    def _note_lately(self, ev, now: float) -> None:
        """A member's own events tonight (spec I3 ruling): where they were, what they won and
        earned, the bosses they helped kill and their battlegrounds, the latest of each."""
        hg = self.human.guid
        if isinstance(ev, Zone) and ev.zone_name.strip():
            notes = [(ev.guid, "zone", f"you were in {ev.zone_name.strip()}")]
        elif isinstance(ev, LootWon) and ev.name.strip():
            notes = [(ev.guid, "loot", f"you won {ev.name.strip()}")]
        elif isinstance(ev, Achievement) and ev.name.strip():
            notes = [(ev.guid, "achievement", f"you earned {ev.name.strip()}")]
        elif isinstance(ev, Boss) and ev.state == "killed" and is_raid_fight(ev.map, ev.boss_id):
            name = boss_name(ev.map, ev.boss_id, ev.boss_name)
            notes = [(g, "kill", f"you helped kill {name}") for g in ev.members]
        elif isinstance(ev, BgEnd):
            notes = [(g, "bg", "you played a battleground") for g in ev.members]
        else:
            return
        for g, what, text in notes:
            if g and g != hg:
                self._lately.setdefault(g, {})[what] = (now, text)

    def _lately_line(self, guid: int, now: float) -> str:
        recent = sorted(((at, text) for at, text in self._lately.get(guid, {}).values()
                         if now - at < LATELY_S), reverse=True)
        return ", ".join(text for _, text in recent[:LATELY_ITEMS])

    def _open_topic(self, p: Persona, now: float) -> str:
        """The next thing this member's opener brings up tonight: their own material
        (prompt.open_topics) and tonight's holidays that fit them (seasons.holiday_topics), in
        an order fixed per member and night, so openers rotate through it instead of circling
        one thing. On a holiday a share of the members (HOLIDAY_FIRST_P) open with it."""
        local = self.clock.local()
        night = night_of(local)
        n = self._opens.get((p.guid, night), 0)
        self._opens[(p.guid, night)] = n + 1
        options = open_topics(p, self._lately_line(p.guid, now),
                              substance=substance_of(p, self._altered_of(p.guid), local))
        holidays = holiday_topics(local, p.identity.region)
        seeded = random.Random(f"{p.guid}:{night}:open")
        options += holidays
        seeded.shuffle(options)
        if holidays and seeded.random() < HOLIDAY_FIRST_P:
            options.remove(holidays[0])
            options.insert(0, holidays[0])
        return options[n % len(options)]

    def _login_quiet(self, now: float) -> bool:
        """Inside LOGIN_QUIET_S of his greeted login: only answers to him."""
        return now - self._login_at < LOGIN_QUIET_S

    def _cap_hellos(self, planned: list, called: list[int]) -> list:
        """His own hello inside the login quiet: the members he named answer, and others only
        up to what is left of HELLOS, never one who has greeted him already."""
        left = HELLOS - self._hellos
        out = []
        for rt in planned:
            if rt.speaker in called:
                out.append(rt)
            elif left > 0 and rt.speaker not in self._greeters:
                out.append(rt)
                left -= 1
        self._hellos += len(out)
        return out

    # -- the human logs in ---------------------------------------------------------------------

    def _greet(self, now: float) -> None:
        """2-3 greeters, those who know him first; the one who knows him best greets first and
        may follow up on something he told them, when her prompt holds a memory with him
        (TurnMeta.follow_up). The login quiet starts (LOGIN_QUIET_S)."""
        hg = self.human.guid
        self._login_at, self._greeters, self._hellos = now, set(), 0
        cands = [g for g in self._candidates() if self.raid.allows(g, TurnKind.GREET, now)]
        weights, pool = self._weigh(cands, last=hg, ctx=self._context(now), reply=True)
        picked = pick_speakers(weights, self.rng.randint(*GREETERS), self.rng, pool)
        if not picked:
            return
        known = {g: self.store.get_relationship(g, hg).familiarity for g in picked}
        best = max(picked, key=lambda g: known[g])
        items = []
        for g in [best] + [g for g in picked if g != best]:
            channel = choose_channel(TurnKind.GREET, speaker=g, world=self.world, raid=self.raid,
                                     now=now, rng=self.rng)
            if channel is None:
                continue
            delay = self.rng.uniform(*(GREET_NEXT_S if items else GREET_FIRST_S))
            items.append((Turn(kind=TurnKind.GREET, speaker=g, channel=channel, addressee=hg,
                               delay=delay),
                          TurnMeta(self._new_id(), answers_human=True, follow_up=g == best,
                                   why=picked_why("greet", g, pool))))
        self._greeters = {t.speaker for t, _ in items}
        self._hellos = len(items)
        self._queue(items)

    # -- gz ------------------------------------------------------------------------------------

    def _gz_achievement(self, ev: Achievement, now: float) -> None:
        """The human's achievement gets 2-4 gz, the first from someone who knows him and in
        their own words; a member's gets 1-3 template gz, only while he is online (liveops
        M9: nobody else reads guild chat). Once per achiever and achievement a session,
        counted when a gz is queued. One earned during a fight waits for its end
        (_gz_after_fight)."""
        key = (ev.guid, ev.achievement_id)
        if key in self._gz_seen:
            return
        hg = self.human.guid
        his = bool(hg) and ev.guid == hg
        if not his and ev.guid not in self.world.cast_guids():
            return
        if not his and (self._login_quiet(now) or not self.world.human_online):
            return  # a member's gz in the quiet or while he is away is skipped, not held
        if self.raid.phase(now) in FIGHT_PHASES:
            self._gz_waiting[key] = ev
            return
        n = self.rng.randint(2, 4) if his else self.rng.randint(1, 3)
        cands = [g for g in self._candidates(exclude=(ev.guid,))
                 if self.raid.allows(g, TurnKind.GZ, now)]
        weights, pool = self._weigh(cands, last=ev.guid, ctx=self._context(now), reply=his)
        reasons = pool if his else None
        items = []
        for g in pick_speakers(weights, n, self.rng, reasons):
            channel = choose_channel(TurnKind.GZ, speaker=g, world=self.world, raid=self.raid,
                                     now=now, rng=self.rng)
            if channel is None:
                continue
            delay = self.rng.uniform(*(GZ_NEXT_S if items else GZ_FIRST_S))
            why = picked_why("gz", g, reasons)
            if his and not items:
                turn = Turn(kind=TurnKind.GZ, speaker=g, channel=channel, addressee=hg,
                            delay=delay)
                meta = TurnMeta(self._new_id(), topic=ev.name or "an achievement", why=why)
            else:
                turn = Turn(kind=TurnKind.GZ, speaker=g, channel=channel,
                            template=self._template(), delay=delay)
                meta = TurnMeta(self._new_id(), why=why)
            items.append((turn, meta))
        if items:
            self._gz_seen.add(key)
            self._queue(items)

    def _gz_after_fight(self, now: float) -> None:
        """The achievements held through a fight get their gz once it is over (tick)."""
        if self._gz_waiting and self.raid.phase(now) not in FIGHT_PHASES:
            waiting, self._gz_waiting = self._gz_waiting, {}
            for ev in waiting.values():
                self._gz_achievement(ev, now)

    def _gz_loot(self, ev: LootWon, now: float) -> None:
        """1-2 gz for the human's loot in his group's chat, drawn and lust women first. Their
        cheer is in their own words (and feeds the men's asides), and so is the first one,
        whoever gives it, which names the item (NAME_ITEM); anyone else's is a template."""
        hg = self.human.guid
        if not hg or ev.guid != hg:
            return
        self._loot_at = now
        g = self.world.group
        if g is not None and self.world.in_group(hg):
            channel, audience = ("raid" if g.raid else "party"), g.members
        else:
            channel, audience = "guild", None
        cands = [x for x in self._candidates(audience) if self.raid.allows(x, TurnKind.GZ, now)]
        # the women who want him are not held back for having just spoken in raid
        # (rehearsal 2, judge C 6: the recency penalty cancelled their boost after a kill)
        women = {x for x in cands if self._cheer_stance(self.personas.get(x))}
        weights, _ = self._weigh(cands, last=hg, ctx=self._context(now), reply=False,
                                 fresh=women)
        for x in weights:
            weights[x] *= _cheer_weight(self.personas.get(x), self._cheer_stance)
        items = []
        why = "gz_loot:weighted_draw"
        for i, x in enumerate(pick_speakers(weights, self.rng.randint(1, 2), self.rng)):
            delay = self.rng.uniform(*(GZ_FIRST_S if i == 0 else GZ_NEXT_S))
            woman = bool(self._cheer_stance(self.personas.get(x)))
            if woman or i == 0:  # the first cheer is in their own words and names the item
                named = NAME_ITEM.format(item=ev.name.strip()) if i == 0 and ev.name.strip() else ""
                items.append((Turn(kind=TurnKind.GZ, speaker=x, channel=channel, addressee=hg,
                                   instruction=named, delay=delay),
                              TurnMeta(self._new_id(), topic=ev.name or "some loot",
                                       loot_cheer=woman, why=why)))
            else:
                items.append((Turn(kind=TurnKind.GZ, speaker=x, channel=channel,
                                   template=self._template(), delay=delay),
                              TurnMeta(self._new_id(), why=why)))
        self._queue(items)
        if ev.name.strip():
            self._evening_whisper(now, f"{self.human.called} just won {ev.name.strip()}.")

    def _evening_whisper(self, now: float, about: str) -> None:
        """The evening's whisper trigger, once a session: after his loot or EVENING_S into
        his session, whichever comes first (rehearsal 2, judge C 6: no woman whispered him
        first all evening). It takes the late hour's place that night: one draw, not two,
        and after a late login's draw there is no evening one (fix round 1, review minor 3)."""
        if self._evening_done:
            return
        self._evening_done = True
        if getattr(self, "_late_night", "") == night_of(self.clock.local()):
            return
        self._evening_night = night_of(self.clock.local())
        self._maybe_whisper("evening", now, about=about)

    @staticmethod
    def _cheer_stance(p: Persona) -> str:
        """"lust" or "drawn" for a woman who wants him, else ""."""
        if p.identity.player_gender == "female" and p.romance.stance in LOOT_CHEER:
            return p.romance.stance
        return ""

    # -- raid bosses ---------------------------------------------------------------------------

    def _on_boss(self, ev: Boss, now: float) -> None:
        """Only fights the raid state took as the human's raid's: it is in COMBAT, KILL or
        WIPE_SILENCE right after the event."""
        if not is_raid_fight(ev.map, ev.boss_id):
            return
        phase = self.raid.phase(now)
        if ev.state == "engaged":
            if phase is RaidPhase.COMBAT:
                self._raid_fought = True
                self._fight_deaths = []
                self._cancel_refused(now)
            return
        members = [g for g in self._candidates()
                   if self.world.in_group(g) and (not ev.members or g in ev.members)]
        if not members:
            return
        name = boss_name(ev.map, ev.boss_id, ev.boss_name)
        if ev.state == "killed" and phase is RaidPhase.KILL:
            self._kill(ev, name, members, now)
        elif ev.state == "wiped" and phase is RaidPhase.WIPE_SILENCE:
            self._wipe(ev, name, members, now)

    def _lead_among(self, members: list[int], now: float) -> int:
        lead = self.raid.lead()
        if lead in members:
            return lead
        weights, _ = self._weigh(members, last=None, ctx=self._context(now), reply=False)
        return (pick_speakers(weights, 1, self.rng) or members)[0]

    def _lead_why(self, cause: str, lead: int) -> str:
        return f"{cause}:{'lead' if lead == self.raid.lead() else 'weighted_draw'}"

    def _fight_facts(self, ev: Boss, name: str) -> str:
        """What happened on the pull that just ended, for the lead's call and the reactions:
        the boss, how long the pull lasted, which wipe it is tonight (or how many came before
        the kill) and who died first, then FACTS_RULE."""
        return f"{self._fight_story(ev, name)} {FACTS_RULE}"

    def _lead_facts(self, ev: Boss, name: str) -> str:
        """The lead's call. A kill: what happened (_fight_story: the time, the wipes before
        it, the deaths) and KILL_RULE, with no mechanics block (judge B 6: it invited "how a
        mechanic went"). A wipe: what happened, WIPE_KNOWN and the boss's real mechanics from
        the library (2-3 lines, looked up once a session), then MECHANICS_RULE; without a
        library, a known boss or its tactics page, _fight_facts."""
        if ev.state != "wiped":
            return f"{self._fight_story(ev, name)} {KILL_RULE}"
        known = bool((ev.boss_name or "").strip()) or (ev.map, ev.boss_id) in BOSS_NAMES
        lines = (self.facts.mechanics(name, RAID_INSTANCES.get(ev.map))
                 if self.facts is not None and known else [])
        if not lines:
            return self._fight_facts(ev, name)
        whose = name + ("'" if name.endswith("s") else "'s")
        return " ".join([self._fight_story(ev, name), WIPE_KNOWN, MECHANICS.format(whose=whose),
                         *lines, MECHANICS_RULE])

    def _fight_story(self, ev: Boss, name: str) -> str:
        fight = self.raid.last_fight(ev.map, ev.boss_id)
        secs = fight[1] if fight is not None else None
        took = _fight_time(secs) if secs is not None and secs >= 1 else ""
        wipes = self.raid.wipes_tonight(ev.map, ev.boss_id)
        first = (self._names().get(self._fight_deaths[0], "")
                 if self._fight_deaths else "")
        if ev.state == "wiped":
            text = f"The raid wiped on {name}" + (f" after {took}" if took else "")
            text += (", the first wipe on it tonight" if wipes <= 1
                     else f", wipe {wipes} on it tonight")
            text += f", and {first} died first." if first else "."
        else:
            text = f"The raid killed {name}" + (f" in {took}" if took else "")
            text += (", on the first pull tonight." if wipes == 0 else
                     f", after {wipes} wipe{'s' if wipes != 1 else ''} on it tonight.")
            text += f" {first} died first." if first else " Nobody died."
        return text

    def _kill(self, ev: Boss, name: str, members: list[int], now: float) -> None:
        """The lead's line first (the model's), then 2-3 template cheers in raid (KILL_CHEERS),
        and cheer or dance emotes from 2-4 members; maybe an unprompted whisper after it."""
        self._kill_at = now
        lead = self._lead_among(members, now)
        channel = choose_channel(TurnKind.KILL, speaker=lead, world=self.world, raid=self.raid,
                                 now=now, rng=self.rng) or "raid"
        items = [(Turn(kind=TurnKind.KILL, speaker=lead, channel=channel,
                       instruction=self._lead_facts(ev, name), delay=self.rng.uniform(*KILL_S)),
                  TurnMeta(self._new_id(), topic=name, why=self._lead_why("kill", lead),
                           fight_map=ev.map, fight_boss=ev.boss_id,
                           fight_instance=ev.instance_id, fight_end_ms=ev.ts,
                           again_tonight=self.raid.wipes_tonight(ev.map, ev.boss_id) >= 1))]
        others = [g for g in members if g != lead]
        weights, _ = self._weigh(others, last=lead, ctx=self._context(now), reply=False)
        for g in pick_speakers(weights, self.rng.randint(2, 3), self.rng):
            items.append((Turn(kind=TurnKind.GZ, speaker=g, channel=channel,
                               template=self.rng.choice(KILL_CHEERS),
                               delay=self.rng.uniform(*GZ_NEXT_S)),
                          TurnMeta(self._new_id(), why="kill_gz:weighted_draw")))
        self._queue(items)
        k = min(len(members), self.rng.randint(2, 4))
        self._spawn(self._emotes(self.rng.sample(members, k)))
        self._maybe_whisper("after_kill", now, about=name)

    async def _emotes(self, guids: list[int]) -> None:
        start = self.clock.now()
        plan = sorted((self.rng.uniform(*EMOTE_S), g, self.rng.choice(KILL_EMOTES))
                      for g in guids)
        for at, g, emote in plan:
            await self.clock.sleep(max(0.0, start + at - self.clock.now()))
            now = self.clock.now()
            if g not in self.world.online or g in self.world.afk or self._is_dead(g, now):
                continue
            try:
                ack = await self.send(Emote(id="", guid=g, emote=emote))
            except BridgeError as e:
                log.info("emote_dropped: guid=%d reason=%s", g, type(e).__name__)
                continue
            if not ack.ok:
                log.info("emote_dropped: guid=%d reason=%s", g, ack.error)

    def _wipe(self, ev: Boss, name: str, members: list[int], now: float) -> None:
        """After the silence, the lead's debrief, then up to 2 reactions; one joke is allowed
        once the raid has wiped on this boss twice tonight."""
        self._wipe_at = now
        lead = self._lead_among(members, now)
        channel = choose_channel(TurnKind.CALLOUT, speaker=lead, world=self.world,
                                 raid=self.raid, now=now, rng=self.rng) or "raid"
        facts = self._fight_facts(ev, name)
        wipes = self.raid.wipes_tonight(ev.map, ev.boss_id)
        again = wipes >= 2  # a wipe on it came before tonight: "killed by the adds again" is true
        items = [(Turn(kind=TurnKind.CALLOUT, speaker=lead, channel=channel,
                       instruction=self._lead_facts(ev, name),
                       delay=WIPE_SILENCE_S + self.rng.uniform(*DEBRIEF_S)),
                  TurnMeta(self._new_id(), topic=DEBRIEF.format(boss=name),
                           fight_map=ev.map, fight_boss=ev.boss_id,
                           fight_instance=ev.instance_id, fight_end_ms=ev.ts,
                           why=self._lead_why("wipe", lead), again_tonight=again))]
        others = [g for g in members if g != lead]
        weights, _ = self._weigh(others, last=lead, ctx=self._context(now), reply=False)
        n = self.rng.randint(1, 2)
        # the first reaction is offered to whoever died first, when it was one of them: never
        # to the second death when he died first (fix round 1, review I4)
        first = self._fight_deaths[0] if self._fight_deaths else None
        died = [first] if first in others else []
        picked = died + pick_speakers({g: w for g, w in weights.items() if g not in died},
                                      n - len(died), self.rng)
        for i, g in enumerate(picked):
            extra = JOKE_OK.format(n=wipes) if wipes >= 2 and i == 0 else NO_JOKE
            if g in died:
                extra = f"{DIED_FIRST} {extra}"
            why = "wipe_react:died_first" if g in died else "wipe_react:weighted_draw"
            items.append((Turn(kind=TurnKind.WIPE_REACT, speaker=g, channel=channel,
                               instruction=f"{extra} {facts}", delay=self.rng.uniform(*REACT_S)),
                          TurnMeta(self._new_id(), topic=name, why=why, again_tonight=again)))
        self._queue(items)

    # -- whispers ------------------------------------------------------------------------------

    def _note_fight(self, ev: Boss, now: float) -> None:
        """A boss fight he is in (a 5-man's included: the bridge reports every instance's),
        from its engage to its kill or wipe, at the event's own time when it has one."""
        hg = self.human.guid
        if not hg or hg not in ev.members:
            return
        key = (ev.map, ev.instance_id, ev.boss_id)
        if ev.state == "engaged":
            self._group_fights[key] = ev.ts / 1000 if ev.ts > 0 else now
        elif ev.state in ("killed", "wiped"):
            self._group_fights.pop(key, None)

    def _in_boss_fight(self, now: float) -> bool:
        """He is in a boss fight (_note_fight) that began less than MAX_FIGHT_S ago."""
        self._group_fights = {k: at for k, at in self._group_fights.items()
                              if now - at < MAX_FIGHT_S}
        return bool(self._group_fights)

    def _his_talk_at(self) -> float:
        """When he last said a line or a member last said one to him: the talk the clocks
        that start conversations with him wait out (budget.AFTER_BURST_S)."""
        hg = self.human.guid
        return max([self.world.last_spoke.get(hg, -math.inf),
                    *self._answered_at.values()]) if hg else -math.inf

    def _initiation_held(self, now: float) -> bool:
        """Nothing starts a conversation with him now: his raid's fight, its wipe or debrief,
        a boss fight of his group, his AFK."""
        return (self.raid.phase(now) in FIGHT_PHASES or self._in_boss_fight(now)
                or self.world.human_afk)

    def _whisper_threads(self, now: float) -> set[int]:
        """The members with a whisper to or from him in the last WHISPER_THREAD_S."""
        hg, out = self.human.guid, set()
        for seen in reversed(self.world.history):
            row = seen.row
            if now - row.ts >= WHISPER_THREAD_S:
                break
            if row.channel == "whisper" and hg in (row.from_guid, row.to_guid):
                out.add(row.to_guid if row.from_guid == hg else row.from_guid)
        return out

    def _may_whisper(self, now: float) -> bool:
        """He can be whispered first: online, past his login's quiet and nothing holding it
        (_initiation_held)."""
        return (bool(self.human.guid) and self.world.human_online
                and not self._login_quiet(now) and not self._initiation_held(now))

    def _whisper_cands(self, now: float) -> dict:
        """Who could whisper him now, with their relationship to him; a whisper reaches him
        anywhere. A member already in a whisper with him (_whisper_threads) is not one to
        start one."""
        hg, busy = self.human.guid, self._whisper_threads(now)
        return {g: (self.personas.get(g), self.store.get_relationship(g, hg))
                for g in self._candidates(hearable=False) if g not in busy}

    def _maybe_whisper(self, trigger: str, now: float, *, about: str = "") -> None:
        """One WhisperPlanner draw for one event (a kill, a quiet stretch, the late hour)."""
        if not self._may_whisper(now):
            return
        g = self.whispers.maybe(now, trigger, self._whisper_cands(now))
        if g is not None:
            self._queue_whisper(g, trigger, now, about=about)

    def _start_whisper(self, now: float) -> None:
        """The WhisperPlanner's own clock (tick): when its next whisper is due, a draw with no
        roll. It holds (_initiation_held) and waits AFTER_BURST_S after his own line or an
        answer to him, so it never talks over him."""
        held = (self._initiation_held(now)
                or now - self._his_talk_at() < AFTER_BURST_S)
        if not self.whispers.due(now, hold=held) or not self._may_whisper(now):
            return
        g = self.whispers.start(now, self._whisper_cands(now),
                                lift=romance_lift(self.cfg.romance.dial))
        if g is None:  # the hour's ceiling or the spacing, else nobody who may
            log.info("whisper_skipped: trigger=start reason=%s",
                     self.whispers.refusal(now) or "no_candidates")
            return
        self._queue_whisper(g, START, now)

    def _queue_whisper(self, g: int, trigger: str, now: float, *, about: str = "") -> None:
        """Her whisper to him: what set it off, that she starts the conversation and what she
        brings up (START_WHAT by romance.whisper_kind, a topic of her own: _open_topic). A
        casual one (everyone but a drawn or lust woman) carries no romance."""
        hg, p = self.human.guid, self.personas.get(g)
        kind = whisper_kind(p)
        they, them, their = self.human.forms
        what = START_WHAT[kind].format(topic=self._open_topic(p, now), they=they, them=them,
                                       their=their)
        told = " ".join(x for x in (WHISPER_WHY[trigger].format(about=about),
                                    START_WHISPER.format(them=them), what) if x)
        turn = Turn(kind=TurnKind.WHISPER, speaker=g, channel="whisper", to_guid=hg,
                    addressee=hg, instruction=told, delay=self.rng.uniform(*WHISPER_S[trigger]))
        self._queue([(turn, TurnMeta(self._new_id(), why=f"whisper:{trigger}",
                                     initiates=True, casual=kind == "casual"))])

    # -- after a line is said ------------------------------------------------------------------

    def _first_line_said(self, turn: Turn, meta: TurnMeta) -> None:
        """A bit counts as used once its first line is in chat (final fix wave ruling), even
        when the rest of it never goes out."""
        if turn.kind is TurnKind.BIT and turn.bit is not None:
            self.bits.record(turn.speaker, turn.bit, self.clock.now(), self._session_id)

    def _after_sent(self, turn: Turn, meta: TurnMeta, p: Persona, said: str = "") -> None:
        """The men's asides and a rival's flirt follow any public line that carried her
        romance text and names him ("evening zan, miss me?", "gz on zan's new axe"), judged by
        what was sent (rehearsal 2, judge C 6-8: only FLIRT turns counted, and a flirt that
        landed on a member did). A line that doesn't name him sets off nothing, and neither
        does any line in the guild's Discord: there members only answer him (D1)."""
        now = self.clock.now()
        if is_private(turn.channel) or not meta.romance or not self._names_him(said):
            return  # a whisper or a DM is seen by nobody else
        if is_discord(turn.channel):
            return  # nobody posts unprompted in his Discord: D1 is his to start
        if meta.loot_cheer:
            self._jealous("loot_cheer", p, turn.channel, now)
            return
        self._jealous("public_flirt", p, turn.channel, now)
        if not meta.rivalry:
            self._rival(turn, p, now)

    def _names_him(self, text: str) -> bool:
        """His name in the line, in any case, a possessive too ("zan's")."""
        return names_any(text, self._his_names())

    def _audience(self, channel: str, guid: int) -> frozenset[int]:
        return self.world.audience(channel, from_guid=guid, to_guid=0, zone=self._zone_of(guid))

    def _jealous(self, trigger: str, woman: Persona, channel: str, now: float) -> None:
        """The men who saw it may answer with an aside (JealousyAsides keeps its hourly cap,
        and gets an aside that is never said back: _turn_dropped)."""
        if self._login_quiet(now):
            return
        men = {g: q for g in self._candidates(self._audience(channel, woman.guid),
                                              exclude=(woman.guid,))
               if (q := self.personas.get(g)).identity.player_gender == "male"}
        plans = self.asides.on_trigger(trigger, woman, now, self.raid.phase(now), men)
        items: list[tuple[Turn, TurnMeta]] = []
        for plan in plans:
            meta = TurnMeta(self._new_id(), why=f"aside:{trigger}",
                            aside=plan if plan.counted else None)
            if items and not plan.counted:  # a defuse answers the sour aside before it (I7)
                meta.after = items[-1][1].turn_id
            items.append((Turn(kind=plan.kind, speaker=plan.speaker, channel=channel,
                               instruction=plan.instruction, delay=self.rng.uniform(*ASIDE_S)),
                          meta))
        self._queue(items)

    def _turn_dropped(self, turn: Turn, meta: TurnMeta) -> None:
        if meta.aside is not None:  # never said: it gives its place in the hour back
            self.asides.release(meta.aside)
            meta.aside = None

    def _rival(self, turn: Turn, woman: Persona, now: float) -> None:
        """Another woman who wants him may cut in with a flirt of her own."""
        if self._login_quiet(now):
            return
        women = {g: q for g in self._candidates(self._audience(turn.channel, woman.guid),
                                                exclude=(woman.guid,))
                 if (q := self.personas.get(g)).identity.player_gender == "female"}
        g = rivalry_reply(woman.guid, women, self.cfg.romance.jealousy, self.rng)
        if g is None:
            return
        flirt = Turn(kind=TurnKind.FLIRT, speaker=g, channel=turn.channel,
                     addressee=self.human.guid, instruction=rivalry_text(woman, self.human),
                     delay=self.rng.uniform(*RIVAL_S))
        self._queue([(flirt, TurnMeta(self._new_id(), rivalry=True, why="rival:flirt"))])


def _cheer_weight(p: Persona, stance_of) -> float:
    """LOOT_CHEER for her stance, its lift scaled by her forward dial (tuning): 0 makes her
    an ordinary cheerer, 10 twice the lift."""
    w = LOOT_CHEER.get(stance_of(p), 1.0)
    f = forward(p) if p is not None else 1.0
    return w if f == 1.0 else 1 + (w - 1) * f


def _fight_time(seconds: float) -> str:
    """A pull's length as raiders say it: "26 seconds", "1:35"."""
    s = int(round(seconds))
    return f"{s} seconds" if s < 60 else f"{s // 60}:{s % 60:02d}"
