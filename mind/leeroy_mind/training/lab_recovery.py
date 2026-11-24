"""Recovery of a blocked dummy lab on native evidence the mind already stored.

The lab blocks when it cannot prove native returned its members (lab_runner).
The operator's ``training lab-clear`` (mind stopped) clears that block only on
native evidence the running mind already stored: the trial's ``dummy_cleanup``
receipt (restored or released), a stored terminal that proves every return, or
a worldserver process that started after the trial (its in-memory job, fence
and journals went with the old process, and the core drops one-member groups
when it loads). A lab blocked with no trial in flight (vocab probes and custom
strategy priming touch no body) needs no native evidence. The running lab
clears itself by the same rule, but only on native's own word that its trial
let go: the receipt or a newer worldserver (``recover_blocked_lab``). Until
2026-11-24 every such block waited for an operator while its members stayed
held. Neither path dials the bridge, sends a command or restores a standing
setup, so a member with a standing setup refuses the clear. The completed record
must pass the body holds' own validation before it commits. Attempt receipts
(with their earlier restoration state), results and native receipts are kept;
the night gets a summary with the lab's halt as its reason.
"""

import math
import re

from leeroy_mind.guild.loot import _get, _put

from .body_holds import identity, members, validate_lab_complete
from .lab import _DAMAGE_SPECS
from .lab_runner import (ATTEMPT_PREFIX, CLEARED_PREFIX, CURSOR_KEY, NATIVE_PREFIX, NATIVE_START_KEY, OWNED_KEY,
                         SUMMARY_PREFIX, desired_setups, terminal_proof)


RECOVERED_KINDS = frozenset({"dummy_cleanup", "native_terminal", "native_restart"})
# Native's own word that the trial holds no body any more: what the running lab clears itself on.
NATIVE_KINDS = frozenset({"dummy_cleanup", "native_restart"})
_NIGHT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class LabClearRefused(Exception):
    """Said to the operator as it is; nothing was written. The running lab logs ``code`` and
    ``members`` (a count) instead of the words."""

    def __init__(self, message, *, code="refused", members=0):
        super().__init__(message)
        self.code, self.members = code, members


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _owned(conn):
    owned = _get(conn, OWNED_KEY)
    if owned is None:
        raise LabClearRefused("there is no lab record; nothing to clear", code="no_lab")
    if type(owned) is not dict:
        raise LabClearRefused("the lab record is not readable; inspect it with `training lab`", code="malformed")
    phase = owned.get("phase")
    if phase != "blocked":
        raise LabClearRefused(f"the lab is {phase if isinstance(phase, str) else 'unreadable'}, "
                              "not blocked; nothing to clear", code="not_blocked")
    try:
        scope, guids = identity(owned["id"]), members(owned["guids"])
        attempts = owned["attempt_ids"]
        if (type(owned["spec"]) is not list or tuple(owned["spec"]) not in _DAMAGE_SPECS
                or type(attempts) is not list or len(attempts) != len(set(attempts)) or len(attempts) > 128
                or any(type(a) is not str for a in attempts) or type(owned["gaps"]) is not list
                or not _NIGHT.fullmatch(owned["night"]) or not _number(owned["started"])
                or type(owned["halt"]) is not str or not owned["halt"]):
            raise ValueError
        for ident in attempts:
            identity(ident)
    except (KeyError, TypeError, ValueError):
        raise LabClearRefused("the lab record is malformed; inspect it with `training lab` "
                              "before clearing anything", code="malformed") from None
    return owned, scope, guids


def _trial(owned, guids):
    """The lab's current trial: its id, members and send time. Raises LabClearRefused."""
    current = owned["current"]
    if type(current) is not dict or current.get("id") not in owned["attempt_ids"]:
        raise LabClearRefused("the lab's current trial is malformed or missing from its attempt list; "
                              "inspect it with `training lab`", code="malformed")
    try:
        ident = identity(current["id"])
        sent = current["sent_ms"]
        trial = members(current["guids"])
        if type(sent) is not int or sent <= 0 or not trial <= guids:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise LabClearRefused("the lab's current trial is malformed; inspect it with `training lab`",
                              code="malformed") from None
    return ident, trial, sent


def _evidence(conn, current, ident, sent):
    """Native's proof that the trial no longer holds a body; None while there is none."""
    if current.get("ack") == "rejected" and current.get("terminal") is None:
        return dict(kind="ack_rejected", trial=ident)
    receipt = _get(conn, NATIVE_PREFIX + ident)
    if (type(receipt) is dict and receipt.get("id") == ident and receipt.get("state") in {"restored", "released"}
            and type(receipt.get("ts")) is int and type(receipt.get("unfinished")) is list):
        return dict(kind="dummy_cleanup", trial=ident, state=receipt["state"],
                    unfinished=[g for g in receipt["unfinished"] if type(g) is int], ts=receipt["ts"])
    if terminal_proof(current) == "":  # The very rule the lab blocked on; stored results included.
        return dict(kind="native_terminal", trial=ident, ts=current["terminal"]["ts"])
    terminal = current.get("terminal")
    after = max(sent, terminal["ts"] if type(terminal) is dict and type(terminal.get("ts")) is int else 0)
    start = _get(conn, NATIVE_START_KEY)
    if type(start) is dict and type(start.get("started_ms")) is int and start["started_ms"] > after:
        return dict(kind="native_restart", trial=ident, started_ms=start["started_ms"])
    return None


def _no_evidence(current, ident, trial):
    terminal = current.get("terminal")
    reason = terminal.get("reason") if type(terminal) is dict else None
    said = (f"its terminal says {reason}" if isinstance(reason, str) and re.fullmatch(r"[A-Za-z0-9_:.-]{1,96}", reason)
            else "no clean terminal was stored")
    return LabClearRefused(
        f"no native evidence yet that dummy trial {ident} let go of members "
        f"{','.join(str(g) for g in sorted(trial))} ({said}). Start the mind and wait: native sends a "
        "dummy_cleanup receipt when the trial's cleanup is restored, or 10 minutes after its failure once it "
        "holds none of them; a worldserver restart the mind has greeted also counts. Then stop the mind and "
        "run this again.", code="no_evidence")


def clear_blocked_lab(conn, now):
    """Clear a blocked lab on stored native evidence, in one transaction. Raises LabClearRefused."""
    owned, scope, guids = _owned(conn)
    evidence = None
    if owned.get("current") is not None:
        ident, trial, sent = _trial(owned, guids)
        evidence = _evidence(conn, owned["current"], ident, sent)
        if evidence is None:
            raise _no_evidence(owned["current"], ident, trial)
    return _clear(conn, now, owned, scope, guids, evidence, by="operator")


def recover_blocked_lab(conn, now):
    """The running lab's own clear, only for a trial native said it let go of (NATIVE_KINDS).

    None while native has said nothing yet, or when the block is not about a trial's bodies:
    those stay the operator's. Raises LabClearRefused when native's word is in but clearing is
    not safe (a standing setup, a malformed record). Nothing is written unless it clears.
    """
    owned, scope, guids = _owned(conn)
    if owned.get("current") is None:
        return None
    ident, _, sent = _trial(owned, guids)
    evidence = _evidence(conn, owned["current"], ident, sent)
    if evidence is None or evidence["kind"] not in NATIVE_KINDS:
        return None
    return _clear(conn, now, owned, scope, guids, evidence, by="native")


def _clear(conn, now, owned, scope, guids, evidence, *, by):
    current = owned.get("current")
    held = sorted(desired_setups(conn, guids))
    if held:
        raise LabClearRefused(
            f"members {','.join(map(str, held))} have a standing setup that only the running mind can apply after "
            "the lab; this command restores nothing. Leave the lab blocked and inspect the setups.",
            code="standing_setup", members=len(held))
    night = owned["night"]
    summary_key = SUMMARY_PREFIX + night
    existing = _get(conn, summary_key)
    if existing is not None and (type(existing) is not dict or existing.get("lab") != scope):
        raise LabClearRefused(f"night {night} already has another lab's summary; inspect it with `training lab`",
                              code="night_taken")
    recovery = dict(at=now, halt=owned["halt"], evidence=evidence, by=by)
    with conn:
        if current is not None:
            key = ATTEMPT_PREFIX + current["id"]
            if evidence["kind"] == "ack_rejected":
                _put(conn, key, dict(current, rejected=True))
            else:
                attempt = _get(conn, key)
                record = attempt if type(attempt) is dict else dict(
                    current, lab=scope, spec=owned["spec"], round=owned.get("round", 0), rows={},
                    metric="dummy_requested_damage_proxy", field_performance="unavailable")
                record.update(restoration_before=record.get("restoration"), restoration="native_recovered",
                              restoration_at=now, recovered=evidence)
                _put(conn, key, record)
                if current["id"] not in owned["gaps"]:
                    owned["gaps"].append(current["id"])
            owned["current"] = None
        owned["attempts"] = len(owned["attempt_ids"])
        owned["phase"] = "complete"
        owned["recovered"] = recovery
        summary = dict(lab=scope, spec=owned["spec"], winner=None, pairs=0, aggregate_pairs=0, winner_round=None,
                       attempts=owned["attempts"], gaps=list(owned["gaps"]), reason=owned["halt"],
                       status="dummy/provisional", metric="dummy_requested_damage_proxy",
                       field_performance="unavailable", ts=now, recovered=True)
        _put(conn, OWNED_KEY, owned)
        _put(conn, summary_key, summary)
        _put(conn, CURSOR_KEY, owned["spec"])
        _put(conn, CLEARED_PREFIX + scope, dict(recovery, night=night, members=sorted(guids)))
        try:
            # The body holds read exactly this rule; a record it rejects would make them hold every body.
            validate_lab_complete(conn, owned)
        except (ValueError, KeyError, TypeError):
            raise LabClearRefused("the cleared record would not be a valid completed lab (an odd member list, "
                                  "attempt or summary); nothing was written. Inspect it with `training lab`.",
                                  code="invalid_completion") from None
    return dict(cleared=scope, night=night, halt=owned["halt"], evidence=evidence,
                members_freed=sorted(guids), summary=summary_key,
                kept="attempt receipts, results and native receipts; no command was sent")
