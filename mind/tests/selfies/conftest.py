import pytest

from nightly.conftest import h  # noqa: F401  (the night's harness, for the selfie stages)


@pytest.fixture
def data(tmp_path):
    """A mind data dir of the test's own."""
    d = tmp_path / "mind-data"
    d.mkdir()
    return d
