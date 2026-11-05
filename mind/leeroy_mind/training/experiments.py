"""Offline raid-outcome trials with atomic observation/verdict receipts.

No bridge calls occur on the ledger callback. Rotation metrics are unavailable:
requested_pre_ai damage and best-effort spell pairing cannot establish boss DPS.
Service owns lifecycle scheduling and delayed summary delivery.
"""

from dataclasses import replace
from datetime import datetime, timezone
import json
import random
import statistics

from leeroy_mind.guild.raids import CATALOG

from . import bandit, drift, store
from .cards import Card, LEVERS, stock_card
from .report import _meta
from .score import COVARIATES, SCORE_BASES, _duration, _finite, _scored_rows, adjusted, context_stats, is_farm, pull_score, standardize


def _put(conn, key, value):
    conn.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, store._json(value)))


def _scope(context):
    first, _, last = context.split(":")
    return f"{first}:*:{last}"


class Experimenter:
    SPEC_UNAVAILABLE = "spec_metric_unavailable: requested_pre_ai is not verified boss damage"

    def __init__(self, ports, cfg, *, score_basis="deaths_time"):
        if score_basis not in SCORE_BASES:
            raise ValueError("unsupported raid score basis")
        self.ports, self.cfg, self.conn = ports, cfg, ports.conn
        self.score_basis = score_basis
        self.refusals = {}

    def _rows(self, context=None, basis=None):
        now = self.ports.clock.now()
        return [r for r in _scored_rows(store.pull_rows(self.conn, context=context))
                if r["kind"] in {"offline", "his"} and _finite(r["received"])
                and 0 <= r["received"] <= now and r["end_ms"] <= now * 1000
                and (basis is None or r["score_basis"] == basis)]

    def ready(self):
        now = self.ports.clock.now()
        first, gate = _meta(self.conn, "tg_first_pull"), _meta(self.conn, "tg_gate")
        rows = self._rows()
        age = max(28, self.cfg.observe_days) * 86400
        return bool(self.cfg.enabled and self.cfg.act and self.cfg.experiments
                    and _finite(first) and 0 <= first <= now - age
                    and rows and max(r["received"] for r in rows) - min(r["received"] for r in rows) >= age
                    and isinstance(gate, dict) and gate.get("phase") == "passed"
                    and _finite(gate.get("verdict_ms")) and 0 <= gate["verdict_ms"] <= self.cfg.gate_max_ms)

    def _farm(self, context, basis):
        return is_farm([r["outcome"] for r in self._rows(context, basis)])

    def _offline(self, run):
        if (run is None or getattr(run, "human_in_raid", True) is not False
                or getattr(run, "lead_is_human", True) is not False or self.ports.human_online()):
            return False
        raid_id = getattr(getattr(run, "session", None), "raid_id", None)
        row = self.conn.execute("SELECT * FROM guild_raid WHERE raid_id=?", (raid_id,)).fetchone()
        raid = CATALOG.get(row["raid_key"]) if row else None
        return bool(row and raid and row["offline"] == 1 and row["ended"] is None
                    and getattr(run.plan, "offline", False) is True
                    and run.session.ctx.map_id == raid.map_id
                    and row["lead_guid"] == run.plan.lead_guid
                    and row["size"] == run.plan.size and bool(row["heroic"]) == run.plan.heroic)

    def _card(self, ident):
        row = self.conn.execute("SELECT * FROM tg_card WHERE id=?", (ident,)).fetchone()
        return Card.from_row(row) if row else None

    def _champion(self, context):
        row = store.champion(self.conn, context)
        return Card.from_row(row) if row else None

    def _insert_card(self, card):
        return store._insert(self.conn, "tg_card", card, store._CARD,
                             ("directives", "assignment", "evidence", "vetoes"))

    def _baseline(self, scope):
        card = self._champion(scope)
        if card is None:
            now = self.ports.clock.now()
            card = (Card(None, scope, 1, "champion", "stock", ()) if ":*:" in scope
                    else replace(stock_card(scope), status="champion"))
            card = replace(card, created=now, changed=now)
            card = replace(card, id=self._insert_card(card))
        return card

    @staticmethod
    def _signature(card):
        if card is None:
            return None
        row = card.to_row()
        row["vetoes"] = list(row["vetoes"])
        return {key: row[key] for key in ("id", "context", "version", "directives", "call", "assignment", "vetoes")}

    def _baselines(self, context):
        return {scope: self._signature(self._champion(scope)) for scope in (context, _scope(context))}

    def _baseline_valid(self, meta, context):
        return meta.get("baselines", {}).get(context) == self._baselines(context)

    def _trial_valid(self, meta):
        return (all(self._baseline_valid(meta, c) for c in meta.get("contexts", []))
                and bool(meta.get("baselines")) and bool(meta.get("references"))
                and all(self._signature(self._card(signature["id"])) == signature
                        for signature in meta.get("arms", {}).values()))

    def _reconcile_baselines(self):
        """Retire stale comparisons without erasing their factual evidence.

        Caller owns the transaction. A promoted pooled/boss card can invalidate
        another scope's fixed baseline. Its consumed proposals stay consumed;
        only newly proposed candidates may start a replacement comparison.
        """
        now = self.ports.clock.now()
        for trial in store.trial_rows(self.conn, status="open"):
            meta = _meta(self.conn, f"tg:trial:{trial['id']}")
            if not isinstance(meta, dict) or not meta.get("baselines"):
                continue
            changed = {context: dict(expected=meta["baselines"].get(context), actual=self._baselines(context))
                       for context in meta.get("contexts", []) if not self._baseline_valid(meta, context)}
            if not changed:
                continue
            counts = [dict(r) for r in self.conn.execute(
                "SELECT o.arm,count(*) AS observations FROM tg_obs o JOIN tg_arm a ON a.id=o.arm "
                "WHERE a.trial=? GROUP BY o.arm ORDER BY o.arm", (trial["id"],))]
            _put(self.conn, f"tg:trial_invalidated:{trial['id']}", dict(reason="baseline_changed",
                at=now, contexts=changed, preserved_observations=counts))
            self.conn.execute("UPDATE tg_card SET status='retired',changed=? WHERE status='trial' AND id IN "
                "(SELECT card FROM tg_arm WHERE trial=? AND champion=0)", (now, trial["id"]))
            self.conn.execute("UPDATE tg_arm SET status='retired' WHERE trial=? AND status='live'", (trial["id"],))
            self.conn.execute("UPDATE tg_trial SET status='retired',closed=?,verdict='baseline_changed' "
                "WHERE id=? AND status='open'", (now, trial["id"]))

    def _source(self, row, *, champion, median):
        """Immutable observed inputs; rolling ledger scores are NOT comparable.

        One frozen admission-time duration reference defines every raw score in
        this trial context. Member roles, receipt times and actual card/arm IDs
        remain alongside the canonical score for provenance and future audits.
        """
        source = dict(row)
        source["champion"] = int(champion)
        source["ledger_score"] = row["score"]
        source["score"] = pull_score(row["outcome"], row["deaths"],
            row["failed_mechanics"] if row["score_basis"] == "mechanics_deaths_time" else 0,
            _duration(row), median)
        source["member_roles"] = [dict(r) for r in self.conn.execute(
            "SELECT guid,role,class,spec_tab FROM tg_member WHERE pull=? ORDER BY guid", (row["id"],))]
        return source

    def _proposal(self, card, scope, champion, lever):
        """Derive one tactic-only arm without relabeling the original proposal.

        Spoken calls and assignments may independently change behavior. Preserve
        those prospective changes verbatim, but do not test or promote them as
        though a command-lever comparison had validated their effect.
        """
        ds = {d.lever: d for d in champion.directives} if lever != "stock" else {}
        ds.update({d.lever: d for d in card.directives})
        version = self.conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM tg_card WHERE context=?", (scope,)).fetchone()[0]
        provenance = dict(kind="tactic_only_treatment", source_card=card.id, source_context=card.context,
            source_version=card.version, tested_lever=lever, prospective_call=card.call,
            prospective_assignment=dict(card.assignment), speech_tested=False,
            tested_call=champion.call, tested_assignment=dict(champion.assignment))
        derived = replace(card, id=None, context=scope, version=version, lineage=card.id,
            directives=tuple(ds.values()), call=champion.call, assignment=dict(champion.assignment),
            evidence=(*card.evidence, provenance), created=self.ports.clock.now(), changed=self.ports.clock.now())
        ident = self._insert_card(derived)
        _put(self.conn, f"tg:proposal:{card.id}", dict(derived_card=ident, treatment=provenance))
        return replace(derived, id=ident)

    def _create(self, scope, lever, contexts, basis, candidates, revs):
        champion = self._baseline(scope)
        now = self.ports.clock.now()
        for context in contexts:
            self._baseline(context)
            self._baseline(_scope(context))
        candidates = [self._proposal(card, scope, champion, lever) for card in candidates]
        # A per-lever stock challenger preserves other champion levers. Whole
        # stock is selected only if that entails at most this one difference.
        if lever != "stock" and len(champion.directives) == 1 and champion.directives[0].lever == lever:
            stock = replace(champion, id=None, version=champion.version + 1,
                            status="candidate", origin="stock", directives=(), created=now, changed=now)
            candidates.append(replace(stock, id=self._insert_card(stock)))
        ident = self.conn.execute("INSERT INTO tg_trial(scope,lever,metric,status,opened) VALUES (?,?,'raid','open',?)",
                                  (scope, lever, now)).lastrowid
        definitions = {}
        for card, champ in [(champion, True), *((c, False) for c in candidates)]:
            arm = self.conn.execute("INSERT INTO tg_arm(trial,card,champion,status) VALUES (?,?,?,'live')",
                              (ident, card.id, int(champ))).lastrowid
            definitions[str(arm)] = self._signature(card)
            if not champ:
                self.conn.execute("UPDATE tg_card SET status='trial',changed=? WHERE id=?", (now, card.id))
        references = {}
        for context in contexts:
            history = self._rows(context, basis)[-200:]
            durations = [_duration(r) for r in history if r["outcome"] == "killed"]
            median = statistics.median(durations) if durations else None
            references[context] = dict(median_kill_s=median, rows=[self._source(r,
                champion=r["card_id"] == champion.id, median=median) for r in history])
        _put(self.conn, f"tg:trial:{ident}", dict(basis=basis, contexts=contexts, revisions=revs,
             champion=champion.id, stock=lever == "stock",
             drift=_meta(self.conn, "tg:drift_run") is not None,
             treatment="tactic_only_fixed_speech_and_assignments",
             baselines={c: self._baselines(c) for c in contexts}, arms=definitions, references=references))
        return ident

    async def open_trials(self) -> int:
        self.refusals = {}
        if not self.ready():
            return 0
        revs = drift.revisions(self.cfg.core_dir)
        if drift.drift_due(self.conn, revs) or _meta(self.conn, "tg:drift_run") is not None:
            return 0
        with self.conn:
            self._reconcile_baselines()
        count = 0
        # One fixed basis per context/scope. Explicit deaths/time remains usable
        # under incomplete mechanics; no scores from the other basis enter it.
        basis = self.score_basis
        contexts = sorted({r["context"] for r in self._rows(basis=basis) if self._farm(r["context"], basis)})
        groups = {}
        for row in self.conn.execute("SELECT * FROM tg_card WHERE status='candidate' ORDER BY id"):
            card = Card.from_row(row)
            if _meta(self.conn, f"tg:proposal:{card.id}") is not None:
                continue
            if len(card.directives) != 1 or card.directives[0].lever not in LEVERS:
                continue
            lever = card.directives[0].lever
            if lever == "rotation":
                self.refusals[card.id] = self.SPEC_UNAVAILABLE
                continue
            scope = _scope(card.context) if LEVERS[lever].pooled else card.context
            members = [c for c in contexts if c == scope or _scope(c) == scope]
            if not members or card.context != scope and card.context not in members:
                continue
            groups.setdefault((scope, lever), []).append(card)
        with self.conn:
            # Materialize stock slots before capturing any context baseline, so
            # a second trial cannot invalidate a first merely by naming stock.
            for context in contexts:
                self._baseline(context)
                self._baseline(_scope(context))
            for (scope, lever), candidates in groups.items():
                if store.trial_rows(self.conn, scope, status="open"):
                    continue
                members = [c for c in contexts if c == scope or _scope(c) == scope]
                self._create(scope, lever, members, basis, candidates, revs)
                count += 1
        return count

    def _arms(self, trial):
        return [dict(r) for r in self.conn.execute("SELECT * FROM tg_arm WHERE trial=? AND status='live' ORDER BY id", (trial,))]

    def _observations(self, arms, context=None):
        return [bandit.Arm(a["id"], tuple(r["value"] for r in store.obs_rows(self.conn, a["id"], context=context)),
                           bool(a["champion"])) for a in arms]

    def _recompute(self, trial, meta):
        """Rebuild derived observations under ONE current per-context transform.

        Caller owns the transaction. Immutable raw receipts remain unchanged;
        tg_obs is a derived cache, atomically updated before either sampling or
        a verdict. Fixed baseline membership and kill-time reference eliminate
        ledger arrival/rolling-median differences. OLS and champion preference
        may activate, but then EVERY observation uses that same new transform.
        """
        arms = self._arms(trial["id"])
        observations = [dict(obs) for arm in arms for obs in store.obs_rows(self.conn, arm["id"])]
        transforms = {}
        for context in meta["contexts"]:
            reference = meta["references"][context]
            selected = [obs for obs in observations if obs["context"] == context]
            receipts = []
            for obs in selected:
                receipt = _meta(self.conn, f"tg:obs:{obs['arm']}:{obs['pull']}")
                if (not isinstance(receipt, dict) or receipt.get("id") != obs["pull"]
                        or receipt.get("score_basis") != meta["basis"]
                        or receipt.get("context") != context):
                    raise ValueError("missing or incompatible immutable observation receipt")
                receipts.append(receipt)
            rows = [*reference["rows"], *receipts]
            values = adjusted(rows)
            stats = context_stats([dict(row, score=values[row["id"]]) for row in rows])
            for obs in selected:
                self.conn.execute("UPDATE tg_obs SET value=? WHERE arm=? AND pull=?",
                    (standardize(values[obs["pull"]], stats), obs["arm"], obs["pull"]))
            transforms[context] = dict(mean=stats.mean, sd=stats.sd, n=len(rows),
                observation_pulls=sorted(obs["pull"] for obs in selected),
                median_kill_s=reference["median_kill_s"], basis=meta["basis"])
        _put(self.conn, f"tg:transform:{trial['id']}", transforms)
        return statistics.fmean(8 / item["sd"] for item in transforms.values())

    def _effective(self, card, context, lever):
        directives = {}
        assignments = {}
        base = self._champion(context)
        pooled = self._champion(_scope(context))
        for baseline in (pooled, base):
            if baseline and context not in baseline.vetoes:
                directives.update({d.lever: d for d in baseline.directives})
                assignments.update(baseline.assignment)
        if lever == "stock":
            # Full stock only compares one changed lever; admission below gates.
            directives = {d.lever: d for d in card.directives}
        else:
            directives.pop(lever, None)
            directives.update({d.lever: d for d in card.directives if d.lever == lever})
        return replace(card, context=context, status="trial", directives=tuple(directives.values()),
                       call=base.call if base else stock_card(context).call,
                       assignment=assignments)

    def arm_valid(self, context, arm_id):
        """Listener rechecks around awaited application, before recording play."""
        row = self.conn.execute("SELECT t.id FROM tg_trial t JOIN tg_arm a ON a.trial=t.id "
            "WHERE a.id=? AND a.status='live' AND t.status='open'", (arm_id,)).fetchone()
        if row is None:
            return False
        meta = _meta(self.conn, f"tg:trial:{row['id']}")
        return bool(isinstance(meta, dict) and context in meta.get("contexts", [])
                    and self._trial_valid(meta) and meta.get("revisions") == drift.revisions(self.cfg.core_dir))

    def arm_for(self, context, run) -> tuple[Card, int] | None:
        if not self.ready() or not self._offline(run):
            return None
        expected = f"{run.session.ctx.map_id}:*:{int(run.plan.size == 25) + 2 * int(run.plan.heroic)}"
        if _scope(context) != expected:
            return None
        revs = drift.revisions(self.cfg.core_dir)
        if drift.drift_due(self.conn, revs):
            return None
        raid_id = run.session.raid_id
        drift_run = _meta(self.conn, "tg:drift_run")
        if drift_run is not None and drift_run != raid_id:
            return None
        week = datetime.fromtimestamp(self.ports.clock.now(), timezone.utc).isocalendar().week
        first = not self.conn.execute("SELECT 1 FROM tg_pull WHERE raid_id=? AND context=?", (raid_id, context)).fetchone()
        if (drift_run is None and first and week % 3 == 0 and self._farm(context, self.score_basis)
                and not store.trial_rows(self.conn, context, status="open")
                and not store.trial_rows(self.conn, _scope(context), status="open")):
            with self.conn:
                self._stock_trial(context, revs)
        candidates = []
        for trial in store.trial_rows(self.conn, status="open"):
            meta = _meta(self.conn, f"tg:trial:{trial['id']}")
            if (not isinstance(meta, dict) or context not in meta.get("contexts", [])
                    or meta.get("revisions") != revs or meta.get("basis") not in SCORE_BASES
                    or not self._farm(context, meta["basis"]) or bool(meta.get("drift")) != (drift_run is not None)
                    or not self._trial_valid(meta)):
                continue
            arms = self._arms(trial["id"])
            # Raw observed wipes bench even when scoring has not run yet.
            arms = [a for a in arms if not self.conn.execute(
                "SELECT 1 FROM tg_pull WHERE raid_id=? AND arm_id=? AND outcome='wiped'", (raid_id, a["id"])).fetchone()]
            if not arms:
                continue
            candidates.append((trial, arms))
        if not candidates:
            return None
        trial, arms = candidates[0]
        with self.conn:
            self._recompute(trial, _meta(self.conn, f"tg:trial:{trial['id']}"))
        stock = [a for a in arms if self._card(a["card"]) is not None and not self._card(a["card"]).directives]
        chosen = (stock[0]["id"] if first and week % 3 == 0 and stock else
                  bandit.top_two(self._observations(arms), random.Random(f"{raid_id}:{context}:{len(store.pull_rows(self.conn))}")))
        arm = next(a for a in arms if a["id"] == chosen)
        card = self._card(arm["card"])
        if card is None or context in card.vetoes or card.status not in {"champion", "trial"}:
            return None
        return self._effective(card, context, trial["lever"]), arm["id"]

    def _matching(self, row):
        for trial in store.trial_rows(self.conn, status="open"):
            meta = _meta(self.conn, f"tg:trial:{trial['id']}")
            if (trial["metric"] != "raid" or not isinstance(meta, dict)
                    or row["context"] not in meta.get("contexts", []) or row["score_basis"] != meta.get("basis")
                    or row["received"] < trial["opened"] or row["start_ms"] < trial["opened"] * 1000
                    or not self._trial_valid(meta)):
                continue
            arms = self._arms(trial["id"])
            arm = next((a for a in arms if a["id"] == row["arm_id"] and a["card"] == row["card_id"]), None)
            if row["kind"] == "his":
                if row["arm_id"] is not None:
                    continue
                arm = next((a for a in arms if a["champion"] and a["card"] == row["card_id"]), None)
                if arm is not None:
                    offline = self.conn.execute("SELECT count(*) FROM tg_obs o JOIN tg_pull p ON p.id=o.pull "
                        "WHERE o.arm=? AND p.kind='offline' AND p.score_basis=? AND p.received<=? AND p.end_ms<=?",
                        (arm["id"], meta["basis"], row["received"], row["start_ms"])).fetchone()[0]
                    if offline < 3:
                        arm = None
            if arm is not None:
                return trial, meta, arms, arm
        return None

    async def on_pull(self, pull_id: str) -> bandit.Verdict | None:
        # No awaits/transport, and no public store writes inside this transaction.
        if not self.ready():
            return None
        row = next((r for r in self._rows() if r["id"] == pull_id), None)
        if row is None or row["kind"] not in {"offline", "his"}:
            return None
        match = self._matching(row)
        if match is None:
            return None
        trial, meta, arms, arm = match
        if meta.get("revisions") != drift.revisions(self.cfg.core_dir):
            return None
        cov = row["covariates"]
        try:
            cov = json.loads(cov) if isinstance(cov, str) else cov
        except (ValueError, TypeError):
            return None
        if (not isinstance(cov, dict) or any(not _finite(cov.get(k)) for k in COVARIATES)
                or cov["human_in_raid"] != int(row["kind"] == "his") or cov["assisted"] != row["assisted"]):
            return None
        source = self._source(row, champion=bool(arm["champion"]),
                              median=meta["references"][row["context"]]["median_kill_s"])
        with self.conn:
            # Taking the write lock before checking mutable trial state ensures a
            # replay cannot append a second verdict or increment wipes twice.
            inserted = self.conn.execute("INSERT INTO tg_obs(arm,pull,context,value) VALUES (?,?,?,?) "
                "ON CONFLICT(arm,pull) DO NOTHING", (arm["id"], pull_id, row["context"], 0.0)).rowcount
            if not inserted:
                return None
            current = self.conn.execute("SELECT status FROM tg_trial WHERE id=?", (trial["id"],)).fetchone()
            if current[0] != "open":
                raise RuntimeError("trial closed during observation")
            _put(self.conn, f"tg:obs:{arm['id']}:{pull_id}", source)
            if row["outcome"] == "wiped":
                self.conn.execute("UPDATE tg_arm SET wipes=wipes+1 WHERE id=?", (arm["id"],))
            arms = self._arms(trial["id"])
            margin = self._recompute(trial, meta)
            observations = self._observations(arms)
            verdict = bandit.decide(observations, random.Random(f"{trial['id']}:{pull_id}"),
                margin=margin, wipes={a["id"]: a["wipes"] for a in arms},
                total_pulls=sum(len(a.xs) for a in observations))
            _put(self.conn, f"tg:verdict:{trial['id']}:{pull_id}", dict(action=verdict.action, arm=verdict.arm,
                 why=verdict.why, basis=meta["basis"], card=row["card_id"], context=row["context"]))
            self._verdict(trial, meta, arms, verdict)
        return verdict

    def _verdict(self, trial, meta, arms, verdict):
        if verdict.action == "continue":
            return
        now = self.ports.clock.now()
        if verdict.action == "promote":
            winner = next(a for a in arms if a["id"] == verdict.arm)
            champion = next(a for a in arms if a["champion"])
            if store.champion(self.conn, trial["scope"])["id"] != champion["card"]:
                raise RuntimeError("champion changed during trial")
            vetoes = []
            for context in meta["contexts"]:
                by_id = {a.id: a for a in self._observations(arms, context)}
                if min(len(by_id[winner["id"]].xs), len(by_id[champion["id"]].xs)) >= 4 and bandit.below(
                        by_id[winner["id"]], by_id[champion["id"]], 0,
                        random.Random(f"veto:{trial['id']}:{context}")) >= .9:
                    vetoes.append(context)
            self.conn.execute("UPDATE tg_card SET status='retired',changed=? WHERE id=?", (now, champion["card"]))
            self.conn.execute("UPDATE tg_card SET status='champion',prev_champion=?,vetoes=?,changed=? WHERE id=?",
                              (champion["card"], store._json(vetoes), now, winner["card"]))
        for arm in arms:
            won = verdict.action == "promote" and arm["id"] == verdict.arm
            self.conn.execute("UPDATE tg_arm SET status=? WHERE id=?", ("won" if won else "lost", arm["id"]))
            if not arm["champion"] and not won:
                self.conn.execute("UPDATE tg_card SET status='retired',changed=? WHERE id=? AND status='trial'", (now, arm["card"]))
        status = {"promote": "promoted", "retire": "retired", "give_up": "gave_up"}[verdict.action]
        self.conn.execute("UPDATE tg_trial SET status=?,closed=?,verdict=? WHERE id=? AND status='open'",
                          (status, now, verdict.why, trial["id"]))

    async def drift_check(self, run):
        if not self.ready() or not self._offline(run):
            return False
        revs = drift.revisions(self.cfg.core_dir)
        if not drift.known(revs):
            return False
        raid_id = run.session.raid_id
        if not drift.drift_due(self.conn, revs):
            previous = _meta(self.conn, "tg:drift_run")
            if previous is not None and previous != raid_id:
                with self.conn:
                    self._close_open("drift")
                    self.conn.execute("DELETE FROM meta WHERE key='tg:drift_run'")
            return True
        with self.conn:
            self._close_open("drift")
            _put(self.conn, "tg_revs", revs)
            _put(self.conn, "tg:drift_run", raid_id)
            contexts = sorted({r["context"] for r in self._rows(basis=self.score_basis)})
            for context in contexts:
                self._stock_trial(context, revs)
        return True

    def _stock_trial(self, context, revs):
        champion = self._champion(context)
        # Whole stock must not change several boss/pooled levers at once.
        pooled = self._champion(_scope(context))
        if (not self._farm(context, self.score_basis) or champion is None
                or len(champion.directives) != 1 or pooled and pooled.directives):
            return
        card = replace(stock_card(context), version=champion.version + 1,
                       created=self.ports.clock.now(), changed=self.ports.clock.now())
        card = replace(card, id=self._insert_card(card))
        self._create(context, "stock", [context], self.score_basis, [card], revs)

    def _close_open(self, status, context=None):
        now = self.ports.clock.now()
        for trial in store.trial_rows(self.conn, context, status="open"):
            self.conn.execute("UPDATE tg_card SET status='retired',changed=? WHERE status='trial' AND id IN "
                "(SELECT card FROM tg_arm WHERE trial=? AND champion=0)", (now, trial["id"]))
            self.conn.execute("UPDATE tg_arm SET status='retired' WHERE trial=?", (trial["id"],))
            self.conn.execute("UPDATE tg_trial SET status=?,closed=?,verdict=? WHERE id=?", (status, now, status, trial["id"]))

    async def rollback(self, context) -> bool:
        champion = self._champion(context)
        previous = self._card(champion.prev_champion) if champion else None
        if previous is None or previous.context != context or previous.status != "retired":
            return False
        with self.conn:
            self._close_open("retired", context)
            self.conn.execute("UPDATE tg_card SET status='retired',changed=? WHERE id=?", (self.ports.clock.now(), champion.id))
            self.conn.execute("UPDATE tg_card SET status='champion',prev_champion=?,changed=? WHERE id=?",
                              (champion.id, self.ports.clock.now(), previous.id))
            _put(self.conn, f"tg:rollback:{context}:{self.ports.clock.now()}", dict(old=champion.id, new=previous.id))
        return True

    async def stock(self, context) -> bool:
        champion = self._champion(context)
        card = (Card(None, context, 1, "champion", "stock", ()) if ":*:" in context else stock_card(context))
        with self.conn:
            self._close_open("retired", context)
            if champion:
                self.conn.execute("UPDATE tg_card SET status='retired',changed=? WHERE id=?", (self.ports.clock.now(), champion.id))
            self._insert_card(replace(card, status="champion", version=champion.version + 1 if champion else 1,
                prev_champion=champion.id if champion else None, created=self.ports.clock.now(), changed=self.ports.clock.now()))
        return True

    async def offline_summary(self, run, ended_at) -> bool:
        """Independent service ticker only; claim one daily attempt before send.

        A failed/ambiguous transport is not retried, preventing duplicate speech.
        The receipt denotes an attempted summary, never guaranteed delivery.
        """
        from .lines import template
        now = self.ports.clock.now()
        if not _finite(ended_at) or now < ended_at + 60 or run.human_in_raid or self.ports.human_online():
            return False
        raid = self.conn.execute("SELECT offline,ended,lead_guid FROM guild_raid WHERE raid_id=?",
                                 (run.session.raid_id,)).fetchone()
        if (not raid or raid["offline"] != 1 or raid["ended"] is None or raid["ended"] > ended_at
                or raid["lead_guid"] != run.plan.lead_guid):
            return False
        rows = self.conn.execute("SELECT DISTINCT p.id FROM tg_pull p JOIN tg_obs o ON o.pull=p.id "
            "WHERE p.raid_id=? AND p.kind='offline'", (run.session.raid_id,)).fetchall()
        if not rows:
            return False
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        with self.conn:
            inserted = self.conn.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO NOTHING",
                (f"tg:summary:{day}", store._json(dict(raid=run.session.raid_id, attempted=now)))).rowcount
        if not inserted:
            return False
        return await self.ports.say(run.plan.lead_guid, "guild", template("offline_summary",
            what=f"tactic trial, {len(rows)} pulls; results recorded"))
