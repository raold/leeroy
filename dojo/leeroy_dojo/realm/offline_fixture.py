"""Repeat offline pulls from the same private synthetic guild and equipment."""
from __future__ import annotations

import hashlib
import json
import subprocess

from . import mysql, world


def validate_training(fixture):
    if (not isinstance(fixture, dict) or type(fixture.get("fixture_preparation_version")) is not int
            or fixture["fixture_preparation_version"] != 2):
        raise world.WorldError("synthetic fixture predates trained talent ranks; archive its checkpoint and prepare again")
    ranks = fixture.get("trained_ranks")
    if not isinstance(ranks, list) or not ranks:
        raise world.WorldError("synthetic fixture has no trained talent rank evidence")
    seen = set()
    for rank in ranks:
        if (not isinstance(rank, dict) or any(type(rank.get(k)) is not int for k in ("slot", "base", "expected"))
                or rank["slot"] not in range(13) or rank["base"] <= 0 or rank["expected"] <= 0
                or rank.get("known") is not True or (rank["slot"], rank["base"]) in seen):
            raise world.WorldError("synthetic fixture has invalid trained talent rank evidence")
        seen.add((rank["slot"], rank["base"]))
    # Both fixed fire mages must have the level-80 Pyroblast learned through their trainer.
    if any(not any(rank["slot"] == slot and rank["base"] == 11366 and rank["expected"] == 42891
                   for rank in ranks) for slot in (6, 12)):
        raise world.WorldError("synthetic fire mage lacks trained Pyroblast rank 12")


class FixtureCheckpoint:
    KINDS = ("auth", "characters", "playerbots")

    def __init__(self, layout):
        self.layout = layout
        self.root = layout.secrets / "offline-fixture"
        self.receipt = self.root / "receipt.json"

    def _client(self):
        if world.running(self.layout):
            raise world.WorldError("fixture checkpoint requires the isolated world to be stopped")
        client = mysql.Client(self.layout, self.layout.server.root_cnf)
        mysql.verify_identity(self.layout, client)
        return client

    @staticmethod
    def digest(path):
        with path.open("rb") as file:
            return hashlib.file_digest(file, "sha256").hexdigest()

    def save(self, fixture):
        validate_training(fixture)
        client = self._client()
        if self.receipt.exists():
            raise world.WorldError("refusing to replace an existing fixture checkpoint")
        self.root.mkdir(parents=True, exist_ok=True)
        mysql.restrict_to_owner(self.root)
        files = {}
        for kind in self.KINDS:
            schema = self.layout.schemas[kind]
            self.layout.check_db_target(self.layout.host, self.layout.db_port, schema)
            path = self.root / f"{kind}.sql"
            args = client._base("mysqldump") + ["--single-transaction", "--skip-lock-tables", "--no-tablespaces",
                    "--set-gtid-purged=OFF", "--hex-blob", "--skip-comments", schema]
            with path.open("wb") as output:
                result = subprocess.run(args, stdout=output, stderr=subprocess.PIPE, timeout=300)
            if result.returncode or not path.stat().st_size:
                raise world.WorldError(f"synthetic {kind} checkpoint failed (exit {result.returncode})")
            files[kind] = self.digest(path)
        data = dict(format=1, instance=self.layout.instance,
                    schemas={kind: self.layout.schemas[kind] for kind in self.KINDS}, files=files, fixture=fixture)
        pending = self.root / "receipt.tmp"
        pending.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        pending.replace(self.receipt)

    def restore(self):
        client = self._client()
        if not self.receipt.exists():
            return None
        data = json.loads(self.receipt.read_text(encoding="utf-8"))
        schemas = {kind: self.layout.schemas[kind] for kind in self.KINDS}
        if data.get("format") != 1 or data.get("instance") != self.layout.instance or data.get("schemas") != schemas:
            raise world.WorldError("fixture checkpoint belongs to a different isolated realm")
        validate_training(data.get("fixture"))
        # Validate every file before importing any schema. SQL dumps contain the
        # synthetic account credentials and remain under the private secrets root.
        for kind in self.KINDS:
            path = self.root / f"{kind}.sql"
            if not path.is_file() or self.digest(path) != data.get("files", {}).get(kind):
                raise world.WorldError("fixture checkpoint is missing or changed")
        for kind in self.KINDS:
            self.layout.check_db_target(self.layout.host, self.layout.db_port, schemas[kind])
            with (self.root / f"{kind}.sql").open("rb") as source:
                result = subprocess.run(client._base("mysql") + ["--default-character-set=utf8mb4", schemas[kind]],
                                        stdin=source, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=300)
            if result.returncode:
                raise world.WorldError(f"synthetic {kind} restore failed (exit {result.returncode})")
        return data["fixture"]

