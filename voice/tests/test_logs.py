import asyncio
import gc
import io
import json
import logging

from leeroy_voice import logs


def records(tmp_path) -> list[dict]:
    text = (tmp_path / "logs" / "voice.log").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def test_json_lines(tmp_path):
    teardown = logs.setup(tmp_path)
    try:
        logs.log("mind_connected", peer="127.0.0.1:50000", lines=2)
        logs.log("speak_dropped", level=logging.WARNING, id="s1", reason="expired", ts=7)
        logging.getLogger("leeroy_voice.server").info("plain message")
    finally:
        teardown()
    first, second, plain = records(tmp_path)
    assert first["event"] == "mind_connected" and first["level"] == "INFO"
    assert (first["peer"], first["lines"]) == ("127.0.0.1:50000", 2) and "T" in first["ts"]
    assert second["level"] == "WARNING" and second["ts_"] == 7  # a field never hides the envelope
    assert (plain["event"], plain["msg"]) == ("log", "plain message")


def test_line_text_never_reaches_the_log(tmp_path):
    teardown = logs.setup(tmp_path)
    try:
        logs.log("heard", text="Belra, you're on adds", raw="bella youre on adds", speech_ms=1480)
    finally:
        teardown()
    (rec,) = records(tmp_path)
    assert (rec["text_chars"], rec["raw_chars"], rec["speech_ms"]) == (21, 19, 1480)
    assert "adds" not in (tmp_path / "logs" / "voice.log").read_text(encoding="utf-8")


SAID = "Belra, you're on adds"  # a line someone said, which no log may hold


def voice_it(text: str) -> None:
    """Fails the way a handler might: the line in its messages, a cause chained."""
    try:
        raise ValueError(f"cannot voice {text}")
    except ValueError as e:
        raise KeyError(text) from e


def test_tracebacks_keep_frames_not_messages(tmp_path, capsys):
    teardown = logs.setup(tmp_path)  # after capsys: the console handler writes to its stderr
    try:
        try:
            voice_it(SAID)
        except KeyError:
            logs.log("mind_message_failed", level=logging.ERROR, error="KeyError", exc_info=True)
    finally:
        teardown()
    (rec,) = records(tmp_path)
    console = capsys.readouterr().err
    for out in ((tmp_path / "logs" / "voice.log").read_text(encoding="utf-8"), console):
        assert "adds" not in out
    for out in (rec["exc"], console):
        assert "ValueError: <redacted>" in out and "KeyError: <redacted>" in out
        assert "direct cause of the following exception" in out
        assert "test_logs.py" in out and "in voice_it" in out  # the frames stay
    assert rec["exc"].index("ValueError") < rec["exc"].index("KeyError")  # cause first


def test_teardown_detaches(tmp_path):
    logs.setup(tmp_path)()
    logs.log("after_teardown")
    assert not (tmp_path / "logs" / "voice.log").exists() or records(tmp_path) == []
    assert not logging.getLogger("leeroy_voice").handlers


# Built here: a traceback prints the source lines it came from
WORDS = "a whisper he only meant " + "for her in the private room"


def spoken(text: str) -> list[str]:
    """What is left of WORDS in `text`: asyncio's repr keeps both ends of a long string."""
    return [word for word in ("whisper", "meant", "private") if word in text]


def asyncio_failure(tmp_path, capsys, fail) -> tuple[str, str]:
    """A real failure in a real event loop: what voice.log and the console keep of it."""
    teardown = logs.setup(tmp_path)
    try:
        asyncio.run(fail())
    finally:
        teardown()
    return ((tmp_path / "logs" / "voice.log").read_text(encoding="utf-8"),
            capsys.readouterr().err)


def test_a_failed_callback_keeps_his_words_out_of_the_log(tmp_path, capsys):
    """asyncio writes a callback's arguments into its record's message, the path of the service's
    _on_heard. Where it failed and the error's type stay; the words do not."""
    def on_heard(pending, text):
        raise ValueError(text)

    async def fail():
        asyncio.get_running_loop().call_soon_threadsafe(on_heard, None, WORDS)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    log, console = asyncio_failure(tmp_path, capsys, fail)
    assert "Exception in callback" in log and "on_heard" in log
    assert "ValueError" in log and logs.REDACTED in log
    assert spoken(log) == []
    assert "on_heard" in console and "ValueError" in console and spoken(console) == []


def test_an_unretrieved_task_keeps_his_words_out_of_the_log(tmp_path, capsys):
    """A task's exception reaches its record's message whole, in `future: <Task ...>`."""
    async def hear():
        raise ValueError(WORDS)

    async def fail():
        task = asyncio.create_task(hear())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        del task
        gc.collect()
    log, console = asyncio_failure(tmp_path, capsys, fail)
    assert "Task exception was never retrieved" in log and "ValueError" in log
    assert spoken(log) == [] and spoken(console) == []
    assert not logging.getLogger("asyncio").handlers


def test_only_asyncios_messages_are_cut(tmp_path):
    """Our own and the libraries' plain messages are ours to word, parentheses and all."""
    teardown = logs.setup(tmp_path)
    try:
        logging.getLogger("leeroy_voice.ui.websockets").warning("connection rejected (403)\nnext")
    finally:
        teardown()
    assert records(tmp_path)[0]["msg"] == "connection rejected (403)\nnext"


def test_a_stream_is_kept_able_to_print_any_name():
    """A device name beyond cp1252 must not raise from the CLI (Task 4's review)."""
    raw = io.BytesIO()
    out = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    logs.utf8_stdio(out)
    out.write("Mic " + chr(0x1F3A4))
    out.flush()
    assert raw.getvalue().decode("utf-8").endswith(chr(0x1F3A4))
    logs.utf8_stdio(None)  # pythonw has none
