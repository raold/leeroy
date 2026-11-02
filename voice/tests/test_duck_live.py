"""The Windows audio sessions (Task 7, marked `windows`; deselected by default). Run locally with
`pytest -q -m "device or windows" tests/test_duck_live.py`. It only reads: no volume is set."""
import pytest

pytestmark = pytest.mark.windows


def test_wow_sessions_are_found():
    """A list, empty when WoW is closed; each session's level reads as 0-1."""
    pytest.importorskip("pycaw")
    pytest.importorskip("psutil")
    from leeroy_voice.duck import PycawSessions
    sessions = PycawSessions()
    try:
        found = sessions.find("Wow.exe")
        assert isinstance(found, list)
        for handle in found:
            assert 0.0 <= handle.get() <= 1.0
    finally:
        sessions.close()
