"""Shared input evidence and invariant screens for public lexicon births."""
import re

from leeroy_mind.nightly.evidence import Evidence, load_items
from leeroy_mind.nightly.ground import Claim, check
from leeroy_mind.text import strip_wow_markup


def evidence(ctx, refs):
    ev = Evidence()
    for item in load_items(ctx.store, refs, ident=ctx.ident):
        ev.add(item)
    return ev


def clean(text):
    return " ".join(strip_wow_markup(text).split())


def screen(ctx, kind, owner, raw, refs, ev, facts):
    """Screen raw input first: cleanup or truncation must not hide a prohibited suffix."""
    # WoW's texture/link opener is glued to its payload (|TName|t). Make only that
    # structural boundary visible to the ordinary case-sensitive real-name matcher;
    # retain the entire payload so stripping it cannot hide names or other invariants.
    screened_raw = re.sub(r"\|([TH])", r"|\1 ", raw)
    claim = Claim(kind, owner, screened_raw, list(refs))
    verdict = check(claim, ev, facts)
    if not verdict.ok:
        return claim, verdict
    claim = Claim(kind, owner, clean(raw), list(refs))
    return claim, check(claim, ev, facts)

