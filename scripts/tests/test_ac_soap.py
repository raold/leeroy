"""ac-soap.py against a fake SOAP server on loopback; never dials a real worldserver."""
from __future__ import annotations

import base64
import http.server
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "ac-soap.py"
USER, PASSWORD = "TESTCONSOLE", "invented-Pass-42"


class FakeSoap(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        FakeSoap.seen.append(body)
        want = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        if self.headers.get("Authorization") != want:
            self.send_response(401)
            self.end_headers()
            return
        if "<command>boom</command>" in body:
            reply = ("<SOAP-ENV:Envelope><SOAP-ENV:Body><SOAP-ENV:Fault>"
                     "<faultstring>There is no such command.</faultstring>"
                     "</SOAP-ENV:Fault></SOAP-ENV:Body></SOAP-ENV:Envelope>")
            self.send_response(500)
        else:
            reply = ("<SOAP-ENV:Envelope><SOAP-ENV:Body><ns1:executeCommandResponse><result>"
                     "Connected players: 0 (peak: 0).&#xD;\nServer uptime: 1 minute(s)"
                     "</result></ns1:executeCommandResponse></SOAP-ENV:Body></SOAP-ENV:Envelope>")
            self.send_response(200)
        data = reply.encode()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class SoapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), FakeSoap)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.temp = tempfile.TemporaryDirectory(prefix="leeroy-soap-tests-")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.temp.cleanup()

    def setUp(self):
        FakeSoap.seen.clear()

    def creds(self, name, text):
        path = Path(self.temp.name) / name
        path.write_text(text, encoding="utf-8")
        return path

    def run_soap(self, path, *command):
        env = dict(os.environ, LEEROY_REMOTE_CONSOLE=str(path))
        return subprocess.run([sys.executable, "-I", str(SCRIPT), "--timeout", "5", *command],
                              capture_output=True, text=True, env=env, timeout=30)

    def good(self, password=PASSWORD, host="127.0.0.1"):
        return self.creds("good.cnf", f"# local only\nuser={USER}\npassword={password}\n"
                                      f"soap={host}:{self.port}\n")

    def test_answer_is_printed_and_no_secret_is(self):
        r = self.run_soap(self.good(), "server", "info")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Connected players: 0", r.stdout)
        self.assertIn("<command>server info</command>", FakeSoap.seen[0])
        self.assertNotIn(PASSWORD, r.stdout + r.stderr)

    def test_command_text_is_escaped(self):
        self.run_soap(self.good(), "announce", "<b>&")
        self.assertIn("<command>announce &lt;b&gt;&amp;</command>", FakeSoap.seen[0])

    def test_refused_account_is_a_failure(self):
        r = self.run_soap(self.good(password="wrong"), "server", "info")
        self.assertEqual(r.returncode, 1)
        self.assertIn("refused", r.stderr)
        self.assertNotIn("wrong", r.stdout + r.stderr)

    def test_fault_is_a_failure_with_its_words(self):
        r = self.run_soap(self.good(), "boom")
        self.assertEqual(r.returncode, 1)
        self.assertIn("There is no such command.", r.stderr)

    def test_not_set_up_is_exit_2(self):
        r = self.run_soap(Path(self.temp.name) / "absent.cnf", "server", "info")
        self.assertEqual(r.returncode, 2)
        self.assertIn("not set up", r.stderr)
        r = self.run_soap(self.creds("partial.cnf", f"user={USER}\n"), "server", "info")
        self.assertEqual(r.returncode, 2)

    def test_non_loopback_address_is_never_dialled(self):
        r = self.run_soap(self.good(host="192.0.2.10"), "server", "info")
        self.assertEqual(r.returncode, 2)
        self.assertIn("non-loopback", r.stderr)
        self.assertEqual(FakeSoap.seen, [])

    def test_no_answer_is_a_failure(self):
        closed = self.creds("closed.cnf", f"user={USER}\npassword={PASSWORD}\nsoap=127.0.0.1:1\n")
        r = self.run_soap(closed, "server", "info")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no answer", r.stderr)


if __name__ == "__main__":
    unittest.main()
