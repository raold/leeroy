import dataclasses
import json
from datetime import datetime, timezone

import pytest

from leeroy_mind.training import store
from leeroy_mind.training.report import training_section
from .test_tg_store import context, record


DAY = 86400
START = datetime(2026, 9, 28, tzinfo=timezone.utc).timestamp()
WEEK = "2026-W44"  # Begins 28 days after START.
NOW = START + 35 * DAY


def add(fake, day, deaths=2, *, duration=300, **changes):
    ts = START + day * DAY
    ident = f"{int(ts * 1000)}-77-0"
    rec = record(ident)
    rec.header = dataclasses.replace(rec.header, start_ms=int(ts * 1000),
        end_ms=int((ts + duration) * 1000), deaths=deaths)
    store.put_pull(fake.conn, rec, context(), {101: "tank"}, received=ts)
    with fake.conn:
        fake.conn.execute("UPDATE tg_pull SET score=-8*deaths,score_basis='deaths_time' WHERE id=?", (ident,))
        for key, value in changes.items():
            assert key in {"scoreable", "complete", "outcome", "score", "boss_id", "received", "kind",
                           "score_basis", "checks_complete", "checks_coverage"}
            fake.conn.execute(f"UPDATE tg_pull SET {key}=? WHERE id=?", (value, ident))
    return ident


def section(fake):
    return training_section(fake.conn, WEEK, now=NOW, names=lambda guid: f"Member {guid}")


def populate(fake, before=2, after=2):
    for i in range(10):
        add(fake, i / 10, before)
        add(fake, 28 + i / 10, after)


def test_section_says_plainly_where_nothing_improved(fake):
    populate(fake)
    result = section(fake)
    row = result["contexts"][0]
    assert row["change"] == "no change"
    assert row["boss"] in result["nothing_improved"]
    assert row["baseline_deaths"] == 2
    assert row["baseline_kill_s"] == 300
    assert row["pulls"] == row["kills"] == 10
    assert row["wipes"] == 0 and row["farm"]


def test_section_better_when_deaths_fall(fake):
    populate(fake, before=4, after=1)
    row = section(fake)["contexts"][0]
    assert row["change"] == "better"
    assert row["deaths_per_pull"] == 1
    assert row["deaths_change_ci"][1] < 0


def test_section_counts_only_scoreable_pulls(fake):
    add(fake, 28)
    for i, bad in enumerate([dict(scoreable=0), dict(complete=0), dict(outcome="aborted"),
                             dict(score=None), dict(boss_id=13)]):
        add(fake, 29 + i / 10, deaths=99, **bad)
    row = section(fake)["contexts"][0]
    assert row["pulls"] == 1 and row["deaths_per_pull"] == 2
    assert row["change"] == "too few"


def test_section_is_json_serialisable_and_has_every_key(fake):
    result = section(fake)
    assert set(result) == {"since", "gate", "contexts", "nothing_improved", "trials", "promotions",
        "rollbacks", "suspended", "rotations", "spread", "mentees", "rivals", "done_when"}
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    assert result["since"] is None and result["gate"] is None
    assert not any(result["done_when"].values())


def test_baseline_does_not_compare_a_pull_with_itself(fake):
    for i in range(10):
        add(fake, 28 + i / 10)
    assert section(fake)["contexts"][0]["change"] == "too few"


def test_backlog_and_elapsed_calendar_time_do_not_prove_four_weeks(fake):
    populate(fake)
    with fake.conn:
        fake.conn.execute("UPDATE tg_pull SET received=?", (NOW - DAY,))
        fake.conn.execute("INSERT INTO meta VALUES ('tg_first_pull',?)", (json.dumps(NOW - DAY),))
    assert not section(fake)["done_when"]["four_weeks"]


def test_done_when_requires_record_span_and_real_gate_evidence(fake):
    populate(fake)
    with fake.conn:
        fake.conn.execute("INSERT INTO meta VALUES ('tg_first_pull',?)", (json.dumps(START),))
        fake.conn.execute("INSERT INTO meta VALUES ('tg_gate',?)",
                          (json.dumps(dict(phase="passed", verdict_ms=0.5, raids=12)),))
    done = section(fake)["done_when"]
    assert done["four_weeks"] and done["gate_within_1ms"]
    assert not done["won_tactic"] and not done["won_rotation"] and not done["voice_run"]


def test_report_excludes_future_receipts_and_week_end(fake):
    add(fake, 28)
    add(fake, 29, received=NOW + DAY)
    add(fake, 35)
    assert section(fake)["contexts"][0]["pulls"] == 1


def test_kill_duration_excludes_wipes(fake):
    add(fake, 28, duration=300)
    add(fake, 29, duration=20, outcome="wiped")
    row = section(fake)["contexts"][0]
    assert row["kill_s"] == 300 and row["kill_s_ci"] == [300, 300]
    assert row["kills"] == row["wipes"] == 1


def test_report_is_repeatable(fake):
    populate(fake)
    assert section(fake) == section(fake)


def test_report_separates_bases_and_shows_missing_coverage(fake):
    populate(fake, before=4, after=1)
    add(fake, 30, deaths=99, score_basis="mechanics_deaths_time", checks_complete=1)
    add(fake, 31, deaths=88, score_basis="unknown")
    add(fake, 32, deaths=1, checks_coverage=json.dumps(["positions_missing"]))
    rows = {r["score_basis"]: r for r in section(fake)["contexts"]}
    assert set(rows) == {"deaths_time", "mechanics_deaths_time"}
    assert rows["deaths_time"]["pulls"] == 11
    assert rows["deaths_time"]["change"] == "better"
    assert "positions_missing" in rows["deaths_time"]["checks_coverage"]
    assert rows["mechanics_deaths_time"]["pulls"] == 1
    assert rows["mechanics_deaths_time"]["change"] == "too few"
