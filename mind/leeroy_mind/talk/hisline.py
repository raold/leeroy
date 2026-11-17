"""What a line of his is, read by cheap rules (no model call, no I/O): bad news of his own
(illness, a death, a hospital, a job lost, a rough day) and a goodbye (he is leaving the
conversation). Callers: an interruption never follows his bad news (life/service.py), the
`question` shape never forces a question on either, and a goodbye of his is answered with one
whatever she said earlier (the `later` move's repeat tell; self-play audit, 2026-11-17).

The words are non-explicit and say nothing about anyone: a lexicon beside life.state's HARSH_RE
and WARM_RE (his tone toward her), for what happened to him."""
from __future__ import annotations

import re

_APOSTROPHES = str.maketrans({"’": "'", "ʼ": "'"})

BAD_NEWS_RE = re.compile(
    r"\b(?:hospital|icu|emergency room|the er|a&e|ambulance|surgery|operation|biops\w+|cancer"
    r"|tumou?r|stroke|heart (?:attack|thing|problem|condition|failure)|chemo\w*|diagnos\w+"
    r"|funeral|passed away|death in the family|put (?:him|her|them) down|had to put \w+ down"
    r"|(?:my|our) (?:dad|father|mum|mom|mother|brother|sister|son|daughter|wife|husband|partner"
    r"|friend|grandma|grandpa|gran|nan|grandad|uncle|aunt|cousin|dog|cat|pet)\b[^.!?]{0,30}"
    r"\b(?:died|is dying|passed)|lost (?:my|our) (?:dad|father|mum|mom|mother|brother|sister"
    r"|son|daughter|wife|husband|partner|friend|grandma|grandpa|gran|nan|uncle|aunt|dog|cat|job)"
    r"|miscarr\w+|at the vet|to the vet|the vet (?:said|says|thinks)"
    r"|(?:got |been |was |were |getting |being )?(?:fired|laid off|let go|sacked)|made redundant"
    r"|lost (?:my|the) job|broke up|breaking up|dumped me|left me|divorc\w+"
    r"|(?:rough|bad|awful|terrible|shit|shitty|horrible|hard|worst) (?:day|night|week|shift|time)"
    r"|chewed me out|yelled at me|screamed at me|in tears|crying|cried|scared|terrified|worried sick"
    r"|(?:really |so |pretty |very )?(?:sick|ill|unwell)(?! of)|in (?:a lot of |so much )?pain"
    r"|car accident|an accident|car crash|crashed (?:my|the|his|her) car|broke (?:my|his|her) (?:arm|leg|wrist|ankle|hip|back|neck)"
    r"|not doing (?:well|good|great)|(?:isn'?t|is not|not) going to make it|bad news)\b",
    re.IGNORECASE)
# a goodbye past talk.select's (which reads a guild line): he says he has to go, or when they
# will talk next
_GOODBYE_RE = re.compile(
    r"\b(?:(?:gotta|got to|have to|need to|gonna|going to|i'?ll) "
    r"(?:go|head|run|leave|sleep|bounce|crash|get going|get to (?:bed|sleep|work)|log off|dip)"
    r"(?=\s*(?:$|[.,!;]|now\b|soon\b|off\b|out\b|then\b|though\b|sorry\b|babe\b|x+\b|in a\b"
    r"|to (?:bed|work|sleep|the \w+)\b|for (?:work|the night|now|a bit)\b))"
    r"|heading (?:off|out|to bed|to work|to sleep)|off to (?:bed|work|sleep)|going to bed"
    r"|(?:talk|speak|chat|catch you|see (?:you|ya|u)|c u) (?:later|tonight|tomorrow|soon|then"
    r"|in the morning|after)|ttyl|bbl|good ?night|g'?night|nighty? night|sleep (?:well|tight)"
    r"|sweet dreams|bye|cya|gn)\b"
    r"|^\s*(?:night|nite|later|laters)(?:\s+(?:x+|then|babe|all|guys|you|u))?\s*[.!]*\s*(?:x+)?$",
    re.IGNORECASE)


def bad_news(text: str) -> bool:
    """His line carries bad news of his own (BAD_NEWS_RE)."""
    return bool(text) and bool(BAD_NEWS_RE.search(text.translate(_APOSTROPHES)))


def goodbye(text: str) -> bool:
    """His line says he is going (talk.select's goodbye, or _GOODBYE_RE); never a question
    ("should i go to bed?")."""
    t = (text or "").translate(_APOSTROPHES).strip()
    if not t or t.endswith("?"):
        return False
    from leeroy_mind.talk.select import classify_human_line
    return classify_human_line(t) == "goodbye" or bool(_GOODBYE_RE.search(t))
