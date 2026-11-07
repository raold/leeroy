"""Every test runs with LEEROY_SELFIE_DATA at a tmp dir of its own (not created), so a default
data dir is never the owner's. SESSION_SELFIE_DATA keeps the run's own value, captured before
any test's, for the `engines` tests that read the fetched model files."""
import os
from pathlib import Path

import pytest

from leeroy_selfie import paths

REPO = Path(paths.__file__).resolve().parents[2]
REAL_SELFIE_DATA = REPO / "selfie-data"  # data_dir() without LEEROY_SELFIE_DATA
SESSION_SELFIE_DATA = os.environ.get("LEEROY_SELFIE_DATA")


@pytest.fixture(autouse=True)
def selfie_data(tmp_path, monkeypatch) -> Path:
    data = tmp_path / "selfie-data"
    monkeypatch.setenv("LEEROY_SELFIE_DATA", str(data))
    return data


@pytest.fixture(scope="session")
def fetched_data_dir() -> Path:
    """The data dir whose fetched models an `engines` test reads: the session's, else the
    checkout's selfie-data."""
    return Path(SESSION_SELFIE_DATA) if SESSION_SELFIE_DATA else REAL_SELFIE_DATA
