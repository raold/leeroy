"""scripts/dojo/preflight.ps1 against a synthetic install. Needs pwsh and git; skipped without them.

Everything lives in a temporary directory: a fake live install (its own git repos, configs with fake credentials, a
Data folder) next to a dojo layout derived from it. Nothing here reads the real install.

Links are junctions on Windows and directory symlinks elsewhere, so the Linux CI job exercises the symlink paths
only; the junction paths (Get-Item LinkType 'Junction', ResolveLinkTarget on a junction) run only where these tests
run on Windows. GitHub's Linux runners cannot create a junction.
"""

import contextlib
import copy
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "dojo" / "preflight.ps1"
PWSH = shutil.which("pwsh")
GIT = shutil.which("git")

# The parts of mod-dojo the preflight checks: a .cmake and a loader named after the folder, shipped off.
DOJO_MODULE_FILES = {
    "mod-dojo.cmake": "# dojo\n",
    "src/mod_dojo_loader.cpp": "void AddSC_dojo_world();\n\nvoid Addmod_dojoScripts()\n{\n    AddSC_dojo_world();\n}\n",
    "conf/mod_dojo.conf.dist": "[worldserver]\nDojo.Enable = 0\n",
}

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


def db(user: str, pw: str, schema: str, host: str = "127.0.0.1", port: int | str = 3306) -> str:
    return f'"{host};{port};{user};{pw};{schema}"'


def snapshot(*roots: Path) -> dict[str, tuple[int, str]]:
    """Every file under roots (git internals included) by size and SHA-256, and every directory."""
    seen: dict[str, tuple[int, str]] = {}
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            seen[dirpath] = (-1, "dir")
            for name in filenames:
                path = Path(dirpath) / name
                data = path.read_bytes()
                seen[str(path)] = (len(data), hashlib.sha256(data).hexdigest())
    return seen


@unittest.skipUnless(PWSH and GIT, "needs pwsh and git")
class PreflightTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="dojo-preflight-"))
        cls.links: list[Path] = []
        t = cls.tmp
        live = t / "live"
        cls.live = live
        make_repo(live, {"README.md": "live install\n", ".gitignore": "*\n!README.md\n!.gitignore\n"})
        cls.live_core = live / "azerothcore-wotlk"
        make_repo(cls.live_core, {"src/server/README": "core\n", "modules/.gitkeep": "",
                                  ".gitignore": "modules/mod-playerbots/\n"})
        cls.live_pb = cls.live_core / "modules" / "mod-playerbots"
        make_repo(cls.live_pb, {"src/Bot/PlayerbotAI.cpp": "// bots\n", "mod-playerbots.cmake": "# bots\n",
                                "src/Script/playerbots_loader.cpp": "void Addmod_playerbotsScripts() { }\n"})
        write(live / "build" / "CMakeCache.txt", f"CMAKE_HOME_DIRECTORY:INTERNAL={cls.live_core.as_posix()}\n")
        configs = live / "build" / "bin" / "RelWithDebInfo" / "configs"
        cls.live_configs = configs
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
        cls.source = source
        cls.dojo_module = source / "modules" / "mod-dojo"
        for rel, text in DOJO_MODULE_FILES.items():
            write(cls.dojo_module / rel, text)
        (source / "dojo" / "leeroy_dojo").mkdir(parents=True)
        cls.core = t / "core"
        cls.pb = t / "pb"
        git(t, "clone", "-q", str(cls.live_core), str(cls.core))
        git(t, "clone", "-q", str(cls.live_pb), str(cls.pb))
        cls.link(cls.pb, cls.core / "modules" / "mod-playerbots")
        cls.link(source / "modules" / "mod-dojo", cls.core / "modules" / "mod-dojo")

        runtime = t / "rt" / "instances" / "t1"
        cls.runtime = runtime
        (t / "rt" / "build").mkdir(parents=True)
        write(runtime / "dojo.marker", json.dumps({"marker": "leeroy-dojo-training", "instance": "t1"}))
        world, auth = free_ports(2)
        cls.world, cls.auth = world, auth
        cls.train_configs = runtime / "configs"
        write(cls.train_configs / "worldserver.conf", "\n".join([
            f"LoginDatabaseInfo     = {db(TRAIN_USER, TRAIN_PW, 'dojo_auth')}",
            f"WorldDatabaseInfo     = {db(TRAIN_USER, TRAIN_PW, 'dojo_world')}",
            f"CharacterDatabaseInfo = {db(TRAIN_USER, TRAIN_PW, 'dojo_characters')}",
            f'SourceDirectory = "{cls.core.as_posix()}"',
            f'DataDir = "{cls.assets.as_posix()}"',
            f'LogsDir = "{(runtime / "logs").as_posix()}"',
            "MapUpdate.Threads = 0",
            f"WorldServerPort = {world}", ""]))
        write(cls.train_configs / "authserver.conf",
              f"LoginDatabaseInfo = {db(TRAIN_USER, TRAIN_PW, 'dojo_auth')}\nRealmServerPort = {auth}\n")
        write(cls.train_configs / "modules" / "playerbots.conf",
              f"Playerbots.DatabaseInfo = {db(TRAIN_USER, TRAIN_PW, 'dojo_playerbots')}\n")

        cls.data = t / "dd"
        cls.layout = {
            "format": 1,
            "marker": "leeroy-dojo-training",
            "instance": "t1",
            "roots": {"source": source.as_posix(), "core": cls.core.as_posix(), "playerbots": cls.pb.as_posix(),
                      "build": (t / "rt" / "build").as_posix(), "runtime": runtime.as_posix(),
                      "data": cls.data.as_posix()},
            "provenance": (cls.data / "manifests" / "provenance.json").as_posix(),
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
    def run_preflight(cls, layout: dict, *extra: str, as_json: bool = True, env: dict | None = None):
        cls.runs += 1
        path = cls.tmp / f"layout-{cls.runs}.json"
        path.write_text(json.dumps(layout), encoding="utf-8")
        args = [PWSH, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT), "-Layout", str(path), *extra]
        if as_json:
            args.append("-Json")
        result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=180, env=env)
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

    @staticmethod
    @contextlib.contextmanager
    def with_file(path: Path, text: str | None):
        """Sets path's text (None removes it) for the block, then puts the original back."""
        original = path.read_bytes() if path.exists() else None
        if text is None:
            path.unlink(missing_ok=True)
        else:
            write(path, text)
        try:
            yield
        finally:
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)

    def test_accepts_the_synthetic_layout(self):
        code, report, output = self.run_preflight(self.layout)
        self.assertEqual(self.failures(report), {}, output)
        self.assertEqual(code, 0)
        self.assertTrue(report["ok"])
        checks = {c["check"]: c for c in report["checks"]}
        for name in ("production", "root source", "root core", "root runtime", "module mod-dojo",
                     "module mod-playerbots", "links core", "links module mod-dojo", "marker", "provenance",
                     "baseline core", "baseline playerbots", "assets", "port world", "port auth",
                     "config WorldServerPort", "config RealmServerPort", "database layout world",
                     "database training worldserver.conf WorldDatabaseInfo",
                     "database training authserver.conf LoginDatabaseInfo", "config SourceDirectory",
                     "toolchain git"):
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

    def test_rejects_a_root_that_contains_the_live_install(self):
        """The reviewer's probe: a data root above the live install used to pass (the check ran one way only)."""
        for root, value in (("data", self.tmp.as_posix()), ("runtime", self.live.parent.as_posix()),
                            ("build", self.live.as_posix() + "/build/..")):
            code, report, _ = self.run_preflight(self.changed(**{f"roots__{root}": value}))
            self.assertEqual(code, 1, root)
            detail = self.failures(report)[f"root {root}"]
            self.assertTrue("contains the live install" in detail or "live install checkout" in detail, detail)
        code, report, _ = self.run_preflight(self.changed(roots__data=(self.live / "build" / "bin").as_posix()))
        self.assertIn("contains the production configs", self.failures(report)["root data"])

    def test_rejects_the_production_config_directory(self):
        layout = self.changed(roots__configs=self.layout["production"]["configs"])
        code, report, output = self.run_preflight(layout)
        self.assertEqual(code, 1)
        self.assertIn("production configuration", self.failures(report)["config target"])
        self.assert_no_secrets(output)

    def test_rejects_a_config_that_loads_sql_from_the_live_core(self):
        conf = self.train_configs / "worldserver.conf"
        original = conf.read_text(encoding="utf-8")
        with self.with_file(conf, original.replace(self.core.as_posix(), self.live_core.as_posix())):
            code, report, _ = self.run_preflight(self.layout)
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

    def test_rejects_a_link_nested_inside_a_module_or_the_core(self):
        """The reviewer's probe: mod-dojo/src as a junction into the live mod-leeroy used to pass."""
        for label, link, target in (
                ("link module mod-dojo", self.dojo_module / "src" / "Runtime", self.live / "modules" / "mod-leeroy"),
                ("link module mod-playerbots", self.pb / "src" / "live", self.live_pb / "src"),
                ("link core", self.core / "src" / "server" / "shared", self.live_core / "src")):
            with self.subTest(label):
                link_dir(target, link)
                try:
                    code, report, _ = self.run_preflight(self.layout)
                finally:
                    unlink_dir(link)
                self.assertEqual(code, 1)
                self.assertIn("leaves the isolated checkouts", self.failures(report)[label])
        inside = self.source / "modules" / "mod-dojo" / "shared"
        link_dir(self.source / "dojo", inside)
        try:
            code, report, output = self.run_preflight(self.layout)
        finally:
            unlink_dir(inside)
        self.assertEqual((code, self.failures(report)), (0, {}), "a link within the isolated checkouts is fine")

    def test_rejects_module_files_that_do_not_follow_the_folder_name(self):
        """CLAUDE.md build trap 1: CMake includes only <dir>/<dir>.cmake and the loader calls Add<dir>Scripts(), so a
        .cmake or loader named for another folder is skipped silently or fails to link."""
        dojo = self.dojo_module
        loader = dojo / "src" / "mod_dojo_loader.cpp"
        cases = (
            ("module rules mod-dojo", {dojo / "mod-dojo.cmake": None, dojo / "mod-dojo-master.cmake": "# dojo\n"},
             "would be skipped silently"),
            ("module rules mod-dojo", {dojo / "mod-dojo.cmake": None}, "has no mod-dojo.cmake"),
            ("module rules mod-dojo", {loader: "void Addmod_dojo_masterScripts()\n{\n}\n"},
             "defines no Addmod_dojoScripts()"),
            ("module rules mod-dojo", {loader: "void Addmod_dojoScripts();\n"}, "defines no Addmod_dojoScripts()"),
            ("module rules mod-playerbots", {self.pb / "src" / "Script" / "playerbots_loader.cpp": None},
             "defines no Addmod_playerbotsScripts()"),
        )
        for check, files, words in cases:
            with self.subTest(check=check, words=words, files=[p.name for p in files]):
                with contextlib.ExitStack() as stack:
                    for path, text in files.items():
                        stack.enter_context(self.with_file(path, text))
                    code, report, output = self.run_preflight(self.layout)
                self.assertEqual(code, 1, output)
                self.assertIn(words, self.failures(report).get(check, ""), output)
        code, report, output = self.run_preflight(self.layout)
        self.assertEqual((code, self.failures(report)), (0, {}), output)

    def test_rejects_a_mod_dojo_that_ships_on(self):
        dist = self.dojo_module / "conf" / "mod_dojo.conf.dist"
        for text in ("[worldserver]\nDojo.Enable = 1\n", "[worldserver]\n", None):
            with self.subTest(text=text):
                with self.with_file(dist, text):
                    code, report, _ = self.run_preflight(self.layout)
                self.assertEqual(code, 1)
                self.assertIn("does not ship Dojo.Enable = 0", self.failures(report)["module rules mod-dojo"])

    def test_configure_and_build_refuse_what_the_preflight_refuses(self):
        """Both stop before CMake when the preflight fails, here on the live build tree as roots.build."""
        layout_path = self.tmp / "layout-refused.json"
        layout_path.write_text(json.dumps(self.changed(roots__build=self.layout["production"]["build"])),
                               encoding="utf-8")
        before = snapshot(self.live, self.tmp / "rt")
        for script in ("configure.ps1", "build.ps1"):
            with self.subTest(script=script):
                result = subprocess.run(
                    [PWSH, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT.parent / script), "-Layout",
                     str(layout_path)], capture_output=True, text=True, encoding="utf-8", timeout=300)
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 1, output)
                expected = "preflight did not pass" if os.name == "nt" else "Windows-only"
                self.assertIn(expected, output)
                self.assert_no_secrets(output)
        self.assertEqual(snapshot(self.live, self.tmp / "rt"), before)

    @unittest.skipUnless(os.name == "nt", "the dojo build is Windows-only")
    def test_build_refuses_a_tree_configure_did_not_make(self):
        layout_path = self.tmp / "layout-unconfigured.json"
        layout_path.write_text(json.dumps(self.layout), encoding="utf-8")
        result = subprocess.run(
            [PWSH, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT.parent / "build.ps1"), "-Layout",
             str(layout_path)], capture_output=True, text=True, encoding="utf-8", timeout=300)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("run configure.ps1 first", result.stdout)
        self.assertEqual(list((self.tmp / "rt" / "build").iterdir()), [])

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

    def test_refuses_a_live_install_it_cannot_identify(self):
        """The reviewer's probe: a misspelt production.core used to make the live core acceptable as roots.core."""
        layout = self.changed(production__core=(self.live / "typo-core").as_posix(), roots__core=self.live_core.as_posix())
        code, report, _ = self.run_preflight(layout)
        self.assertEqual(code, 1)
        failures = self.failures(report)
        self.assertIn("does not exist", failures["production core"])
        self.assertIn("does not name production.core", failures["production build"])
        cache = self.live / "build" / "CMakeCache.txt"
        with self.with_file(cache, None):
            code, report, _ = self.run_preflight(self.layout)
        self.assertIn("not the live build", self.failures(report)["production build"])
        for name in ("install_root", "build", "configs"):
            layout = copy.deepcopy(self.layout)
            del layout["production"][name]
            code, report, _ = self.run_preflight(layout)
            self.assertEqual(code, 1, name)
            self.assertIn(f"production {name}", self.failures(report))

    def test_rejects_writable_live_database_targets(self):
        code, report, output = self.run_preflight(self.changed(database__schemas__world="acore_world"))
        self.assertEqual(code, 1)
        self.assertIn("writable live database target", self.failures(report)["database layout world"])
        code, report, output2 = self.run_preflight(self.changed(database__user=LIVE_USER))
        self.assertEqual(code, 1)
        self.assertIn("live server's database user", self.failures(report)["database layout auth"])
        self.assert_no_secrets(output + output2)

    def test_refuses_when_the_live_targets_cannot_be_read(self):
        """The reviewer's probe: no readable production.configs and no production.databases let acore_world pass."""
        conf = self.train_configs / "worldserver.conf"
        original = conf.read_text(encoding="utf-8")
        layout = self.changed(database__schemas__world="acore_world")
        layout["production"]["configs"] = (self.live / "no-such-configs").as_posix()
        layout["production"]["databases"] = []
        with self.with_file(conf, original.replace("dojo_world", "acore_world")):
            code, report, output = self.run_preflight(layout)
        self.assertEqual(code, 1)
        failures = self.failures(report)
        self.assertIn("database live targets", failures)
        self.assertIn("production configs", failures)
        pipe = f'WorldDatabaseInfo = ".;MySQL;{LIVE_USER};{LIVE_PW};acore_world"\n'
        live_world = self.live_configs / "worldserver.conf"
        live_original = live_world.read_text(encoding="utf-8")
        with self.with_file(live_world, live_original.replace(f"WorldDatabaseInfo     = {db(LIVE_USER, LIVE_PW, 'acore_world')}\n", pipe)):
            code, report, output2 = self.run_preflight(self.layout)
        self.assertEqual(code, 1)
        self.assertIn("cannot be read", self.failures(report)["database production worldserver.conf WorldDatabaseInfo"])
        self.assert_no_secrets(output + output2)

    def test_this_machine_by_any_name_is_the_live_server(self):
        """The reviewer's probe: the host name (or a LAN address of this machine) with acore_world used to pass."""
        names = [socket.gethostname()]
        try:
            names += [a for a in socket.gethostbyname_ex(socket.gethostname())[2] if not a.startswith("127.")][:1]
        except OSError:
            pass
        for host in names + ["localhost", "127.0.0.2", "[::1]"]:
            code, report, _ = self.run_preflight(self.changed(database__schemas__world="acore_world",
                                                              database__host=host))
            self.assertEqual(code, 1, host)
            self.assertIn("database layout world", self.failures(report), host)
        code, report, _ = self.run_preflight(self.changed(database__host="192.0.2.1"))
        self.assertIn("not on loopback", self.failures(report)["database layout auth"])
        conf = self.train_configs / "worldserver.conf"
        original = conf.read_text(encoding="utf-8")
        with self.with_file(conf, original.replace(f'"127.0.0.1;3306;{TRAIN_USER};{TRAIN_PW};dojo_characters"',
                                                   f'"{socket.gethostname()};3306;{TRAIN_USER};{TRAIN_PW};acore_characters"')):
            code, report, output = self.run_preflight(self.layout)
        self.assertEqual(code, 1)
        self.assertIn("database training worldserver.conf CharacterDatabaseInfo", self.failures(report))
        self.assert_no_secrets(output)

    def test_rejects_a_live_target_in_the_training_config(self):
        conf = self.train_configs / "worldserver.conf"
        original = conf.read_text(encoding="utf-8")
        with self.with_file(conf, original.replace("dojo_characters", "acore_characters")):
            code, report, output = self.run_preflight(self.layout)
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

    def test_reads_the_ports_the_training_configs_would_bind(self):
        """The reviewer's probes: a training conf on a live port passed when the layout did not name that port, and
        authserver.conf (where RealmServerPort lives) was never read."""
        world = self.train_configs / "worldserver.conf"
        auth = self.train_configs / "authserver.conf"
        world_text = world.read_text(encoding="utf-8")
        auth_text = auth.read_text(encoding="utf-8")
        layout = copy.deepcopy(self.layout)
        del layout["ports"]["world"]
        cases = [
            (world, world_text.replace(f"WorldServerPort = {self.world}", "WorldServerPort = 8085"), layout,
             "config WorldServerPort", "production port"),
            (auth, auth_text.replace(f"RealmServerPort = {self.auth}", "RealmServerPort = 3724"), self.layout,
             "config RealmServerPort", "production port"),
            (world, world_text.replace(f"WorldServerPort = {self.world}\n", ""), self.layout,
             "config WorldServerPort", "the default 8085"),
            (auth, auth_text.replace(f"RealmServerPort = {self.auth}\n", ""), self.layout,
             "config RealmServerPort", "the default 3724"),
            (world, world_text + "SOAP.Enabled = 1\n", self.layout, "config SOAP.Port", "the default 7878"),
            (auth, auth_text.replace(f"RealmServerPort = {self.auth}", f"RealmServerPort = {self.world}"), self.layout,
             "config RealmServerPort", "also WorldServerPort"),
        ]
        for path, text, lay, check, words in cases:
            with self.subTest(check=check, words=words):
                with self.with_file(path, text):
                    code, report, _ = self.run_preflight(lay)
                self.assertEqual(code, 1)
                self.assertIn(words, self.failures(report).get(check, ""))
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        try:
            port = busy.getsockname()[1]
            layout = copy.deepcopy(self.layout)
            del layout["ports"]["auth"]
            with self.with_file(auth, auth_text.replace(f"RealmServerPort = {self.auth}", f"RealmServerPort = {port}")):
                code, report, _ = self.run_preflight(layout)
        finally:
            busy.close()
        self.assertIn("already in use", self.failures(report)["config RealmServerPort"])

    def test_rejects_a_missing_provenance_manifest(self):
        layout = self.changed(provenance=(self.data / "nowhere" / "provenance.json").as_posix())
        code, report, _ = self.run_preflight(layout)
        self.assertEqual(code, 1)
        self.assertIn("missing provenance manifest", self.failures(report)["provenance"])

    def test_write_provenance_never_writes_outside_the_data_root(self):
        """The reviewer's probe: -WriteProvenance used to overwrite <production configs>/worldserver.conf."""
        victim = self.live_configs / "worldserver.conf"
        before = victim.read_bytes()
        stray = self.data / "notes.txt"
        write(stray, "the owner's notes\n")
        try:
            for target, words in ((victim, "not inside the data root"), (self.live / "mind-data" / "p.json", "not inside"),
                                  (stray, "is not a provenance manifest"), (self.data, "not inside the data root"),
                                  (self.data / "manifests", "is a directory")):
                with self.subTest(target=target.name):
                    code, report, output = self.run_preflight(self.changed(provenance=target.as_posix()),
                                                              "-WriteProvenance")
                    self.assertEqual(code, 1, output)
                    self.assertIn(words, self.failures(report)["provenance path"])
                    self.assertIn("not written", self.failures(report)["provenance"])
            self.assertEqual(victim.read_bytes(), before)
            self.assertEqual(stray.read_text(encoding="utf-8"), "the owner's notes\n")
            self.assertFalse((self.live / "mind-data" / "p.json").exists())
        finally:
            stray.unlink()

    def test_write_provenance_waits_for_every_root_check(self):
        target = self.data / "manifests" / "refused.json"
        for layout in (self.changed(roots__build=self.layout["production"]["build"], provenance=target.as_posix()),
                       self.changed(roots__data=self.tmp.as_posix(),
                                    provenance=(self.tmp / "manifests" / "refused.json").as_posix()),
                       self.changed(production__core=(self.live / "typo").as_posix(), provenance=target.as_posix())):
            code, report, output = self.run_preflight(layout, "-WriteProvenance")
            self.assertEqual(code, 1, output)
            self.assertIn("not written", self.failures(report)["provenance"])
        self.assertFalse(target.exists())
        self.assertFalse((self.tmp / "manifests").exists(), "no directory was created either")

    def test_provenance_records_baselines_and_dirty_fingerprints(self):
        dirty = self.live_pb / "src" / "Bot" / "PlayerbotAI.cpp"
        original = dirty.read_bytes()
        target = (self.data / "manifests" / "dirty.json").as_posix()
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
        code, _, output = self.run_preflight(self.changed(provenance=target), "-WriteProvenance")
        self.assertEqual(code, 0, "an existing manifest may be replaced")

    def test_rejects_a_core_that_does_not_descend_from_the_baseline(self):
        target = (self.data / "manifests" / "later.json").as_posix()
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

    def test_changes_nothing_outside_the_data_root(self):
        roots = (self.live, self.core, self.pb, self.source, self.tmp / "rt")
        before = snapshot(*roots)
        target = (self.data / "manifests" / "quiet.json").as_posix()
        self.assertEqual(self.run_preflight(self.changed(provenance=target), "-WriteProvenance")[0], 0)
        self.run_preflight(self.changed(provenance=target))
        self.run_preflight(self.changed(database__schemas__world="acore_world"), "-WriteProvenance")
        self.assertEqual(snapshot(*roots), before)

    def test_never_starts_or_connects_to_anything(self):
        """The only programs on PATH are logging shims; every database and game endpoint is a listener that must
        see no connection. git may only read; cmake and Python may only report a version."""
        shims = self.tmp / "shims"
        shims.mkdir(exist_ok=True)
        log = self.tmp / "shims.log"
        log.unlink(missing_ok=True)
        fake = {"cmake": "cmake version 0.0-shim", "py": "Python 3.12.0", "python3.12": "Python 3.12.0",
                "uv": "/shim/python3.12"}
        if os.name == "nt":
            for name, line in fake.items():
                (shims / f"{name}.cmd").write_text(
                    f'@echo off\r\n>>"{log}" echo {name} %*\r\necho {line}\r\nexit /b 0\r\n', encoding="utf-8")
            (shims / "git.cmd").write_text(
                f'@echo off\r\n>>"{log}" echo git %*\r\n"{GIT}" %*\r\nexit /b %errorlevel%\r\n', encoding="utf-8")
        else:
            for name, line in fake.items():
                path = shims / name
                path.write_text(f"#!/bin/sh\nprintf '%s\\n' \"{name} $*\" >> '{log}'\necho '{line}'\n", encoding="utf-8")
                path.chmod(path.stat().st_mode | stat.S_IEXEC)
            path = shims / "git"
            path.write_text(f"#!/bin/sh\nprintf '%s\\n' \"git $*\" >> '{log}'\nexec '{GIT}' \"$@\"\n", encoding="utf-8")
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        env = {k: v for k, v in os.environ.items() if k.upper() != "PATH"}
        env["PATH"] = str(shims)
        if os.name == "nt":
            env["ProgramFiles(x86)"] = str(self.tmp / "no-program-files")

        listeners = []
        try:
            for _ in range(3):
                s = socket.socket()
                s.bind(("127.0.0.1", 0))
                s.listen(8)
                s.setblocking(False)
                listeners.append(s)
            db_port, world, auth = (s.getsockname()[1] for s in listeners)
            layout = self.changed(database__port=db_port, ports__world=world, ports__auth=auth)
            layout["production"]["databases"] = [{"host": "127.0.0.1", "port": db_port, "schema": "acore_world"}]
            files = {p: p.read_text(encoding="utf-8") for p in (
                self.train_configs / "worldserver.conf", self.train_configs / "authserver.conf",
                self.train_configs / "modules" / "playerbots.conf", self.live_configs / "worldserver.conf",
                self.live_configs / "authserver.conf", self.live_configs / "modules" / "playerbots.conf")}
            try:
                for path, text in files.items():
                    path.write_text(text.replace(";3306;", f";{db_port};").replace(
                        f"WorldServerPort = {self.world}", f"WorldServerPort = {world}").replace(
                        f"RealmServerPort = {self.auth}", f"RealmServerPort = {auth}"), encoding="utf-8")
                results = [self.run_preflight(layout, env=env),
                           self.run_preflight(self.changed(provenance=(self.data / "m" / "shim.json").as_posix()),
                                              "-WriteProvenance", env=env)]
            finally:
                for path, text in files.items():
                    path.write_text(text, encoding="utf-8")
            for listener in listeners:
                with self.assertRaises(BlockingIOError, msg="the preflight connected to a listener"):
                    listener.accept()[0].close()
        finally:
            for s in listeners:
                s.close()

        for code, report, output in results:
            self.assertIsNotNone(report, output)
            self.assertNotIn("is not recognized", output)
            self.assertNotIn("CommandNotFound", output)
        self.assertEqual(results[1][0], 0, results[1][2])
        calls = log.read_text(encoding="utf-8").splitlines()
        self.assertTrue(calls)
        allowed_git = {"rev-parse", "remote", "status", "merge-base"}
        for call in calls:
            words = call.split()
            tool, args = words[0], words[1:]
            if tool == "git":
                if args == ["--version"]:
                    continue
                self.assertEqual(args[:2], ["-c", "core.fsmonitor=false"], call)
                self.assertEqual(args[2], "-C", call)
                self.assertIn(args[4], allowed_git, call)
                if args[4] == "remote":
                    self.assertIn(args[5:6], ([], ["get-url"]), call)
            else:
                self.assertIn(tool, fake, call)
                self.assertIn(args, (["--version"], ["-3.12", "--version"], ["python", "find", "3.12"]), call)


class PreflightSourceTest(unittest.TestCase):
    def test_names_no_server_or_database_client(self):
        text = SCRIPT.read_text(encoding="utf-8").lower()
        for forbidden in ("start-process", "invoke-expression", "mysql.exe", "& mysql", "worldserver.exe",
                          "authserver.exe", "ac-server.sh", "ac-build.sh", "new-object system.net.sockets",
                          "system.net.sockets", "dns]::gethostentry", "dns]::resolve", "invoke-webrequest",
                          "invoke-restmethod"):
            self.assertNotIn(forbidden, text)

    def test_configure_and_build_run_only_the_build_tools(self):
        """D03: neither uses the production ac-build.sh or ac-server.sh, a database client or a server. The only
        programs they run are the preflight, cmake, git, vswhere and dumpbin."""
        for name in ("configure.ps1", "build.ps1"):
            text = (SCRIPT.parent / name).read_text(encoding="utf-8")
            calls = set(re.findall(r"(?m)(?:^|[\s(=])&\s+(\$?[\w.-]+)", text))
            self.assertTrue(calls, name)
            self.assertLessEqual(calls, {"pwsh", "cmake", "git", "$vswhere", "$dumpbin"}, name)
            self.assertLessEqual(set(re.findall(r"Start-Process\s+-FilePath\s+'([^']+)'", text)), {"cmake"}, name)
            lowered = text.lower()
            for forbidden in ("invoke-expression", "mysql.exe", "ac-server", "system.net.sockets", "bash"):
                self.assertNotIn(forbidden, lowered, name)


if __name__ == "__main__":
    unittest.main()
