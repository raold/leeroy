import pytest

from nightly.conftest import h  # noqa: F401


@pytest.fixture(autouse=True)
def _own_night_counters(monkeypatch):
    """Meme stages register their budget counters when made; keep them inside each test, so no
    later test's night plan (budget.COUNTERS is process-wide) ever counts them."""
    from leeroy_mind.nightly import budget
    monkeypatch.setattr(budget, "COUNTERS", dict(budget.COUNTERS))
