"""The Realm's parts: each later feature attaches here, without editing shared files.

A part module is ``leeroy_mind.realm.<name>`` for a name in PART_MODULES. Its
``attach(realm)`` returns ``(part, event_types)``, or None when the part declines, and does
no I/O at all: no socket, task, thread, subprocess, database write or model call. The
Realm then awaits ``part.tick()`` (optional) each second and ``part.on_event(ev)`` for its
event types, both only while it is enabled. Anything longer than a moment goes to
``realm.spawn``, so a part never holds up the Realm's loop. A part reaches another through
``realm.part(name)``, which is None while that one is absent.

The ``direct`` part is also the exactly-once owner of the non-body commands it prepared
(DirectPart): the service asks it after the announcer and the action queue, and before
its own in-memory direct path.

``realm.night`` is not a part. install_night adds its stage to the night runner once, at
attach, and only when the Realm is enabled then; a later switch waits for a restart.
"""
from __future__ import annotations

import importlib
import logging
from typing import Protocol

log = logging.getLogger(__name__)

PART_MODULES = ("direct", "steps", "speech", "talk", "rivals", "race", "poach", "canon", "bank",
                "calendar", "mail")
NIGHT_MODULE = "night"
NIGHT_STAGE = "realm"
NIGHT_AFTER = "stories"


class DirectPart(Protocol):
    """What the service asks of realm.parts["direct"]; every call is synchronous.

    owns_command says whether the part prepared this exact command. before_send is its
    final, committing barrier, called last, immediately before the bridge writes; raising
    refuses the send. on_ack, on_send_error and on_job report actual settlement (also while
    the Realm is disabled) and return whether the part matched them.
    """

    def owns_command(self, cmd) -> bool: ...
    def before_send(self, cmd, *, context) -> None: ...
    def on_ack(self, cmd, ack) -> bool: ...
    def on_send_error(self, cmd, error_type: str) -> bool: ...
    def on_job(self, ev) -> bool: ...


def _module(name):
    """The realm module, or None when it does not exist. A present module whose own imports
    fail raises: that is a fault, not an absence."""
    full = f"{__package__}.{name}"
    try:
        return importlib.import_module(full)
    except ModuleNotFoundError as error:
        if error.name != full:
            raise
        return None


def build_parts(realm) -> dict:
    """Attach every present part module, in PART_MODULES order; returns realm.parts.

    Absent modules are named in one realm_part_absent line. A module that fails to import
    or attach is logged by its error's type only and left out, so a broken part never stops
    the mind (nor the Realm's other parts).
    """
    absent = []
    for name in PART_MODULES:
        try:
            module = _module(name)
            if module is None:
                absent.append(name)
                continue
            attached = module.attach(realm)
            if attached is None:
                log.info("realm_part_declined name=%s", name)
                continue
            part, event_types = attached
            realm.add_part(part, event_types=event_types)
        except Exception as error:
            log.error("realm_part_failed name=%s error=%s", name, type(error).__name__)
            continue
        realm.parts[name] = part
    if absent:
        log.info("realm_part_absent names=%s", ",".join(absent))
    return realm.parts


def install_night(app, realm) -> bool:
    """Add the realm's night stage after "stories" on the reflection model, only when the
    Realm is enabled now and realm.night is present; otherwise log realm_night_unavailable
    once, with the reason. realm.night supplies attach(realm) -> stage | None, or stage_realm."""
    reason = None
    runner = getattr(app, "night_runner", None)
    if not realm.enabled:
        reason = "disabled"
    elif runner is None or not callable(getattr(runner, "add_stage", None)):
        reason = "no_runner"
    else:
        try:
            night = _module(NIGHT_MODULE)
            if night is None:
                reason = "absent"
            else:
                attach = getattr(night, "attach", None)
                stage = attach(realm) if callable(attach) else getattr(night, "stage_realm", None)
                if not callable(stage):
                    reason = "declined"
                else:
                    runner.add_stage(NIGHT_STAGE, stage, after=NIGHT_AFTER, model="reflection")
        except Exception as error:
            log.error("realm_night_failed error=%s", type(error).__name__)
            reason = "failed"
    if reason is not None:
        log.info("realm_night_unavailable reason=%s", reason)
        return False
    log.info("realm_night_installed after=%s", NIGHT_AFTER)
    return True
