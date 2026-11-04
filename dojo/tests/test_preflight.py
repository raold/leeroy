"""scripts/dojo/preflight.ps1 against a synthetic install. Needs pwsh and git; skipped without them.

Everything lives in a temporary directory: a fake live install (its own git repos, configs with fake credentials, a
Data folder) next to a dojo layout derived from it. Nothing here reads the real install.
"""

import copy
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "dojo" / "preflight.ps1"
PWSH = shutil.which("pwsh")
GIT = shutil.which("git")

LIVE_PW = "livepw-7c3x"
LIVE_USER = "liveuser7"
TRAIN_PW = "trainpw-9d1x"
TRAIN_USER = "trainuser8"


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=dojo-test", "-c", "user.email=dojo@invalid", "-c", "init.defaultBranch=main",
         "-c", "commit.gpgsign=false", *args],
        cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_repo(path: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        write(path / rel, text)
    git(path, "init", "-q")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "baseline")


def link_dir(target: Path, link: Path) -> None:
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def unlink_dir(link: Path) -> None:
    if os.name == "nt":
        os.rmdir(link)
    else:
        os.unlink(link)


def free_ports(count: int) -> list[int]:
    sockets = [socket.socket() for _ in range(count)]
    try:
        for s in sockets:
            s.bind(("127.0.0.1", 0))
        return [s.getsockname()[1] for s in sockets]
    finally:
        for s in sockets:
            s.close()


def db(user: str, pw: str, schema: str) -> str:
    return f'"127.0.0.1;3306;{user};{pw};{schema}"'


@unittest.skipUnless(PWSH and GIT, "needs pwsh and git")
class PreflightTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="dojo-preflight-"))
        cls.links: list[Path] = []
        t = cls.tmp
        live = t / "live"
        make_repo(live, {"README.md": "live install\n", ".gitignore": "*\n!README.md\n!.gitignore\n"})
        cls.live_core = live / "azerothcore-wotlk"
        make_repo(cls.live_core, {"src/server/README": "core\n", "modules/.gitkeep": "",
                                  ".gitignore": "modules/mod-playerbots/\n"})
        cls.live_pb = cls.live_core / "modules" / "mod-playerbots"
        make_repo(cls.live_pb, {"src/Bot/PlayerbotAI.cpp": "// bots\n"})
        configs = live / "build" / "bin" / "RelWithDebInfo" / "configs"
        write(configs / "worldserver.conf", "\n".join([
            "# live",
            f"LoginDatabaseInfo     = {db(LIVE_USER, LIVE_PW, 'acore_auth')}",
            f"WorldDatabaseInfo     = {db(LIVE_USER, LIVE_PW, 'acore_world')}",
            f"CharacterDatabaseInfo = {db(LIVE_USER, LIVE_PW, 'acore_characters')}",
            "WorldServerPort = 8085", ""]))
        write(configs / "authserver.conf",
              f"LoginDatabaseInfo = {db(LIVE_USER, LIVE_PW, 'acore_auth')}\nRealmServerPort = 3724\n")
        write(configs / "modules" / "playerbots.conf",
              f"Playerbots.DatabaseInfo = {db(LIVE_USER, LIVE_PW, 'acore_playerbots')}\n")
        cls.assets = live / "build" / "bin" / "RelWithDebInfo" / "Data"
        for sub in ("dbc", "maps", "vmaps", "mmaps"):
            (cls.assets / sub).mkdir(parents=True)
        (live / "modules" / "mod-leeroy").mkdir(parents=True)
        (live / "mind-data").mkdir()

        source = t / "wt"
        (source / "modules" / "mod-dojo").mkdir(parents=True)
        (source / "dojo" / "leeroy_dojo").mkdir(parents=True)
        cls.core = t / "core"
        cls.pb = t / "pb"
        git(t, "clone", "-q", str(cls.live_core), str(cls.core))
        git(t, "clone", "-q", str(cls.live_pb), str(cls.pb))
        cls.link(cls.pb, cls.core / "modules" / "mod-playerbots")
        cls.link(source / "modules" / "mod-dojo", cls.core / "modules" / "mod-dojo")

        runtime = t / "rt" / "instances" / "t1"
        (t / "rt" / "build").mkdir(parents=True)
        write(runtime / "dojo.marker", json.dumps({"marker": "leeroy-dojo-training", "instance": "t1"}))
        world, auth = free_ports(2)
        cls.train_configs = runtime / "configs"
        write(cls.train_configs / "worldserver.conf", "\n".join([
            f"LoginDatabaseInfo     = {db(TRAIN_USER, TRAIN_PW, 'dojo_auth')}",
            f"WorldDatabaseInfo     = {db(TRAIN_USER, TRAIN_PW, 'dojo_world')}",
            f"CharacterDatabaseInfo = {db(TRAIN_USER, TRAIN_PW, 'dojo_characters')}",
            f'SourceDirectory = "{cls.core.as_posix()}"',
            f'DataDir = "{cls.assets.as_posix()}"',
            f'LogsDir = "{(runtime / "logs").as_posix()}"',
            "MapUpdate.Threads = 0",
            f"WorldServerPort = {world}",
            f"RealmServerPort = {auth}", ""]))
        write(cls.train_configs / "modules" / "playerbots.conf",
              f"Playerbots.DatabaseInfo = {db(TRAIN_USER, TRAIN_PW, 'dojo_playerbots')}\n")

        cls.layout = {
            "format": 1,
            "marker": "leeroy-dojo-training",
            "instance": "t1",
            "roots": {"source": source.as_posix(), "core": cls.core.as_posix(), "playerbots": cls.pb.as_posix(),
                      "build": (t / "rt" / "build").as_posix(), "runtime": runtime.as_posix(),
                      "data": (t / "dd").as_posix()},
            "provenance": (t / "dd" / "manifests" / "provenance.json").as_posix(),
            "modules": ["mod-playerbots", "mod-dojo"],
            "assets": cls.assets.as_posix(),
            "database": {"host": "127.0.0.1", "port": 3306, "user": TRAIN_USER,
                         "schemas": {"auth": "dojo_auth", "characters": "dojo_characters", "world": "dojo_world",
                                     "playerbots": "dojo_playerbots"},
                         "bootstrap_sources": ["acore_world"]},
            "ports": {"auth": auth, "world": world, "dojo_host": "127.0.0.1", "dojo": 0},
            "production": {"install_root": live.as_posix(), "core": cls.live_core.as_posix(),
                           "build": (live / "build").as_posix(), "configs": configs.as_posix(),
                           "modules": (live / "modules").as_posix(), "protected": [(live / "mind-data").as_posix()],
                           "databases": [{"host": "127.0.0.1", "port": 3306, "schema": "acore_world"}],
                           "ports": [3724, 8085, 7878]},
        }
        cls.runs = 0
        code, report, output = cls.run_preflight(cls.layout, "-WriteProvenance")
        if code != 0:
            raise AssertionError(f"provenance not written: {output}")

    @classmethod
    def tearDownClass(cls):
        for link in cls.links:
            if link.exists() or link.is_symlink():
                unlink_dir(link)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def link(cls, target: Path, link: Path) -> None:
        link_dir(target, link)
        cls.links.append(link)

    @classmethod
    def run_preflight(cls, layout: dict, *extra: str, as_json: bool = True):
        cls.runs += 1
        path = cls.tmp / f"layout-{cls.runs}.json"
        path.write_text(json.dumps(layout), encoding="utf-8")
        args = [PWSH, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT), "-Layout", str(path), *extra]
        if as_json:
            args.append("-Json")
        result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=180)
        output = result.stdout + result.stderr
        report = json.loads(result.stdout) if as_json and result.stdout.strip().startswith("{") else None
        return result.returncode, report, output

    def failures(self, report: dict) -> dict[str, str]:
        return {c["check"]: c["detail"] for c in report["checks"] if c["status"] == "FAIL"}

    def assert_no_secrets(self, output: str) -> None:
        for secret in (LIVE_PW, TRAIN_PW, LIVE_USER, TRAIN_USER):
            self.assertNotIn(secret, output)

    def changed(self, **edits) -> dict:
        layout = copy.deepcopy(self.layout)
        for dotted, value in edits.items():
            node = layout
            keys = dotted.split("__")
            for key in keys[:-1]:
                node = node[key]
            node[keys[-1]] = value
        return layout

    def test_accepts_the_synthetic_layout(self):
        code, report, output = self.run_preflight(self.layout)
        self.assertEqual(self.failures(report), {}, output)
        self.assertEqual(code, 0)
        self.assertTrue(report["ok"])
        checks = {c["check"]: c for c in report["checks"]}
        for name in ("root source", "root core", "root runtime", "module mod-dojo", "module mod-playerbots", "marker",
                     "provenance", "baseline core", "baseline playerbots", "assets", "port world", "port auth",
                     "database layout world", "database training worldserver.conf WorldDatabaseInfo",
                     "config SourceDirectory", "toolchain git"):
            self.assertIn(name, checks, output)
        self.assertIn("junction", checks["module mod-dojo"]["detail"])
        self.assert_no_secrets(output)

    def test_text_report_names_roots_without_credentials(self):
        code, _, output = self.run_preflight(self.layout, as_json=False)
        self.assertEqual(code, 0, output)
        self.assertIn("preflight: ok", output)
        self.assertIn(self.core.as_posix().lower(), output.lower())
        self.assert_no_secrets(output)

    def test_rejects_the_production_build_tree(self):
        layout = self.changed(roots__build=self.layout["production"]["build"])
        code, report, output = self.run_preflight(layout)
        self.assertEqual(code, 1)
        self.assertIn("production build", self.failures(report)["root build"])
        self.assert_no_secrets(output)

    def test_rejects_the_production_config_directory(self):
        layout = self.changed(roots__configs=self.layout["production"]["configs"])
        code, report, output = self.run_preflight(layout)
        self.assertEqual(code, 1)
        self.assertIn("production configuration", self.failures(report)["config target"])
        self.assert_no_secrets(output)

    def test_rejects_a_config_that_loads_sql_from_the_live_core(self):
        conf = self.train_configs / "worldserver.conf"
        original = conf.read_text(encoding="utf-8")
        try:
            conf.write_text(original.replace(self.core.as_posix(), self.live_core.as_posix()), encoding="utf-8")
            code, report, _ = self.run_preflight(self.layout)
        finally:
            conf.write_text(original, encoding="utf-8")
        self.assertEqual(code, 1)
        self.assertIn("config SourceDirectory", self.failures(report))

    def test_rejects_an_external_junction(self):
        link = self.core / "modules" / "mod-leeroy"
        link_dir(Path(self.layout["production"]["modules"]) / "mod-leeroy", link)
        try:
            code, report, output = self.run_preflight(self.changed(modules=["mod-playerbots", "mod-dojo", "mod-leeroy"]))
            code_default, report_default, _ = self.run_preflight(self.layout)
        finally:
            unlink_dir(link)
        self.assertEqual(code, 1)
        self.assertIn("external junction", self.failures(report)["module mod-leeroy"], output)
        self.assertEqual(code_default, 1)
        self.assertIn("not in the training module set", self.failures(report_default)["module mod-leeroy"])

    def test_rejects_mod_dojo_linked_outside_the_source(self):
        link = self.core / "modules" / "mod-dojo"
        unlink_dir(link)
        link_dir(Path(self.layout["production"]["modules"]) / "mod-leeroy", link)
        try:
            code, report, _ = self.run_preflight(self.layout)
        finally:
            unlink_dir(link)
            link_dir(Path(self.layout["roots"]["source"]) / "modules" / "mod-dojo", link)
        self.assertEqual(code, 1)
        self.assertIn("external junction", self.failures(report)["module mod-dojo"])

    def test_rejects_a_missing_marker(self):
        marker = Path(self.layout["roots"]["runtime"]) / "dojo.marker"
        hidden = marker.with_name("dojo.marker.hidden")
        marker.rename(hidden)
        try:
            code, report, _ = self.run_preflight(self.layout)
        finally:
            hidden.rename(marker)
        self.assertEqual(code, 1)
        self.assertIn("missing training marker", self.failures(report)["marker"])
        code, report, _ = self.run_preflight(self.changed(instance="other"))
        self.assertIn("does not mark instance", self.failures(report)["marker"])

    def test_rejects_writable_live_database_targets(self):
        code, report, output = self.run_preflight(self.changed(database__schemas__world="acore_world"))
        self.assertEqual(code, 1)
        self.assertIn("writable live database target", self.failures(report)["database layout world"])
        code, report, output2 = self.run_preflight(self.changed(database__user=LIVE_USER))
        self.assertEqual(code, 1)
        self.assertIn("live server's database user", self.failures(report)["database layout auth"])
        self.assert_no_secrets(output + output2)

    def test_rejects_a_live_target_in_the_training_config(self):
        conf = self.train_configs / "worldserver.conf"
        original = conf.read_text(encoding="utf-8")
        try:
            conf.write_text(original.replace("dojo_characters", "acore_characters"), encoding="utf-8")
            code, report, output = self.run_preflight(self.layout)
        finally:
            conf.write_text(original, encoding="utf-8")
        self.assertEqual(code, 1)
        failures = self.failures(report)
        self.assertIn("writable live database target",
                      failures["database training worldserver.conf CharacterDatabaseInfo"])
        self.assert_no_secrets(output)

    def test_rejects_production_ports(self):
        code, report, _ = self.run_preflight(self.changed(ports__world=8085))
        self.assertEqual(code, 1)
        self.assertIn("production port", self.failures(report)["port world"])
        code, report, _ = self.run_preflight(self.changed(ports__dojo_host="0.0.0.0"))
        self.assertIn("not a loopback", self.failures(report)["port dojo_host"])

    def test_rejects_a_missing_provenance_manifest(self):
        layout = self.changed(provenance=(self.tmp / "nowhere" / "provenance.json").as_posix())
        code, report, _ = self.run_preflight(layout)
        self.assertEqual(code, 1)
        self.assertIn("missing provenance manifest", self.failures(report)["provenance"])

    def test_provenance_records_baselines_and_dirty_fingerprints(self):
        dirty = self.live_pb / "src" / "Bot" / "PlayerbotAI.cpp"
        original = dirty.read_bytes()
        target = (self.tmp / "dd2" / "provenance.json").as_posix()
        try:
            dirty.write_bytes(b"// locally changed\n")
            code, _, output = self.run_preflight(self.changed(provenance=target), "-WriteProvenance")
        finally:
            dirty.write_bytes(original)
        self.assertEqual(code, 0, output)
        manifest = json.loads(Path(target).read_text(encoding="utf-8"))
        self.assertEqual(manifest["kind"], "leeroy-dojo-provenance")
        self.assertEqual(manifest["baseline"]["core"]["head"], git(self.live_core, "rev-parse", "HEAD"))
        self.assertEqual(manifest["isolated"]["core"]["head"], git(self.core, "rev-parse", "HEAD"))
        pb = manifest["baseline"]["playerbots"]
        self.assertEqual(pb["dirty_count"], 1)
        expected = "sha256:" + hashlib.sha256(b"// locally changed\n").hexdigest()
        self.assertEqual(pb["dirty"][0], {"status": " M", "path": "src/Bot/PlayerbotAI.cpp", "fingerprint": expected})
        self.assert_no_secrets(Path(target).read_text(encoding="utf-8"))

    def test_rejects_a_core_that_does_not_descend_from_the_baseline(self):
        target = (self.tmp / "dd3" / "provenance.json").as_posix()
        layout = self.changed(provenance=target)
        write(self.live_core / "src" / "server" / "later.txt", "a commit the dojo core never saw\n")
        git(self.live_core, "add", "-A")
        git(self.live_core, "commit", "-q", "-m", "later")
        try:
            self.assertEqual(self.run_preflight(layout, "-WriteProvenance")[0], 0)
            code, report, _ = self.run_preflight(layout)
        finally:
            git(self.live_core, "reset", "-q", "--hard", "HEAD~1")
        self.assertEqual(code, 1)
        self.assertIn("baseline", self.failures(report)["baseline core"])

    def test_unreadable_layout_exits_2(self):
        path = self.tmp / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        result = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT), "-Layout", str(path)],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 2)
        self.assertIn("FAIL layout", result.stdout)


class PreflightSourceTest(unittest.TestCase):
    def test_never_starts_or_connects_to_anything(self):
        text = SCRIPT.read_text(encoding="utf-8").lower()
        for forbidden in ("start-process", "invoke-expression", "mysql.exe", "& mysql", "worldserver.exe",
                          "authserver.exe", "ac-server.sh", "ac-build.sh", "new-object system.net.sockets"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
