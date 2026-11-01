"""python -m leeroy_voice <command>. The data dir is LEEROY_VOICE_DATA, else voice-data beside
the repo.

run is the service; where, held, doctor and devices read only. The commands import the audio
libraries when they need them, so `where` and `held`, which ac-voice.sh runs, stay quick. What
reaches the machine or a model goes through a function here that the tests replace."""
import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

from . import models
from .config import ConfigError, VoiceConfig, default_data_dir, load_config
from .decision import CLONING, Decision, DecisionError, load_decision
from .lexicon import LexiconError, load_lexicon
from .lock import LOCK_FILE, ServiceLocked, lock_held, read_pid
from .logs import setup as setup_logs
from .logs import utf8_stdio

DOCTOR_MARK = {True: "ok  ", False: "FAIL", None: "note"}
DUCKING_KEY = r"Software\Microsoft\Multimedia\Audio"
DUCKING_VALUE = "UserDuckingPreference"
DUCKING_NOTHING = 3  # Sound > Communications: "Do nothing"
SELFTEST_KEY = "ready"  # the stock line selftest speaks
SELFTEST_FILE = "selftest.wav"  # in <data dir>/debug
PLAY_MARGIN_S = 1.0  # how long past the clip's end --play waits for the output
TEST_KEY = 0x87  # F24: a key no keyboard has, injected by selftest --ptt
KEY_WAIT_S = 2.0
WHISPER_NEED_GB = 1.5  # Whisper large-v3-turbo int8 on the card
# the card's need of each cloning engine, GpuGuard's margin on top: estimates, the kit's
# private/voice-test/engine_sizes.json (the weights alone: 3.2, 3.0 and 8.9 GB)
EXPRESSIVE_NEED_GB = {"chatterbox": 6.0, "chatterbox-turbo": 3.0, "indextts2": 8.0}
CPU_THREADS = 8  # the kit's, for a render on the CPU beside the game server
CPU_LINE_TIMEOUT_S = 300.0
# a design engine in the listening kit (private/voice-lab): its venv, and its model's hub folder
# and revision (Task 1)
DESIGN_LAB = {"qwen3-voicedesign": ("engines/qwen-tts/.venv",
                                    "models--Qwen--Qwen3-TTS-12Hz-1.7B-VoiceDesign",
                                    "5ecdb67327fd37bb2e042aab12ff7391903235d3")}


class Probes:
    """What the doctor reads of the machine. Tests replace PROBES with fakes.FakeProbes."""

    def devices(self) -> list:
        api = device_api()
        try:
            return api.wasapi_devices()
        finally:
            api.close()

    def port_owner(self, port: int) -> int | None:
        """The pid listening on this port; None when none does."""
        import psutil
        for c in psutil.net_connections("tcp"):
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port:
                return c.pid or 0
        return None

    def ducking_preference(self) -> int | None:
        """HKCU's UserDuckingPreference; None when it is not set."""
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, DUCKING_KEY) as key:
                value, _ = winreg.QueryValueEx(key, DUCKING_VALUE)
        except (ImportError, OSError):
            return None
        return value if type(value) is int else None


    def ollama_loaded(self):
        """/api/ps through gpu.OllamaPs, the one probe; None when Ollama cannot be asked."""
        from .gpu import OllamaPs
        return OllamaPs().loaded()


PROBES = Probes()


def device_api():
    from .audio_io import SoundDeviceApi
    return SoundDeviceApi()


def session_api():
    from .duck import PycawSessions
    return PycawSessions()


def key_factory():
    from .hotkey import RawInputKeys
    return RawInputKeys


def inject_key(vk: int, down: bool) -> None:
    from .hotkey import send_key
    send_key(vk, down)


def _offline(root: Path) -> None:
    """Before anything imports huggingface_hub: models load from their pinned folders, never
    from the network (I4)."""
    os.environ["HF_HOME"] = str(root / models.HF_CACHE)
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HUB_OFFLINE"] = "1"


def _require(root: Path, name: str) -> None:
    """Raises ModelError unless the model is fetched and as pinned."""
    bad = [text for ok, text in models.check(root, name) if ok is not True]
    if bad:
        raise models.ModelError("; ".join(bad))


def load_engine(root: Path, cfg: VoiceConfig):
    """The engine and the bank of its voices file: Kokoro on the CPU. Raises ModelError."""
    from .engines.kokoro import KokoroEngine
    from .voices import StyleBank
    _require(root, "kokoro")
    d = models.model_dir(root, "kokoro")
    return (KokoroEngine(d / "kokoro-v1.0.onnx", d / "voices-v1.0.bin", threads=cfg.tts_threads),
            StyleBank(d / "voices-v1.0.bin"))


def load_embedder(root: Path):
    """ECAPA on the CPU, for casting. Raises ModelError."""
    from .cast import ecapa_embedder
    _require(root, "ecapa")
    return ecapa_embedder(models.model_dir(root, "ecapa"), root / "models" / "ecapa-run")


def mind_voices_dir() -> Path:
    """Where the voice specs live: the mind's data dir (LEEROY_MIND_DATA, else mind-data beside
    the repo) / voices."""
    env = os.environ.get("LEEROY_MIND_DATA")
    return (Path(env) if env else Path(__file__).resolve().parents[2] / "mind-data") / "voices"


def load_recognizer(root: Path, cfg: VoiceConfig):
    """Parakeet behind the speech gate, or None: a model that will not load costs his side only,
    and the status says stt "off"."""
    from .logs import log
    try:
        from .stt.parakeet import ParakeetRecognizer
        from .stt.vad import SpeechGate
        for name in ("parakeet", "silero"):
            _require(root, name)
        gate = SpeechGate(models.model_dir(root, "silero"))
        return ParakeetRecognizer(models.model_dir(root, "parakeet"), gate,
                                  threads=cfg.stt_threads)
    except Exception as e:  # models.ModelError, onnx-asr's ModelLoadingError, a missing extra
        log("stt_unavailable", level=logging.ERROR, error=type(e).__name__)
        return None


def load_whisper(root: Path, cfg: VoiceConfig):
    """Whisper on the GPU behind its own speech gate. Raises: ModelError, a missing gpu extra,
    CUDA's own errors; GpuGuard logs the type and tries again later."""
    from .stt.vad import SpeechGate
    from .stt.whisper import WhisperRecognizer
    for name in ("whisper", "silero"):
        _require(root, name)
    return WhisperRecognizer(models.model_dir(root, "whisper"),
                             SpeechGate(models.model_dir(root, "silero")))


def attach_gpu(service, root: Path, cfg: VoiceConfig) -> None:
    """The admission rule over the service. It logs gpu_budget whatever stt.recognizer says (that
    is what the owner reads before choosing "auto"), and loads Whisper only on "auto"."""
    from .gpu import GpuGuard, OllamaPs, free_vram_gb, wow_running
    guard = GpuGuard(service, ps=OllamaPs(), free=free_vram_gb, wow=wow_running,
                     need_gb=WHISPER_NEED_GB, margin_gb=cfg.gpu_margin_gb)
    service.attach_gpu(guard, lambda: load_whisper(root, cfg))


def expressive_name(decision: Decision) -> str | None:
    """The cloning engine the decision names; None when everyone is Kokoro."""
    return next((n for n in (decision.engine, *decision.featured.values()) if n in CLONING), None)


def worker_env(root: Path, *, cpu: bool) -> dict[str, str]:
    """Every library cache inside the data dir, as the kit keeps them inside its own, and the
    worker offline (I4)."""
    env = {"HF_HOME": str(root / models.HF_CACHE), "HF_HUB_DISABLE_TELEMETRY": "1",
           "HF_HUB_OFFLINE": "1", "TORCH_HOME": str(root / "cache" / "torch"),
           "MPLCONFIGDIR": str(root / "cache" / "matplotlib"),
           "NUMBA_CACHE_DIR": str(root / "cache" / "numba")}
    return {**env, "CUDA_VISIBLE_DEVICES": ""} if cpu else env


def load_expressive(root: Path, cfg: VoiceConfig, decision: Decision, *, device: str,
                    refs_dir: Path):
    """The engine the decision names, not started, under its own venv
    (<data dir>/engines/<engine>/.venv); None when it names none. Raises ModelError when it is
    not set up, and LeverError on a bad attitudes.toml."""
    from .engines.expressive import LINE_TIMEOUT_S, ExpressiveEngine, load_levers
    name = expressive_name(decision)
    if name is None:
        return None
    python = root / "engines" / name / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        raise models.ModelError(f"{name}: no venv at {python}")
    _require(root, name)
    cpu = device == "cpu"
    threads = CPU_THREADS if cpu else cfg.tts_threads
    return ExpressiveEngine(
        name, python, need_gb=EXPRESSIVE_NEED_GB[name], refs_dir=refs_dir,
        levers=load_levers(root, name), env=worker_env(root, cpu=cpu), below_normal=cpu,
        args=("--model", models.model_dir(root, name), "--device", device, "--threads", threads),
        timeout_s=CPU_LINE_TIMEOUT_S if cpu else LINE_TIMEOUT_S)


def attach_expressive(service, root: Path, cfg: VoiceConfig, decision: Decision) -> None:
    """The chosen engine over the service; without its venv or model the service runs on Kokoro
    and says why. The references are the mind's, beside the specs."""
    from .logs import log
    try:
        engine = load_expressive(root, cfg, decision, device="cuda", refs_dir=mind_voices_dir())
    except (models.ModelError, ValueError) as e:  # LeverError is a ValueError
        log("engine_unavailable", level=logging.ERROR, error=str(e))
        return
    if engine is not None:
        service.attach_expressive(engine)


def design_source(root: Path, cfg: VoiceConfig, decision: Decision, engine, bank):
    """What makes the references: the design engine in the kit's venv, on the CPU, or the
    member's own Kokoro blend. Raises ModelError when the kit does not have it."""
    from . import cast
    source = decision.voice_source
    if source == "kokoro":
        return cast.KokoroSource(engine, bank, decision)
    if source not in DESIGN_LAB:
        raise models.ModelError(f"{source}: no design worker")
    venv, repo, revision = DESIGN_LAB[source]
    for base in models.lab_dirs():
        lab = base / "private" / "voice-lab"
        python = lab / venv / "Scripts" / "python.exe"
        model = lab / models.HF_CACHE / "hub" / repo / "snapshots" / revision
        if python.is_file() and model.is_dir():
            return cast.DesignWorker(source, python, env=worker_env(root, cpu=True), args=(
                "--model", model, "--device", "cpu", "--threads", CPU_THREADS))
    raise models.ModelError(f"{source}: the listening kit's venv or model is not there")


def load_corrector(root: Path):
    from .stt.names import RosterCorrector, common_words
    return RosterCorrector((), load_lexicon(root).words(), common=common_words())


def _device_lines(cfg: VoiceConfig, probes) -> list[tuple[bool | None, str]]:
    wanted = [("input device", cfg.input_device, "inputs"),
              ("output device", cfg.output_device, "outputs")]
    out: list[tuple[bool | None, str]] = []
    listed = None
    for label, name, channels in wanted:
        if not name:
            out.append((None, "input device: none named in voice.toml, so no push-to-talk")
                       if channels == "inputs" else (True, "output device: the Windows default"))
            continue
        if listed is None:
            try:
                listed = probes.devices()
            except Exception as e:
                return out + [(False, f"devices: {type(e).__name__}")]
        found = any(d.name == name and getattr(d, channels) > 0 for d in listed)
        out.append((found, f"{label}: the one voice.toml names is "
                           f"{'listed' if found else 'not listed'}"))
    return out


def _port_line(root: Path, label: str, port: int, probes) -> tuple[bool, str]:
    owner = probes.port_owner(port)
    if owner is None:
        return True, f"{label} port {port}: free"
    if lock_held(root) and owner == read_pid(root):
        return True, f"{label} port {port}: held by this service"
    return False, f"{label} port {port}: held by process {owner}"


def _ducking_line(probes) -> tuple[bool | None, str]:
    value = probes.ducking_preference()
    if value == DUCKING_NOTHING:
        return True, f'communications ducking: {value}, "Do nothing"'
    shown = "not set" if value is None else str(value)
    return None, (f"communications ducking: warning: {DUCKING_VALUE} is {shown}, so Windows may "
                  'turn WoW down while the mic is open; set Sound > Communications to "Do '
                  'nothing"')


def _ollama_line(probes) -> tuple[bool | None, str]:
    try:
        loaded = probes.ollama_loaded()
    except Exception as e:
        return None, f"Ollama: not reachable ({type(e).__name__})"
    if loaded is None:
        return None, "Ollama: not reachable; Whisper stays off"
    if not loaded:
        return None, "Ollama: no model loaded"
    shown = []
    for name, (size, in_vram) in sorted(loaded.items(), key=lambda kv: -kv[1][0]):
        pct = 100 if size <= 0 else round(100 * in_vram / size)
        shown.append(f"{name} {pct}% GPU")
    split = any(v < s for s, v in loaded.values())
    return (None if split else True), "Ollama: " + ", ".join(shown)


def doctor(root: Path, probes=None) -> list[tuple[bool | None, str]]:
    """One (ok, text) per check, reading only: ok None is a note, which never fails it."""
    probes = probes or PROBES
    out: list[tuple[bool | None, str]] = []
    v = sys.version_info
    out.append((v[:2] == (3, 12), f"python {v.major}.{v.minor}.{v.micro} (3.12)"))
    path = root / "voice.toml"
    cfg = None
    try:
        cfg = load_config(path)
        out.append((True, f"voice.toml: {path} valid" if path.exists() else
                    f"voice.toml: none at {path}; the defaults"))
    except ConfigError as e:
        out.append((False, f"voice.toml: {e}"))
    path = root / "decision.toml"
    try:
        d = load_decision(path)
        text = (f"decision: {d.engine}, swap {str(d.prosody_swap).lower()}, "
                f"uk mix {str(d.uk_timbre_mix).lower()}")
        out.append((True, text))
        if not path.exists():
            out.append((None, f"decision: none at {path}; Kokoro until the owner decides"))
    except DecisionError as e:
        out.append((False, f"decision: {e}"))
    try:
        out.append((True, f"lexicon: {len(load_lexicon(root).words())} words"))
    except LexiconError as e:
        out.append((False, f"lexicon: {e}"))
    for name in models.MODEL_PINS:
        out += models.check(root, name)
    if cfg is not None:
        out += _device_lines(cfg, probes)
        out.append(_port_line(root, "mind", cfg.mind_port, probes))
        out.append(_port_line(root, "ui", cfg.ui_port, probes))
    out.append(_ducking_line(probes))
    out.append(_ollama_line(probes))
    return out


def cmd_doctor(args) -> int:
    lines = doctor(default_data_dir())
    for ok, text in lines:
        print(f"{DOCTOR_MARK[ok]} {text}")
    return 0 if all(ok is not False for ok, _ in lines) else 1


def cmd_fetch_models(args) -> int:
    only = None if args.only is None else {n.strip() for n in args.only.split(",") if n.strip()}
    try:
        lines = models.fetch_models(default_data_dir(), only)
    except (models.ModelError, OSError) as e:  # one line, as a network failure is
        return _fail("fetch-models", e)
    for ok, text in lines:
        print(f"{DOCTOR_MARK[ok]} {text}")
    return 0


def _close(owner) -> None:
    """The real device and session layers have a close; the fakes need none."""
    close = getattr(owner, "close", None)
    if close is not None:
        close()


def _fail(what: str, e: Exception) -> int:
    print(f"{what}: {' '.join(str(e).split())}", file=sys.stderr)
    return 1


def cmd_where(args) -> int:
    root = default_data_dir()
    try:
        cfg = load_config(root / "voice.toml")
    except ConfigError as e:
        return _fail("where", e)
    print(f"data_dir={root}")
    print(f"mind_port={cfg.mind_port}")
    print(f"ui_port={cfg.ui_port}")
    return 0


def cmd_held(args) -> int:
    root = default_data_dir()
    if not lock_held(root):
        return 1
    print(read_pid(root) or "")
    return 0


def cmd_devices(args) -> int:
    api = device_api()
    try:
        default = api.default_output_name()
        for d in api.wasapi_devices():
            if d.inputs:
                print(f"input   {d.name}")
            if d.outputs:
                print(f"output  {d.name}{'  (default)' if d.name == default else ''}")
    finally:
        _close(api)
    return 0


def _selftest_spec(sha256: str):
    from .voices import SPEC_VERSION, VoiceSpec
    return VoiceSpec(version=SPEC_VERSION, guid=1, name="Selftest", engine="kokoro", lang="a",
                     timbre={"af_heart": 1.0}, prosody={}, speed=1.0, pitch_st=0.0,
                     mic="desk_mic", gain_db=0.0, locked=False, voices_sha256=sha256, ref="")


def _render_selftest(root: Path, cfg: VoiceConfig):
    """The stock line in the test voice through the chain at the configured grit: (clip, path)."""
    import soundfile as sf

    from . import fx
    from .attitude import RAID_CALL_LEVERS
    from .stock import STOCK
    engine, bank = load_engine(root, cfg)
    spec = _selftest_spec(bank.sha256)
    style = bank.style(spec, "none", Decision())
    dry = engine.synth(STOCK[SELFTEST_KEY], spec, style,
                       spec.speed * RAID_CALL_LEVERS.speed_mult)
    clip = fx.process(dry, engine.sample_rate, spec, grit=cfg.comms_grit, whisper=False)
    path = root / "debug" / SELFTEST_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, clip, fx.OUT_RATE, subtype="FLOAT")
    return clip, path


def _admission(root: Path, cfg: VoiceConfig, need_gb: float) -> str | None:
    """GpuGuard's rule, asked once with no session: Ollama's largest model is the live one, its
    smallest the embedding model."""
    from .gpu import OllamaPs, admit, free_vram_gb, wow_running
    ps = OllamaPs().loaded()
    names = sorted(ps or {}, key=lambda name: ps[name][0], reverse=True)
    return admit(ps=ps, free_gb=free_vram_gb(), wow=wow_running(),
                 live=names[0] if len(names) > 1 else "", embed=names[-1] if names else "",
                 need_gb=need_gb, margin_gb=cfg.gpu_margin_gb)


def _expressive_line(root: Path, cfg: VoiceConfig, decision: Decision, spec, text: str,
                     speed: float, *, specs_dir: Path, cpu: bool):
    """The line from the member's engine, or None with a note saying why it cannot speak."""
    from .engines import EngineError
    name = spec.engine
    why = None if cpu else _admission(root, cfg, EXPRESSIVE_NEED_GB[name])
    if why is not None:
        print(f"note {name} refused: {why}; Kokoro's blend instead")
        return None
    engine = load_expressive(root, cfg, decision, device="cpu" if cpu else "cuda",
                             refs_dir=specs_dir)
    if engine is None:
        print(f"note {name}: the decision names no such engine; Kokoro's blend instead")
        return None
    try:
        engine.start()
        return engine.synth(text, spec, None, speed)
    except EngineError as e:
        print(f"note {e}; Kokoro's blend instead")
        return None
    finally:
        engine.stop()


def _render_member(root: Path, cfg: VoiceConfig, guid: int, specs_dir: Path, cpu: bool):
    """The stock line in the member's own voice through the chain: (clip, path, who spoke)."""
    import soundfile as sf

    from . import cast, fx
    from .attitude import RAID_CALL_LEVERS
    from .stock import STOCK
    spec = cast.load_existing(specs_dir).get(guid)
    if spec is None:
        raise ValueError(f"no voice spec for {guid} in {specs_dir}")
    decision = load_decision(root / "decision.toml")
    engine, bank = load_engine(root, cfg)
    text, speed = STOCK[SELFTEST_KEY], spec.speed * RAID_CALL_LEVERS.speed_mult
    dry = None
    if spec.engine != "kokoro":
        dry = _expressive_line(root, cfg, decision, spec, text, speed, specs_dir=specs_dir,
                               cpu=cpu)
    who = "kokoro" if dry is None else spec.engine
    if dry is None:
        dry = engine.synth(text, spec, bank.style(spec, "none", decision), speed)
    clip = fx.process(dry, engine.sample_rate, spec, grit=cfg.comms_grit, whisper=False)
    path = root / "debug" / SELFTEST_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, clip, fx.OUT_RATE, subtype="FLOAT")
    return clip, path, who


def _play(clip, cfg: VoiceConfig) -> None:
    """Through the mixer on the output the service would use."""
    from .audio_io import AudioOut
    from .mixer import SAMPLE_RATE, Mixer
    api = device_api()
    mixer = Mixer()
    out = AudioOut(mixer, api, pinned=cfg.output_device)
    try:
        out.start()
        mixer.play("selftest", clip, 0.0)
        end = time.monotonic() + len(clip) / SAMPLE_RATE + PLAY_MARGIN_S
        while time.monotonic() < end and "selftest" not in mixer.ended():
            time.sleep(0.05)
    finally:
        out.stop()
        _close(api)


async def _key_check() -> list[bool]:
    """F24 injected down and up, with what the keys saw of it. It goes through the same Raw
    Input window as the service's key, and it is never his key."""
    seen: list[bool] = []
    keys = key_factory()(TEST_KEY, seen.append, on_close=lambda: None)
    keys.start()
    keys.armed = True
    try:
        for down in (True, False):
            inject_key(TEST_KEY, down)
            end = time.monotonic() + KEY_WAIT_S
            while down not in seen and time.monotonic() < end:
                await asyncio.sleep(0.01)
    finally:
        keys.stop()
    return seen


def cmd_selftest(args) -> int:
    root = default_data_dir()
    try:
        cfg = load_config(root / "voice.toml")
        _offline(root)
        if args.guid is None:
            clip, path = _render_selftest(root, cfg)
            who = ""
        else:
            specs = Path(args.specs) if args.specs else mind_voices_dir()
            clip, path, who = _render_member(root, cfg, args.guid, specs, args.cpu)
    except Exception as e:  # a bad config, models.ModelError, a missing tts extra
        return _fail("selftest", e)
    print(f"ok   wrote {path}{f' ({who})' if who else ''}")
    if args.play:
        _play(clip, cfg)
        print("ok   played")
    if args.ptt:
        seen = asyncio.run(_key_check())
        if True in seen and False in seen:
            print("ok   ptt: F24 seen down and up")
        else:
            print(f"FAIL ptt: F24 seen {seen}")
            return 1
    return 0


def _write_json(path: Path, data) -> None:
    import json
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def cmd_cast(args) -> int:
    """A voice for every member of the brief (cast.py): new members and --recast ones are cast,
    the rest keep their specs. Writes the specs to --out, the cast report to
    <data dir>/cast/report.json, and renders each changed member's stock callouts."""
    from datetime import date

    from . import cast
    from .stock import StockCache
    root = default_data_dir()
    try:
        recast = {int(x) for x in args.recast.split(",") if x.strip()} if args.recast else set()
        cfg = load_config(root / "voice.toml")
        decision = load_decision(root / "decision.toml")
        brief = cast.load_brief(Path(args.brief))
        out = Path(args.out)
        existing = cast.load_existing(out)
        _offline(root)
        engine, bank = load_engine(root, cfg)
        embed = load_embedder(root)
        report: dict = {}
        specs = cast.cast_voices(
            brief, bank, decision=decision, embed=embed,
            render=cast.make_render(engine, bank, decision), existing=existing,
            threshold=args.threshold, tries=args.tries, today=date.today().isoformat(), recast=recast,
            report=report)
        changed = [s for s in specs if existing.get(s.guid) != s]
        for spec in changed:
            cast.write_spec(out, spec)
        report["changed"] = [s.guid for s in changed]
        _write_json(root / "cast" / "report.json", report)
        stock = StockCache(root / "cache" / "stock", engine, bank, decision)
        for spec in changed:
            stock.drop_stale(spec)
            stock.render_missing(spec)
    except Exception as e:  # CastError, DecisionError, models.ModelError, a missing cast extra
        return _fail("cast", e)
    print(f"ok   cast: {len(specs)} members, {len(changed)} written to {out}")
    if report["flagged"]:
        print("note flagged (too like another voice after "
              f"{args.tries} tries): {', '.join(map(str, report['flagged']))}")
    if report["skipped_locked"]:
        print("note locked, not recast: " + ", ".join(map(str, report["skipped_locked"])))
    print(f"ok   report {root / 'cast' / 'report.json'}")
    return 0


def cmd_references(args) -> int:
    """A reference clip for every unlocked member (cast.render_designed_references), then their
    callouts in the new voice. On the CPU, with the design engine in the kit's venv and the
    chosen engine in its own; a run that stops goes on where it was."""
    from . import cast
    from .stock import StockCache
    root = default_data_dir()
    made = None
    try:
        cfg = load_config(root / "voice.toml")
        decision = load_decision(root / "decision.toml")
        brief = cast.load_brief(Path(args.brief))
        if args.only:
            only = {int(x) for x in args.only.split(",") if x.strip()}
            brief = [e for e in brief if e.guid in only]
        out = Path(args.specs) if args.specs else mind_voices_dir()
        _offline(root)
        engine, bank = load_engine(root, cfg)

        def stock() -> StockCache:
            nonlocal made
            made = load_expressive(root, cfg, decision, device="cpu", refs_dir=out)
            made.start()
            return StockCache(root / "cache" / "stock", made, bank, decision)
        report = cast.render_designed_references(
            brief, design_source(root, cfg, decision, engine, bank), out, decision=decision,
            stock=None if args.no_stock else stock)
        _write_json(root / "cast" / "references.json", report)
    except Exception as e:  # CastError, DecisionError, EngineError, models.ModelError
        return _fail("references", e)
    finally:
        if made is not None:
            made.stop()
    print(f"ok   references: {len(report['written'])} written, {len(report['kept'])} kept, "
          f"to {out}")
    for key, text in (("locked", "locked, unchanged"), ("uncast", "no voice spec yet, skipped"),
                      ("failed", "failed, unchanged")):
        if report[key]:
            print(f"note {text}: {', '.join(map(str, report[key]))}")
    print(f"ok   report {root / 'cast' / 'references.json'}")
    return 0


def cmd_audition(args) -> int:
    import json
    from dataclasses import replace

    from . import cast
    from .audition import build_audition
    from .engines import EngineError
    root = default_data_dir()
    made = None
    try:
        cfg = load_config(root / "voice.toml")
        decision = load_decision(root / "decision.toml")
        lexicon = load_lexicon(root)
        specs_dir = Path(args.specs) if args.specs else mind_voices_dir()
        specs = cast.load_existing(specs_dir)
        if not specs:
            raise ValueError("no voice specs to audition: run cast first")
        try:
            report = json.loads((root / "cast" / "report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError("no cast report: run cast first") from None
        _offline(root)
        needs_kokoro = any(s.engine == "kokoro" for s in specs.values())
        engine, bank = load_engine(root, cfg) if needs_kokoro else (None, None)

        def expressive(name):
            nonlocal made
            if made is None or made.name != name:
                if made is not None:
                    made.stop()
                    made = None
                # Locked specs can retain an earlier engine after the decision changes.
                selected = replace(decision, engine=name, featured={})
                made = load_expressive(root, cfg, selected, device="cpu", refs_dir=specs_dir)
                if made is None:
                    raise EngineError(f"{name}: no engine for audition")
                made.start()
            return made

        # Reuse one worker for each engine's members, then release it before the next engine.
        ordered = sorted(specs.values(), key=lambda s: (s.engine != "kokoro", s.engine, s.guid))
        page = build_audition(ordered, engine, root / "audition",
                              report=report, bank=bank, decision=decision, grit=cfg.comms_grit,
                              lexicon=lexicon, expressive=expressive)
    except Exception as e:
        return _fail("audition", e)
    finally:
        if made is not None:
            made.stop()
    print(f"ok   audition page: {page}")
    return 0


def cmd_lock(args) -> int:
    from . import cast
    try:
        changed = cast.lock_specs(Path(args.specs) if args.specs else mind_voices_dir(),
                                  args.which)
    except cast.CastError as e:
        return _fail("lock", e)
    print(f"ok   locked {len(changed)}" + (": " + ", ".join(map(str, changed)) if changed else ""))
    return 0


def cmd_run(args) -> int:
    root = default_data_dir()
    try:
        cfg = load_config(root / "voice.toml")
        decision = load_decision(root / "decision.toml")
        load_lexicon(root)
    except (ConfigError, DecisionError, LexiconError) as e:
        return _fail("run", e)
    if lock_held(root):  # before the models load
        return _fail("run", ServiceLocked(f"{root / LOCK_FILE} is held by a running service"))
    _offline(root)
    teardown = setup_logs(root)
    devices = sessions = None
    try:
        from .logs import log
        from .service import SystemClock, VoiceService
        try:
            engine, bank = load_engine(root, cfg)
        except Exception as e:  # models.ModelError, a missing tts extra, a model that will not load
            log("engine_unavailable", error=str(e))
            return _fail("run", e)
        devices, sessions = device_api(), session_api()
        service = VoiceService(cfg, decision, root, clock=SystemClock(), engine=engine, bank=bank,
                               devices=devices, sessions=sessions, keys_factory=key_factory(),
                               recognizer=load_recognizer(root, cfg),
                               corrector=load_corrector(root))
        attach_gpu(service, root, cfg)  # watches the budget either way; loads only on "auto"
        attach_expressive(service, root, cfg, decision)
        try:
            asyncio.run(service.run())
        except ServiceLocked as e:
            return _fail("run", e)
        except KeyboardInterrupt:  # Ctrl+C in a terminal: run() has stopped cleanly
            pass
        except OSError as e:
            return _fail("run", e)
        return 0
    finally:
        _close(sessions)
        _close(devices)
        teardown()


def main(argv: list[str] | None = None) -> int:
    utf8_stdio(sys.stdout, sys.stderr)
    parser = argparse.ArgumentParser(prog="leeroy_voice")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="the service: holds <data dir>/voice.lock, writes "
                                       "voice.pid, logs to <data dir>/logs")
    p_run.set_defaults(func=cmd_run)
    p_where = sub.add_parser("where", help="print data_dir, mind_port and ui_port, one line each")
    p_where.set_defaults(func=cmd_where)
    p_held = sub.add_parser("held", help="exit 0 and print the pid when a service holds this "
                                         "data dir, else exit 1")
    p_held.set_defaults(func=cmd_held)
    p_doc = sub.add_parser("doctor", help="check Python, voice.toml, decision.toml, the models, "
                                          "the devices, the ports and Windows' ducking; one ok, "
                                          "note or FAIL line each")
    p_doc.set_defaults(func=cmd_doctor)
    p_dev = sub.add_parser("devices", help="list the WASAPI inputs and outputs by name")
    p_dev.set_defaults(func=cmd_devices)
    p_self = sub.add_parser("selftest", help=f"write debug/{SELFTEST_FILE}: a stock line in a "
                                             "test voice through the chain")
    p_self.add_argument("--play", action="store_true", help="also play it on the output")
    p_self.add_argument("--ptt", action="store_true",
                        help="also inject F24 and report it seen down and up")
    p_self.add_argument("--guid", type=int, help="that member's stock line through their own "
                                                 "engine, while GpuGuard's rule admits it")
    p_self.add_argument("--specs", help="with --guid: the folder of voice specs (default: the "
                                        "mind's mind-data/voices)")
    p_self.add_argument("--cpu", action="store_true",
                        help="with --guid: the engine on the CPU, no admission asked")
    p_self.set_defaults(func=cmd_selftest)
    p_cast = sub.add_parser("cast", help="write a voice spec for every member of the brief "
                                         "(new members only, plus --recast), then render their "
                                         "stock callouts")
    p_cast.add_argument("--brief", required=True, help="brief.json from `voices brief`")
    p_cast.add_argument("--out", required=True, help="the folder of voice specs (mind-data/voices)")
    p_cast.add_argument("--recast", metavar="GUIDS", help="comma-separated guids to cast again "
                                                          "(a locked voice is never changed)")
    p_cast.add_argument("--threshold", type=float, default=0.80,
                        help="ECAPA cosine at which two voices are too alike (default 0.80)")
    p_cast.add_argument("--tries", type=int, default=20, help=argparse.SUPPRESS)
    p_cast.set_defaults(func=cmd_cast)
    p_ref = sub.add_parser("references", help="a reference clip for every unlocked member, on "
                                              "the CPU, then their stock callouts in that voice")
    p_ref.add_argument("--brief", required=True, help="brief.json from `voices brief`")
    p_ref.add_argument("--specs", help="the folder of voice specs (default: the mind's "
                                       "mind-data/voices)")
    p_ref.add_argument("--only", metavar="GUIDS", help="comma-separated guids (default: all)")
    p_ref.add_argument("--no-stock", action="store_true", help="the clips only: no callouts")
    p_ref.set_defaults(func=cmd_references)
    p_aud = sub.add_parser("audition", help="build <data dir>/audition/index.html: everyone "
                                            "reading a line, and the closest pairs")
    p_aud.add_argument("--specs", help="the folder of voice specs (default: the mind's "
                                       "mind-data/voices)")
    p_aud.set_defaults(func=cmd_audition)
    p_lock = sub.add_parser("lock", help="lock voices so a recast leaves them alone")
    p_lock.add_argument("which", help="'all' or comma-separated guids")
    p_lock.add_argument("--specs", help="the folder of voice specs (default: the mind's "
                                        "mind-data/voices)")
    p_lock.set_defaults(func=cmd_lock)
    p_fetch = sub.add_parser("fetch-models",help="fetch the pinned models into <data dir>/models "
                                                  "and check them")
    p_fetch.add_argument("--only", metavar="NAMES",
                         help="comma-separated, of " + ", ".join(models.MODEL_PINS) +
                              " (default: all)")
    p_fetch.set_defaults(func=cmd_fetch_models)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
