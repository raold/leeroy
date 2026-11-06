import sys
import types

import pytest

from leeroy_selfie import models, render
from leeroy_selfie.fit import PromptTooLong
from leeroy_selfie.protocol import Render, RenderFailed

from fakes import FakeImage, make_renderer, make_torch, stub_pil


def request(tmp_path, **over):
    d = dict(id="r", phrases=(("a woman", 0), ("smiling", 3)), negative=(("blurry", 1),),
             seed=42, steps=20, cfg=6.0, size=896, out=str(tmp_path / "o.jpg"),
             thumb=str(tmp_path / "t.jpg"), thumb_size=200, nsfw_max=0.2)
    d.update(over)
    return Render(**d)


def test_a_render_writes_both_jpegs_through_tmp_and_returns_the_exact_fields(
        tmp_path, monkeypatch):
    stub_pil(monkeypatch)
    r = make_renderer(make_torch(nsfw=0.05, peak_gib=10.0))
    answer = r.render(request(tmp_path))
    assert set(answer) == {"flagged", "nsfw", "prompt", "dropped", "tokens", "seconds",
                           "peak_gib"}
    assert answer["flagged"] is False and answer["nsfw"] == 0.05
    assert answer["prompt"] == "a woman, smiling" and answer["dropped"] == []
    assert answer["tokens"] == 6 and answer["peak_gib"] == 10.0
    assert (tmp_path / "o.jpg").read_bytes() == b"jpg"
    assert (tmp_path / "t.jpg").read_bytes() == b"jpg"
    assert not list(tmp_path.glob("*.tmp"))
    image = r.pipe.image
    assert image.saved == [(str(tmp_path / "o.jpg.tmp"), "JPEG", 92)]
    # the generator is seeded, and the size is the requested one
    call = r.pipe.calls[0]
    assert call["generator"].seed == 42 and call["generator"].device == "cuda"
    assert (call["width"], call["height"], call["num_inference_steps"],
            call["guidance_scale"]) == (896, 896, 20, 6.0)


def test_the_thumbnail_is_a_lanczos_square_of_the_requested_size(tmp_path, monkeypatch):
    stub_pil(monkeypatch)
    thumbs = []
    original = FakeImage.resize

    def spy(self, size, resample):
        t = original(self, size, resample)
        thumbs.append(t)
        return t

    monkeypatch.setattr(FakeImage, "resize", spy)
    r = make_renderer()
    r.render(request(tmp_path))
    assert [(t.size, t.resample) for t in thumbs] == [((200, 200), "LANCZOS")]
    assert thumbs[0].saved == [(str(tmp_path / "t.jpg.tmp"), "JPEG", 85)]


@pytest.mark.parametrize("score,flagged", [(0.19, False), (0.2, True), (0.5, True)])
def test_flagged_at_or_over_the_threshold_writes_neither_file(tmp_path, monkeypatch,
                                                              score, flagged):
    stub_pil(monkeypatch)
    answer = make_renderer(make_torch(nsfw=score)).render(request(tmp_path, nsfw_max=0.2))
    assert answer["flagged"] is flagged
    assert (tmp_path / "o.jpg").exists() is (not flagged)
    assert (tmp_path / "t.jpg").exists() is (not flagged)


def test_an_out_of_memory_error_answers_oom_and_writes_nothing(tmp_path, monkeypatch):
    stub_pil(monkeypatch)
    with pytest.raises(RenderFailed, match="oom"):
        make_renderer(oom=True).render(request(tmp_path))
    assert not list(tmp_path.iterdir())


def test_the_token_limit_is_two_under_the_window_and_the_count_the_larger_of_both(
        tmp_path, monkeypatch):
    stub_pil(monkeypatch)
    r = make_renderer()
    # tokenizer_2 counts two per word: a 4-word phrase is 8 tokens, 77 - 2 = 75 allowed
    phrases = (("one two three four", 0),) + tuple((f"w{i} x{i} y{i} z{i}", 1 + i % 3)
                                                   for i in range(12))
    answer = r.render(request(tmp_path, phrases=phrases))
    assert answer["tokens"] <= 75
    assert answer["dropped"] and len(answer["prompt"].split()) <= 37
    assert answer["tokens"] == 2 * len(answer["prompt"].replace(",", " ").split())
    with pytest.raises(PromptTooLong):
        r.render(request(tmp_path, phrases=((" ".join("w" * 40 for _ in range(40)), 0),)))
    assert len(r.pipe.calls) == 1  # the refused prompt never reached the pipeline


def fake_stack(monkeypatch, torch, *, pipe_error=None, labels=None, to_error=None):
    log = []

    class Pipe:
        def __init__(self):
            self.scheduler = types.SimpleNamespace(config={"c": 1})

        def to(self, device):
            if to_error and device == "cuda":
                raise to_error
            log.append(("to", device))
            return self

        def set_progress_bar_config(self, **kw):
            log.append(("bars", kw))

    class Sched:
        @staticmethod
        def from_config(config, **kw):
            log.append(("sched", kw))
            return "sched"

    def from_pretrained(path, **kw):
        if pipe_error:
            raise pipe_error
        log.append(("pipe", kw))
        return Pipe()

    diffusers = types.SimpleNamespace(
        __version__="0.1", DPMSolverMultistepScheduler=Sched,
        StableDiffusionXLPipeline=types.SimpleNamespace(from_pretrained=from_pretrained))
    classifier = types.SimpleNamespace(
        config=types.SimpleNamespace(id2label=labels or {0: "normal", 1: "NSFW"}),
        eval=lambda: classifier)
    transformers = types.SimpleNamespace(
        AutoImageProcessor=types.SimpleNamespace(from_pretrained=lambda p, **kw: "proc"),
        AutoModelForImageClassification=types.SimpleNamespace(
            from_pretrained=lambda p, **kw: classifier))
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "diffusers", diffusers)
    monkeypatch.setitem(sys.modules, "transformers", transformers)
    monkeypatch.setattr(models, "check", lambda data, name, quick=False: [(True, "ok")])
    torch.cuda.empty_cache = lambda: log.append(("empty_cache",))
    return log


def reason(tmp_path, **kw):
    with pytest.raises(render.ReadyError) as e:
        render.load(tmp_path, min_free_gib=8, **kw)
    return e.value.reason


def test_load_builds_the_pinned_pipeline_and_reports_ready_info(tmp_path, monkeypatch):
    log = fake_stack(monkeypatch, make_torch(free_gib=13.14))
    r = render.load(tmp_path, min_free_gib=8)
    assert {k: v for k, v in r.info.items() if not k.endswith("_ms")} == {
        "device": "cuda", "free_gib": 13.1, "versions": {"torch": "9.9", "diffusers": "0.1"}}
    assert r.nsfw_index == 1
    assert ("sched", {"use_karras_sigmas": True}) in log
    pipe_kw = next(v for k, v in log if k == "pipe")
    assert pipe_kw == {"torch_dtype": "fp16", "variant": "fp16", "use_safetensors": True,
                       "local_files_only": True}
    assert [k for k, _ in log].index("pipe") < [k for k, _ in log].index("to")
    assert ("to", "cuda") in log and ("bars", {"disable": True}) in log


def test_load_refuses_without_torch_cuda_or_a_cuda_device(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)
    assert reason(tmp_path) == "cuda"
    fake_stack(monkeypatch, make_torch(cuda=False))
    assert reason(tmp_path) == "cuda"
    fake_stack(monkeypatch, make_torch())
    assert reason(tmp_path, device="cpu") == "cuda"


def test_load_answers_vram_when_less_than_min_free_is_left(tmp_path, monkeypatch):
    fake_stack(monkeypatch, make_torch(free_gib=7.9))
    assert reason(tmp_path) == "vram"


def test_a_cuda_out_of_memory_while_moving_to_the_gpu_is_vram_not_model(tmp_path, monkeypatch):
    torch = make_torch()
    fake_stack(monkeypatch, torch, to_error=torch.cuda.OutOfMemoryError("oom"))
    assert reason(tmp_path) == "vram"


def test_the_vram_check_comes_after_the_cpu_load_and_a_bad_model_still_wins(tmp_path,
                                                                           monkeypatch):
    fake_stack(monkeypatch, make_torch(free_gib=1), pipe_error=OSError("bad"))
    assert reason(tmp_path) == "model"


def test_load_answers_model_for_a_check_problem_a_failed_load_or_no_nsfw_label(
        tmp_path, monkeypatch):
    fake_stack(monkeypatch, make_torch())
    monkeypatch.setattr(models, "check", lambda data, name, quick=False: [(False, "bad")])
    assert reason(tmp_path) == "model"
    monkeypatch.setattr(models, "check", lambda data, name, quick=False: [(None, "absent")])
    assert reason(tmp_path) == "model"
    fake_stack(monkeypatch, make_torch(), pipe_error=OSError("bad"))
    assert reason(tmp_path) == "model"
    fake_stack(monkeypatch, make_torch(), labels={0: "a", 1: "b"})
    assert reason(tmp_path) == "model"
    fake_stack(monkeypatch, make_torch(), to_error=RuntimeError("boom"))
    assert reason(tmp_path) == "model"


# -- the start's race with mod-ollama-chat (final review I2) -------------------------------------

def test_the_start_checks_the_models_without_hashing_them(tmp_path, monkeypatch):
    fake_stack(monkeypatch, make_torch())
    asked = []
    monkeypatch.setattr(models, "check",
                        lambda data, name, quick=False: asked.append((name, quick)) or [(True, "ok")])
    render.load(tmp_path, min_free_gib=8)
    assert asked == [("realvisxl", True), ("nsfw", True)]  # the full hash stays in doctor


def test_the_ready_info_says_how_long_the_check_and_the_load_took(tmp_path, monkeypatch):
    fake_stack(monkeypatch, make_torch())
    info = render.load(tmp_path, min_free_gib=8).info
    assert type(info["check_ms"]) is int and type(info["load_ms"]) is int
    assert info["check_ms"] >= 0 and info["load_ms"] >= 0


def test_a_vram_refusal_keeps_the_cpu_pipeline_and_its_retry_loads_nothing_again(
        tmp_path, monkeypatch):
    torch = make_torch()
    free = [7.9]
    torch.cuda.mem_get_info = lambda: (int(free[0] * 2**30), 2**36)
    log = fake_stack(monkeypatch, torch)
    monkeypatch.setattr(render, "RETRY_WAIT_S", 0.0)
    with pytest.raises(render.ReadyError) as e:
        render.load(tmp_path, min_free_gib=8)
    err = e.value
    assert err.reason == "vram" and err.retry is not None
    assert set(err.fields) == {"check_ms", "load_ms", "free_gib"} and err.fields["free_gib"] == 7.9
    with pytest.raises(render.ReadyError) as again:  # still too little: refused, still retryable
        err.retry()
    assert again.value.reason == "vram" and again.value.retry is not None
    free[0] = 13.0  # make_room unloaded Orion
    r = again.value.retry()
    assert [k for k, _ in log].count("pipe") == 1  # loaded once, on the CPU
    assert ("to", "cuda") in log and r.info["free_gib"] == 13.0
    assert r.info["check_ms"] == 0 and r.info["load_ms"] == 0  # nothing checked or loaded again


def test_a_retry_waits_a_little_for_the_vram_an_unload_frees(tmp_path, monkeypatch):
    torch = make_torch()
    reads = iter([7.0, 7.0, 9.0, 12.0])  # the first check, then the retry's polls
    torch.cuda.mem_get_info = lambda: (int(next(reads) * 2**30), 2**36)
    fake_stack(monkeypatch, torch)
    slept = []
    monkeypatch.setattr(render.time, "sleep", slept.append)
    with pytest.raises(render.ReadyError) as e:
        render.load(tmp_path, min_free_gib=8)
    r = e.value.retry()
    assert r.info["free_gib"] == 9.0 and slept == [render.VRAM_POLL_S]


def test_an_out_of_memory_moving_to_the_gpu_moves_back_and_can_be_retried(tmp_path, monkeypatch):
    torch = make_torch()
    log = fake_stack(monkeypatch, torch, to_error=torch.cuda.OutOfMemoryError("oom"))
    with pytest.raises(render.ReadyError) as e:
        render.load(tmp_path, min_free_gib=8)
    assert e.value.reason == "vram" and e.value.retry is not None
    assert ("to", "cpu") in log and ("empty_cache",) in log
