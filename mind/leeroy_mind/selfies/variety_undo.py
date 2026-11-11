"""Private field receipts consumed by the existing persona-ledger undo mechanism."""
from dataclasses import asdict
import json

from leeroy_mind.nightly.enrich import _race_class
from leeroy_mind.nightly.store import ledger_add
from leeroy_mind.personas.io import load_persona, save_persona
from leeroy_mind.personas.schema import persona_from_dict, persona_to_dict
from . import kind

PRIOR = "selfies:variety-prior:"
UNDO = "selfies:variety-undo:"


def record(store, before, after, *, now, released):
    source = store.conn.execute("SELECT source FROM persona_core WHERE guid=?",(before.guid,)).fetchone()[0]
    receipt = dict(appearance=asdict(before.appearance) if before.appearance else None,
        photo=asdict(before.photo) if before.photo else None, after=asdict(after.appearance),
        source=source, photo_record=store.get_meta(kind.STYLE_KEY.format(guid=before.guid)),
        pending_pick=store.get_meta(f"selfies:variety-pick:{before.guid}"), released=released)
    ledger = ledger_add(store,guid=before.guid,kind="variety",text="re-rolled appearance",
                        evidence=[],now=now)
    raw = json.dumps(receipt)
    store.set_meta(PRIOR+str(before.guid),raw)
    store.set_meta(UNDO+str(ledger),raw)
    return ledger


def restore(store, row, *, now):
    raw = store.get_meta(UNDO+str(row["id"]))
    if raw is None:
        return False
    receipt = json.loads(raw)
    p = load_persona(store,row["guid"])
    if p is None or p.appearance is None or json.loads(json.dumps(asdict(p.appearance))) != receipt["after"]:
        return False  # a later appearance choice wins
    data = persona_to_dict(p)
    data["appearance"] = receipt["appearance"]
    restore_photo = p.photo is None or kind.chosen_style(store,p) is not None
    if restore_photo:
        data["photo"] = receipt["photo"]
    restored = persona_from_dict(data)
    race,cls = _race_class(store,p.guid)
    # Older receipts lack provenance; retain their current source rather than invent owner authorship.
    source = receipt.get("source") or store.conn.execute(
        "SELECT source FROM persona_core WHERE guid=?",(p.guid,)).fetchone()[0]
    save_persona(store,restored,source=source,ts=now,race=race,class_name=cls)
    if restore_photo:
        key = kind.STYLE_KEY.format(guid=p.guid)
        if receipt["photo_record"] is None:
            store.conn.execute("DELETE FROM meta WHERE key=?",(key,))
        else:
            store.set_meta(key,receipt["photo_record"])
    pending = f"selfies:variety-pick:{p.guid}"
    if receipt["pending_pick"] is None:
        store.conn.execute("DELETE FROM meta WHERE key=?",(pending,))
    else:
        store.set_meta(pending,receipt["pending_pick"])
    store.conn.execute("DELETE FROM meta WHERE key=?",(f"selfies:restyle:pending:{p.guid}",))
    return True
