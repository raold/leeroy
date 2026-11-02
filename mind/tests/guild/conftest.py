import pytest

from .fakes import FakePorts


@pytest.fixture
def fake(tmp_path, clock):
    ports = FakePorts(tmp_path, clock)
    try:
        yield ports
    finally:
        ports.close()
