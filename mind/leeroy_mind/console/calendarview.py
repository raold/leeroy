"""The Calendar tab's read-only question to mind.db: what the Realm's calendar desk has posted
(with who signed up), and the raid nights he missed. A table an older mind.db lacks is named in
`missing` and reads as empty."""
import sqlite3

from leeroy_mind.realm.calendar import night_text

from . import views
from .feed import human_guid

CALENDAR_TABLES = ("realm_calendar", "realm_rsvp", "realm_direct", "session", "guild_raid")
UPCOMING_DAYS = 14
MISSED_DAYS = 28
RUNNING_S = 3 * 3600
DAY_S = 86400
STATUSES = ("signed_up", "tentative", "declined")


def _text(conn, at) -> str:
    try:
        return night_text(conn, at) or "missed"
    except sqlite3.OperationalError as e:
        if str(e).startswith(("no such table", "no such column")):
            return "missed"
        raise


def calendar_view(conn, *, now: float) -> dict:
    have = views.tables(conn)
    missing = [t for t in CALENDAR_TABLES if t not in have]
    human = human_guid(conn)
    upcoming = []
    for r in views.rows(conn, "SELECT id, kind, title, at, creator, state, event_id FROM realm_calendar "
                              "WHERE state IN ('posted','pending') AND at >= ? AND at <= ? "
                              "ORDER BY at, id", (now - RUNNING_S, now + UPCOMING_DAYS * DAY_S)):
        signups: dict[str, list[int]] = {"signed_up": [], "tentative": [], "declined": [], "other": []}
        you = None
        if r["event_id"] is not None:
            for s in views.rows(conn, "SELECT guid, status FROM realm_rsvp WHERE event = ? ORDER BY guid",
                                (r["event_id"],)):
                if human and s["guid"] == human:
                    you = s["status"]
                else:
                    signups[s["status"] if s["status"] in STATUSES else "other"].append(s["guid"])
        upcoming.append({"id": r["id"], "kind": r["kind"], "title": r["title"], "at": r["at"],
                         "creator": r["creator"], "state": r["state"], "signups": signups, "you": you})
    missed = [{"id": r["id"], "at": r["at"], "title": r["title"], "text": _text(conn, r["at"])}
              for r in views.rows(
                  conn, "SELECT c.id, c.at, c.title FROM realm_calendar c JOIN realm_direct d "
                        "ON d.owner = 'calendar' AND d.ref = 'update:' || c.id AND d.state = 'acked' "
                        "WHERE c.kind = 'raid' AND c.at >= ? AND c.at <= ? ORDER BY c.at DESC, c.id DESC",
                  (now - MISSED_DAYS * DAY_S, now))]
    return {"human": human, "names": {str(g): n for g, n in views.names(conn).items()},
            "missing": missing, "upcoming": upcoming, "missed": missed}
