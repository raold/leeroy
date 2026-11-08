"""Opt-in timings and reproducible collection shuffling, including xdist workers.

From mind/: python -m pytest -q tests -p suite_profile --suite-profile=profile.json
Add --suite-seed=11 to shuffle collection without reseeding application randomness.
Fixture times measure setup only; per-file times include setup, call and teardown.
"""
import json
import hashlib
import random
from collections import defaultdict
from pathlib import Path
from time import perf_counter

import pytest

_fixtures = defaultdict(lambda: {"count": 0, "seconds": 0.0})
_files = defaultdict(lambda: {"count": 0, "setup": 0.0, "call": 0.0, "teardown": 0.0})
_selection = {}


def pytest_addoption(parser):
    parser.addoption("--suite-profile", help="Write per-file and fixture setup timings as JSON")
    parser.addoption("--suite-seed", type=int, help="Shuffle collection with this integer seed")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    _selection["selected"] = len(items)
    _selection["nodeids_sha256"] = hashlib.sha256(
        "\n".join(sorted(item.nodeid for item in items)).encode()).hexdigest()
    seed = config.getoption("suite_seed")
    if seed is not None:
        random.Random(seed).shuffle(items)


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef, request):
    if not request.config.getoption("suite_profile"):
        return (yield)
    start = perf_counter()
    try:
        return (yield)
    finally:
        key = f"{fixturedef.baseid or '<root>'}::{fixturedef.argname}"
        _fixtures[key]["seconds"] += perf_counter() - start
        _fixtures[key]["count"] += 1


def pytest_runtest_logreport(report):
    row = _files[report.nodeid.split("::", 1)[0]]
    row[report.when] += report.duration
    if report.when == "call":
        row["count"] += 1


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error):
    output = getattr(node, "workeroutput", {})
    _selection.update(output.get("suite_selection", {}))
    for key, value in output.get("suite_fixtures", {}).items():
        for field in ("count", "seconds"):
            _fixtures[key][field] += value[field]


def pytest_sessionfinish(session):
    path = session.config.getoption("suite_profile")
    if path:
        if hasattr(session.config, "workeroutput"):
            session.config.workeroutput["suite_fixtures"] = dict(_fixtures)
            session.config.workeroutput["suite_selection"] = dict(_selection)
        else:
            Path(path).write_text(json.dumps({
                "seed": session.config.getoption("suite_seed"),
                "selection": dict(_selection),
                "files": dict(_files), "fixtures": dict(_fixtures),
            }, indent=2), encoding="utf-8")
