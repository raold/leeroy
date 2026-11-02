"""The voice tables of mind.db (migration 0005): presence, spoken lines, his recognized lines."""
from leeroy_mind.storage.store import Store, clean


class VoiceStore:
    def __init__(self, store: Store):
        self.s = store

    def enter(self, guid: int, room: str, by: int, at: float, session_id: int | None) -> None:
        """Open a stretch; one still open for `guid` is closed first."""
        with self.s.transaction():
            self.s.conn.execute("UPDATE voice_presence SET until = ? "
                                "WHERE guid = ? AND until IS NULL", (at, guid))
            self.s.conn.execute(
                "INSERT INTO voice_presence(guid, room, since, by, session_id) "
                "VALUES (?, ?, ?, ?, ?)", (guid, room, at, by, session_id))

    def leave(self, guid: int, at: float) -> None:
        with self.s.transaction():
            self.s.conn.execute("UPDATE voice_presence SET until = ? "
                                "WHERE guid = ? AND until IS NULL", (at, guid))

    def close_all(self, at: float, *, keep_session: int | None = None) -> int:
        """Close every open stretch (except those of `keep_session`, when given); how many."""
        with self.s.transaction():
            return self.s.conn.execute(
                "UPDATE voice_presence SET until = ? WHERE until IS NULL "
                "AND (? IS NULL OR session_id IS NOT ?)", (at, keep_session, keep_session)).rowcount

    def open_presence(self, session_id: int) -> dict[int, tuple[str, int]]:
        """guid -> (room, by) for the stretches still open in that session."""
        rows = self.s._all("SELECT guid, room, by FROM voice_presence "
                           "WHERE until IS NULL AND session_id = ? ORDER BY id", (session_id,))
        return {r["guid"]: (r["room"], r["by"]) for r in rows}

    def add_line(self, speak_id: str, line_id: int, room: str, state: str, at: float,
                 *, reason: str | None = None) -> None:
        with self.s.transaction():
            self.s.conn.execute(
                "INSERT INTO voice_line(speak_id, line_id, room, state, reason, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)", (speak_id, line_id, room, state, reason, at))

    def set_line(self, speak_id: str, state: str, reason: str | None, at: float) -> None:
        with self.s.transaction():
            self.s.conn.execute("UPDATE voice_line SET state = ?, reason = ?, ts = ? "
                                "WHERE speak_id = ?", (state, reason, at, speak_id))

    def heard_line(self, guid: int, channel: str, text: str, since: float) -> int | None:
        """The newest line stored at `since` or later with those words from `guid` on
        `channel`: where a transcript landed."""
        rows = self.s._all("SELECT id FROM chat_line WHERE from_guid = ? AND channel = ? "
                           "AND text = ? AND ts >= ? ORDER BY id DESC LIMIT 1",
                           (guid, channel, clean(text), since))
        return rows[0]["id"] if rows else None

    def add_heard(self, line_id: int, raw: str, confidence: float, speech_ms: int,
                  latency_ms: int) -> None:
        with self.s.transaction():
            self.s.conn.execute(
                "INSERT OR REPLACE INTO voice_heard(line_id, raw, confidence, speech_ms, "
                "latency_ms) VALUES (?, ?, ?, ?, ?)",
                (line_id, raw, confidence, speech_ms, latency_ms))
