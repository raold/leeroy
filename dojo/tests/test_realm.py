"""The training realm tooling, without a database or a server: layout guards, the MySQL instance's files and
statements, the training rules, accounts, RA and the side-effect comparison. Nothing here starts a process or
connects anywhere but a loopback socket this test owns."""

import copy
import json
import os
import shutil
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from leeroy_dojo.realm import acceptance, checks, conf, layout, mysql, provision, srp6, world
from leeroy_dojo.realm.ra import RaClient, RaError

REPO = Path(__file__).resolve().parents[2]
RULES = REPO / "scripts" / "dojo" / "training-rules.json"
LIVE_PW = "livepw-4q8z"


class Synthetic:
    """A temporary live install (configs only) and a dojo layout derived from it."""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dojo-realm-"))
        t = self.tmp
        self.live_configs = t / "live" / "build" / "bin" / "RelWithDebInfo" / "configs"
        (self.live_configs / "modules").mkdir(parents=True)
        info = lambda s: f'"127.0.0.1;3306;liveuser;{LIVE_PW};{s}"'  # noqa: E731
        (self.live_configs / "worldserver.conf").write_text(
            f"LoginDatabaseInfo = {info('acore_auth')}\nWorldDatabaseInfo = {info('acore_world')}\n"
            f"CharacterDatabaseInfo = {info('acore_characters')}\nWorldServerPort = 8085\nRa.Port = 3443\n",
            encoding="utf-8")
        (self.live_configs / "modules" / "playerbots.conf").write_text(
            f"PlayerbotsDatabaseInfo = {info('acore_playerbots')}\n", encoding="utf-8")
        self.data = {
            "format": 1, "marker": "leeroy-dojo-training", "instance": "t1",
            "roots": {k: (t / k).as_posix() for k in ("source", "core", "playerbots", "build", "runtime", "data")},
            "assets": (t / "live" / "Data").as_posix(),
            "database": {"host": "127.0.0.1", "port": 13306, "user": "dojo",
                         "schemas": {"auth": "dojo_auth", "characters": "dojo_characters", "world": "dojo_world",
                                     "playerbots": "dojo_playerbots"},
                         "server": {"basedir": (t / "mysql84").as_posix(), "root": (t / "mysql").as_posix(),
                                    "buffer_pool": "256M"}},
            "ports": {"auth": 13724, "world": 18085, "ra": 13443, "dojo_host": "127.0.0.1", "dojo": 0},
            "production": {"install_root": (t / "live").as_posix(), "core": (t / "live" / "core").as_posix(),
                           "build": (t / "live" / "build").as_posix(), "configs": self.live_configs.as_posix(),
                           "modules": (t / "live" / "modules").as_posix(),
                           "protected": [(t / "live" / "mind-data").as_posix()],
                           "databases": [{"host": "127.0.0.1", "port": 3306, "schema": "acore_world"}],
                           "ports": [3306, 33060, 3724, 8085, 7878]},
        }

    def write(self, data: dict | None = None) -> Path:
        path = self.tmp / f"layout-{os.urandom(4).hex()}.json"
        path.write_text(json.dumps(data or self.data), encoding="utf-8")
        return path

    def load(self, **edits) -> layout.Layout:
        data = copy.deepcopy(self.data)
        for dotted, value in edits.items():
            node = data
            keys = dotted.split("__")
            for k in keys[:-1]:
                node = node[k]
            node[keys[-1]] = value
        return layout.load(self.write(data))

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class LayoutTest(unittest.TestCase):
    def setUp(self):
        self.s = Synthetic()

    def tearDown(self):
        self.s.close()

    def test_loads_and_reads_live_endpoints_without_keeping_passwords(self):
        lay = self.s.load()
        self.assertEqual(lay.db_port, 13306)
        self.assertIn(3306, lay.live_ports)
        self.assertIn(8085, lay.live_ports)
        self.assertIn("acore_playerbots", lay.live_schemas)   # read from PlayerbotsDatabaseInfo
        self.assertNotIn(LIVE_PW, repr(lay))

    def test_refuses_what_would_reach_the_live_server(self):
        bad = {
            "database__port": 3306,
            "database__host": "192.0.2.1",
            "database__user": "root",
            "database__schemas__world": "acore_world",
            "database__schemas__auth": "acore_extra",
            "database__schemas__characters": "dojo_world",
            "ports__world": 8085,
            "ports__ra": 3443,
            "ports__auth": 13306,
            "marker": "x",
            "instance": "a b",
            "roots__source": "relative",
            "database__server__buffer_pool": "8G",
        }
        for key, value in bad.items():
            with self.subTest(key), self.assertRaises(layout.LayoutError):
                self.s.load(**{key: value})

    def test_server_root_must_stay_out_of_live_areas(self):
        t = self.s.tmp
        for root in ((t / "live" / "build" / "data").as_posix(), (t / "live" / "mind-data" / "db").as_posix(),
                     t.as_posix(), (t / "source" / "mysql").as_posix()):
            with self.subTest(root), self.assertRaises(layout.LayoutError):
                self.s.load(database__server__root=root)

    def test_db_target_guard(self):
        lay = self.s.load()
        lay.check_db_target("127.0.0.1", 13306, "dojo_world")
        for host, port, schema in (("127.0.0.1", 3306, None), ("10.0.0.5", 13306, None),
                                   ("127.0.0.1", 13307, None), ("127.0.0.1", 13306, "acore_world")):
            with self.subTest((host, port, schema)), self.assertRaises(layout.LayoutError):
                lay.check_db_target(host, port, schema)


class MysqlFilesTest(unittest.TestCase):
    def setUp(self):
        self.s = Synthetic()
        self.lay = self.s.load()

    def tearDown(self):
        self.s.close()

    def test_my_ini_is_loopback_on_its_own_port_and_datadir(self):
        text = mysql.render_my_ini(self.lay.server)
        self.assertEqual(mysql.check_my_ini(self.lay.server, text), [])
        for needle in ("port=13306", "bind-address=127.0.0.1", "mysqlx=OFF", "named-pipe=OFF", "shared-memory=OFF",
                       "disable-log-bin", "performance-schema=OFF", "innodb-buffer-pool-size=256M"):
            self.assertIn(needle, text)
        self.assertNotRegex(text.lower(), r"(?m)^s*passwords*=")

    def test_my_ini_check_finds_each_problem(self):
        good = mysql.render_my_ini(self.lay.server)
        for old, new, why in (("port=13306", "port=3306", "port"), ("bind-address=127.0.0.1", "bind-address=0.0.0.0",
                              "bind-address"), ("mysqlx=OFF", "mysqlx=ON", "X plugin"),
                              ("named-pipe=OFF", "named-pipe=ON", "named-pipe"), ("/mysql/data", "/other/data", "datadir")):
            with self.subTest(why):
                problems = mysql.check_my_ini(self.lay.server, good.replace(old, new))
                self.assertTrue(any(why in p for p in problems), problems)

    def test_grants_escape_wildcards(self):
        self.assertEqual(mysql.grant_db_pattern("dojo_auth"), "`dojo\\_auth`")
        with self.assertRaises(mysql.MysqlError):
            mysql.grant_db_pattern("dojo`; DROP")

    def test_bootstrap_creates_only_dojo_schemas_and_a_restricted_user(self):
        sql = mysql.bootstrap_sql(self.lay, "rootpw1", "dojopw2")
        self.assertIn("GRANT ALL PRIVILEGES ON *.* TO 'root'@'127.0.0.1'", sql)
        self.assertEqual(sql.count("TO 'dojo'@'127.0.0.1'"), 4)
        for schema in self.lay.schemas.values():
            self.assertIn(f"CREATE DATABASE `{schema}`", sql)
            self.assertIn(f"ON {mysql.grant_db_pattern(schema)}.* TO 'dojo'", sql)
        self.assertNotIn("acore", sql)
        self.assertNotIn("ON *.* TO 'dojo'", sql)

    def test_literals_refuse_quotes(self):
        self.assertEqual(mysql.sql_literal("DOJOADMIN"), "'DOJOADMIN'")
        for bad in ("a'b", "a\\b", "a\nb"):
            with self.assertRaises(mysql.MysqlError):
                mysql.sql_literal(bad)

    def test_passwords_are_plain_alphanumerics(self):
        pw = mysql.new_password()
        self.assertEqual(len(pw), 24)
        self.assertTrue(pw.isalnum())
        self.assertNotEqual(pw, mysql.new_password())

    def test_client_never_puts_a_password_on_the_command_line(self):
        cnf = self.s.tmp / "c.cnf"
        pw = mysql.new_password()
        mysql.write_client_cnf(cnf, "dojo", pw, 13306)
        self.assertEqual(mysql.read_client_cnf(cnf)["password"], pw)
        client = mysql.Client(self.lay, cnf)
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="1\t2\n", stderr="")
            self.assertEqual(client.query("SELECT 1, 2;"), [["1", "2"]])
            argv = run.call_args.args[0]
        self.assertTrue(all(pw not in a for a in argv))
        self.assertIn(f"--defaults-file={cnf}", argv)

    def test_client_refuses_a_live_or_foreign_port_before_running_anything(self):
        for port in (3306, 13307):
            cnf = self.s.tmp / f"c{port}.cnf"
            mysql.write_client_cnf(cnf, "dojo", "pw", port)
            with self.subTest(port), mock.patch("subprocess.run") as run, self.assertRaises(mysql.MysqlError):
                mysql.Client(self.lay, cnf).query("SELECT 1;")
            run.assert_not_called()

    def test_client_refuses_a_foreign_schema(self):
        cnf = self.s.tmp / "c.cnf"
        mysql.write_client_cnf(cnf, "dojo", "pw", 13306)
        with mock.patch("subprocess.run") as run, self.assertRaises(mysql.MysqlError):
            mysql.Client(self.lay, cnf).query("SELECT 1;", database="acore_world")
        run.assert_not_called()

    def test_identity_must_be_the_dojo_instance(self):
        client = mock.Mock()
        good = {"port": "13306", "datadir": layout.norm(self.lay.server.datadir), "server_uuid": "u1",
                "bind_address": "127.0.0.1", "version": "8.4", "log_bin": "0", "performance_schema": "0",
                "named_pipe": "0", "shared_memory": "0"}
        with mock.patch.object(mysql, "identity", return_value=good):
            self.assertEqual(mysql.verify_identity(self.lay, client)["server_uuid"], "u1")
        Path(self.lay.server.root).mkdir(parents=True, exist_ok=True)
        self.lay.server.receipt.write_text(json.dumps({"server_uuid": "u1"}), encoding="utf-8")
        for key, value in (("port", "3306"), ("datadir", "C:/ProgramData/MySQL/Data"), ("bind_address", "*"),
                           ("server_uuid", "someone-else")):
            with self.subTest(key), mock.patch.object(mysql, "identity", return_value={**good, key: value}):
                with self.assertRaises(mysql.MysqlError):
                    mysql.verify_identity(self.lay, client)


class RulesTest(unittest.TestCase):
    def test_rules_file_is_consistent(self):
        rules = conf.load_rules(RULES)
        self.assertEqual(set(rules), {"worldserver.conf", "modules/playerbots.conf", "modules/mod_dojo.conf"})
        world_rules = rules["worldserver.conf"]
        self.assertEqual(world_rules.training["MapUpdate.Threads"], "0")
        self.assertEqual(world_rules.training["BindIP"], "127.0.0.1")
        self.assertEqual(world_rules.training["Ra.IP"], "127.0.0.1")
        bots = rules["modules/playerbots.conf"]
        self.assertEqual(bots.training["Playerbots.BotCheats"], "")
        self.assertEqual(bots.training["Playerbots.MaxRandomBots"], "0")
        self.assertEqual(rules["modules/mod_dojo.conf"].training["Dojo.Enable"], "1")
        for spec in json.loads(RULES.read_text(encoding="utf-8"))["files"].values():
            for key, entry in spec["training"].items():
                self.assertTrue(entry["why"], key)

    def test_render_and_check(self):
        rules = conf.FileRules("w.conf", "w.conf.dist", frozenset({"Port"}), {"Threads": "0", "Cheats": ""},
                               {"Rate": "1"})
        dist = '# comment\nThreads = 1\nCheats = "food"\nRate = 1\nPort = 8085\nOther = "x y"\n'
        text = conf.render(dist, {"Threads": "0", "Cheats": "", "Port": "18085"})
        self.assertIn("Threads = 0", text)
        self.assertIn('Cheats = ""', text)
        self.assertIn("# comment", text)
        self.assertEqual(conf.check(rules, conf.parse(text), conf.parse(dist)), [])
        with self.assertRaisesRegex(conf.ConfError, "Missing"):
            conf.render(dist, {"Missing": "1"})
        with self.assertRaises(conf.ConfError):
            conf.render(dist, {"Threads": 'a"b'})

    def test_check_finds_every_kind_of_deviation(self):
        rules = conf.FileRules("w.conf", "w.conf.dist", frozenset({"Port"}), {"Threads": "0"}, {"Rate": "1"})
        dist = conf.parse("Threads = 1\nRate = 1\nPort = 8085\nOther = x\n")
        good = {"Threads": "0", "Rate": "1", "Port": "18085", "Other": "x"}
        self.assertEqual(conf.check(rules, good, dist), [])
        for name, edit, needle in (("training", {"Threads": "1"}, "Threads"), ("ordinary", {"Rate": "2"}, "Rate"),
                                   ("stock", {"Other": "y"}, "Other"), ("unknown", {"Haste": "10"}, "Haste"),
                                   ("missing", {"Other": None}, "missing")):
            with self.subTest(name):
                c = {**good, **edit}
                c = {k: v for k, v in c.items() if v is not None}
                self.assertTrue(any(needle in p for p in conf.check(rules, c, dist)))
        drifted = conf.parse("Threads = 1\nRate = 2\nPort = 8085\nOther = x\n")
        self.assertTrue(any("the .dist says" in p for p in conf.check(rules, {**good, "Rate": "1"}, drifted)))


class AccountsTest(unittest.TestCase):
    def test_srp6_verifier(self):
        salt = bytes(range(32))
        v = srp6.verifier("DojoAdmin", "pass", salt)
        self.assertEqual(len(v), 32)
        self.assertEqual(v, srp6.verifier("DOJOADMIN", "PASS", salt))   # AzerothCore upper-cases both
        self.assertNotEqual(v, srp6.verifier("DOJOADMIN", "PASS2", salt))
        # Regression pin: g^H(s | H(USER:PASS)) mod N, little-endian, as the core's SRP6::CalculateVerifier.
        self.assertEqual(v.hex(), srp6.verifier("DOJOADMIN", "PASS", salt).hex())
        x = int.from_bytes(__import__("hashlib").sha1(salt + __import__("hashlib").sha1(b"DOJOADMIN:PASS").digest())
                           .digest(), "little")
        self.assertEqual(int.from_bytes(v, "little"), pow(7, x, srp6.N))
        salt2, v2 = srp6.make("A", "B")
        self.assertEqual(srp6.verifier("A", "B", salt2), v2)

    def test_account_passwords_fit_azerothcore(self):
        pw = provision.new_account_password()
        self.assertEqual(len(pw), 16)
        self.assertTrue(pw.isupper() or pw.isdigit() or pw.isalnum())
        self.assertEqual(pw, pw.upper())


class FakeRa:
    """A loopback RA server: prompts, checks the credentials, echoes commands."""

    def __init__(self, user="DOJOADMIN", password="PW"):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.user, self.password = user, password
        self.received: list[str] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        conn, _ = self.sock.accept()
        f = conn.makefile("rb")
        conn.sendall(b"Authentication Required\r\nUsername: ")
        user = f.readline().strip().decode()
        conn.sendall(b"Password: ")
        password = f.readline().strip().decode()
        if (user, password) != (self.user, self.password):
            conn.sendall(b"Authentication failed\r\n")
            conn.close()
            return
        conn.sendall(b"motd\r\nAC>")
        for line in f:
            cmd = line.strip().decode()
            self.received.append(cmd)
            conn.sendall(f"you said {cmd}\r\nline two\r\nAC>".encode())
        conn.close()


class RaTest(unittest.TestCase):
    def test_commands_and_output(self):
        server = FakeRa()
        with RaClient(server.port, "DOJOADMIN", "PW", timeout=5) as client:
            self.assertEqual(client.command("dojo status"), "you said dojo status\nline two")
            with self.assertRaises(RaError):
                client.command("a\nb")
        self.assertEqual(server.received, ["dojo status"])

    def test_bad_credentials_and_hosts(self):
        server = FakeRa()
        with self.assertRaisesRegex(RaError, "authentication"):
            RaClient(server.port, "DOJOADMIN", "WRONG", timeout=5)
        with self.assertRaises(RaError):
            RaClient(1, "u", "p", host="192.0.2.1")


class WorldTest(unittest.TestCase):
    def test_child_env_drops_azerothcore_overrides(self):
        with mock.patch.dict(os.environ, {"AC_WORLD_DATABASE_INFO": "live", "ac_rate_health": "9", "PATH": "x"}):
            env = world.child_env()
        self.assertNotIn("AC_WORLD_DATABASE_INFO", env)
        self.assertNotIn("ac_rate_health", env)
        self.assertEqual(env["AC_DISABLE_INTERACTIVE"], "1")
        self.assertEqual(env["PATH"], "x")

    def test_dry_run_verdict_reads_the_server_log(self):
        tmp = Path(tempfile.mkdtemp(prefix="dojo-dry-"))
        try:
            log = tmp / "Server.log"
            self.assertFalse(world.dry_run_verdict(log, 0)[0])
            log.write_text("...\nAzerothCore Dry Run Completed, Terminating.\n", encoding="utf-8")
            self.assertEqual(world.dry_run_verdict(log, 0), (True, "completed"))
            ok, why = world.dry_run_verdict(log, 0xC0000005)
            self.assertTrue(ok)
            self.assertIn("0xc0000005", why)
            log.write_text("AzerothCore Dry Run Completed With 2 Failed Database Update(s), Terminating.\n",
                           encoding="utf-8")
            self.assertFalse(world.dry_run_verdict(log, 1)[0])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class SideEffectsTest(unittest.TestCase):
    def snap(self, chars=None, aura=None, counts=None):
        cols = ["guid", "name", "level", "money", "totaltime", "logout_time", "position_x"]
        return {"tables": {
            "characters": {"columns": cols, "rows": chars or [["1", "A", "80", "0", "5", "100", "1.0"]]},
            "character_aura": {"columns": ["guid", "spell"], "rows": aura or []},
            "character_entry_point": {"columns": ["guid", "joinX"], "rows": [["1", "0"]]}},
            "counts": counts or {"arena_team": 0, "random_bot_accounts": 0}}

    def test_bookkeeping_is_reported_not_failed(self):
        after = self.snap(chars=[["1", "A", "80", "0", "9", "200", "4027.6"]])
        after["tables"]["character_entry_point"]["rows"] = [["1", "16222.1"]]
        result = acceptance.compare(self.snap(), after)
        self.assertTrue(result["ok"])
        self.assertIn("character_entry_point", result["bookkeeping"])
        self.assertTrue(any("totaltime" in b for b in result["bookkeeping"]))

    def test_progression_fails(self):
        for name, after in (("money", self.snap(chars=[["1", "A", "80", "5", "5", "100", "1.0"]])),
                            ("aura", self.snap(aura=[["1", "32727"]])),
                            ("arena team", self.snap(counts={"arena_team": 1, "random_bot_accounts": 0})),
                            ("bots", self.snap(counts={"arena_team": 0, "random_bot_accounts": 50}))):
            with self.subTest(name):
                self.assertFalse(acceptance.compare(self.snap(), after)["ok"])


class GrantsTest(unittest.TestCase):
    def test_expected_grants_are_exactly_the_four_schemas(self):
        s = Synthetic()
        try:
            grants = checks.expected_grants(s.load())
        finally:
            s.close()
        self.assertEqual(len(grants), 5)
        self.assertIn("GRANT USAGE ON *.* TO `dojo`@`127.0.0.1`", grants)
        self.assertIn("GRANT ALL PRIVILEGES ON `dojo\\_world`.* TO `dojo`@`127.0.0.1`", grants)


if __name__ == "__main__":
    unittest.main()
