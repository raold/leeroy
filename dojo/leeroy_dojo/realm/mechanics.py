"""Supervise the candidate mechanic-feed process without importing the mind."""
import json
from pathlib import Path
import subprocess
import sys
import threading

from . import world


class MechanicConnection:
    def __init__(self, layout, *, guild, map_id):
        self.ready = False
        self.error = ""
        self.features = []
        self.rules = 0
        self.process = None
        self.thread = None
        self.command = [sys.executable, str(Path(layout.roots["source"]) / "scripts/dojo/mechanic-feed.py"),
                        "--layout", str(layout.path), "--guild", str(guild), "--map", str(map_id)]

    def __enter__(self):
        self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=world.child_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.thread = threading.Thread(target=self._read, daemon=True, name="dojo-mechanic-feed")
        self.thread.start()
        return self

    def __exit__(self, *args):
        if self.process is None:
            return
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
            self.error = "mechanic feed did not stop"
        self.thread.join(5)
        self.process.stdout.close()
        self.ready = False

    def _read(self):
        try:
            for line in self.process.stdout:
                state = json.loads(line)
                if state.get("kind") != "dojo-mechanic-feed" or type(state.get("ready")) is not bool:
                    raise ValueError("invalid feed observation")
                self.ready = state["ready"]
                self.error = state.get("error", "")
                self.features = state.get("features", [])
                self.rules = state.get("rules", 0)
        except (OSError, ValueError, TypeError):
            self.error = "invalid mechanic-feed process output"
        finally:
            self.ready = False
