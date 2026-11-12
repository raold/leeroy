"""Shared world-to-cast seed and atomic write, after normal cast generation/screens."""
from .io import canonical_json, load_persona
from .tiers import is_cast


def promotion_seed(store, guid, summary):
    version = store.persona_versions(tier=None).get(guid)
    if not is_cast(store.conn, guid):
        try:
            seed = load_persona(store, guid, include_world=True)
        except ValueError:
            seed = None  # an unreadable old seed must not strand a cast member
        if seed is not None:
            summary += "\nPrevious world persona for this newcomer (seed data; adapt to the cast):\n" + canonical_json(seed)
    return version, summary


def promote_world(store, guid, expected_version):
    """Join the caller's persona-write transaction; a changed seed wins over generation.

    Callers recheck authoritative cast membership before this write. Already-rendered
    world sets are passed, retaining files/candidates. A queued set has no old images
    and will render from the newly saved cast persona instead.
    """
    if is_cast(store.conn, guid):
        return True
    if store.persona_versions(tier=None).get(guid) != expected_version:
        return False
    store.conn.execute("UPDATE persona_tier SET tier='cast' WHERE guid=?", (guid,))
    store.conn.execute("UPDATE world_member SET retired=1 WHERE guid=?", (guid,))
    for (number,) in store.conn.execute("SELECT set_no FROM selfie_set WHERE guid=? "
                                       "AND status='ready'", (guid,)).fetchall():
        store.set_meta(f"selfies:set-world:{guid}:{number}", "1")
    store.conn.execute("UPDATE selfie_set SET decided='passed' WHERE guid=? "
                       "AND status='ready' AND decided IS NULL", (guid,))
    return True
