"""Real VoiceService and sockets; existing fake audio, recognizer and engine fixtures."""
import asyncio
import contextlib
import sys

import numpy as np

import leeroy_voice
import test_service
from leeroy_voice.service import SystemClock
from rehearsal_common import (CUT_LINE, FIRST_LINE, RUN, WORKTREE, endpoint, package_path, publish,
                              stage, under, until)


def test_voice_leg(tmp_path, fake_bank, monkeypatch, live_guard):
    assert sys.version_info[:2] == (3, 12)
    imported = package_path(leeroy_voice, "voice")
    under(test_service.__file__, WORKTREE / "voice" / "tests")
    under(tmp_path, RUN)
    # Use a shared Unix time base across the two interpreters; the hardware remains fake.
    monkeypatch.setattr(test_service, "FakeClock", lambda *a, **kw: SystemClock())

    async def run():
        rig = test_service.Rig(tmp_path, fake_bank, connect=False)
        heard_count = 0
        peak = 0.0
        pump_task = None

        async def pump():
            nonlocal peak
            while True:
                output = rig.out()
                if output is not None:
                    peak = max(peak, float(np.abs(output.pump(480)).max()))
                mic = rig.stream("input")
                if mic is not None:
                    mic.feed(test_service.BLOCK)
                await asyncio.sleep(0.01)

        async with rig:
            assert rig.service._gpu is None and rig.service._expressive is None
            assert type(rig.engine) is test_service.Engine
            assert type(rig.devices).__name__ == "FakeDeviceApi"
            assert type(rig.recognizer).__name__ == "FakeRecognizer"
            ports = [endpoint(rig.service.server.port), endpoint(rig.service.ui.port)]
            assert len(set(ports)) == 2
            specs = [test_service.spec(guid) | {"name": f"Member {guid}"}
                     for guid in range(101, 106)]
            publish("voice-ready", ports=ports, specs=specs, imported=imported,
                    python=sys.version, data=str(under(rig.data, tmp_path)))
            pump_task = asyncio.create_task(pump())
            try:
                await until(lambda: rig.service.online and len(rig.service.occupancy.where) == 6,
                            "real mind session and six seats")
                state = rig.service.state()
                assert state["human"] == 1 and state["room"] == "guild_hall"
                assert set(state["where"]) == {1, 101, 102, 103, 104, 105}
                assert set(state["names"]) == {101, 102, 103, 104, 105}
                assert all(room == "guild_hall" for room in state["where"].values())
                assert len(state["tree"]) >= 20
                assert state["status"]["gpu"] == "none" and state["status"]["ok"]
                page = await rig.page()
                assert page.received[0]["room"] == "guild_hall"
                publish("snapshot-checked", members=5, seats=6)

                await stage("first-played")
                assert FIRST_LINE in rig.engine.live() and peak > 0.01
                assert any(row["text"] == FIRST_LINE for row in rig.service.state()["transcript"])
                # Browser -> voice -> mind policy -> voice mirror, through both real sockets.
                await page.send({"type": "join", "channel": "lore"})
                await until(lambda: rig.service.occupancy.human_room == "lore", "UI room join")
                await page.send({"type": "join", "channel": "guild_hall"})
                await until(lambda: rig.service.occupancy.human_room == "guild_hall", "UI return")
                publish("joins-checked", rooms=["lore", "guild_hall"])

                rig.keys.key(True)
                await until(lambda: rig.service.state()["ptt"], "synthetic PTT press")
                await asyncio.sleep(0.35)
                rig.keys.key(False)
                await stage("heard-stored")
                heard_count = len(rig.recognizer.calls)
                assert heard_count == 1
                publish("ptt-checked", recognizer_calls=heard_count)

                await stage("cut-requested")
                await until(lambda: CUT_LINE in rig.engine.live() and rig.service.scheduler.playing(),
                            "second line playing")
                rig.keys.key(True)
                await until(lambda: rig.service.state()["ptt"], "PTT before logout")
                publish("logout-ready", playing=True, ptt=True)
                await until(lambda: not rig.service.online, "logout across real wire")
                await until(lambda: rig.stream("input") is None and rig.out() is None,
                            "fake streams closed on logout")
                assert not rig.keys.armed and not rig.service.state()["ptt"]
                assert not rig.service.capture.ready and not rig.service._clips
                assert abs(rig.wow().volume - 0.8) < 1e-6
                rig.keys.key(False)
                await asyncio.sleep(0.3)
                assert len(rig.recognizer.calls) == heard_count
                publish("logout-checked", mic_closed=True, output_closed=True,
                        ptt_disarmed=True, volume_restored=True, stale_transcripts=0)
                await stage("stop-voice")
            finally:
                if pump_task is not None:
                    pump_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await pump_task
        assert rig.task.done() and rig.task.exception() is None
        assert all(stream.closed for stream in rig.devices.streams)
        assert rig.keys.stopped
        assert not (rig.data / "voice.pid").exists()
        assert not live_guard.hits
        publish("voice-result", passed=True, imported=imported, ports=ports,
                fake_audio_peak=peak, recognizer_calls=heard_count,
                service_task_stopped=True, fake_streams_closed=True, key_listener_stopped=True,
                guard_hits=0, gpu_models_loaded=0, expressive_models_loaded=0)

    asyncio.run(asyncio.wait_for(run(), 100))
