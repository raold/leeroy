import pytest

from lab.rig import make_live


@pytest.fixture
def live(tmp_path):
    return make_live(tmp_path)
