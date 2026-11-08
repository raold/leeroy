import os
import sys
import types
from pathlib import Path

import pytest

from leeroy_selfie import grid as grids
from leeroy_selfie import models, render
from leeroy_selfie.fit import PromptTooLong
from leeroy_selfie.protocol import Grid, Identity, Render, RenderFailed

from fakes import JPEG, FakeImage, OutOfMemoryError, make_renderer, make_torch, stub_pil


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
                           "peak_gib", "width", "height"}
    assert (answer["width"], answer["height"]) == (896, 896)
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


def fake_stack(monkeypatch, torch, *, pipe_error=None, labels=None, to_error=None,
               adapter_error=None):
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

        def load_ip_adapter(self, path, **kw):
            if adapter_error:
                raise adapter_error
            log.append(("adapter", (path, kw)))

    def encoder(path, **kw):
        log.append(("encoder", (path, kw)))
        return "encoder"

    def clip_processor(**kw):
        log.append(("clip_processor", kw))
        return "clip-processor"

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
            from_pretrained=lambda p, **kw: classifier),
        CLIPVisionModelWithProjection=types.SimpleNamespace(from_pretrained=encoder),
        CLIPImageProcessor=clip_processor)
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


# -- the identity load ---------------------------------------------------------------------------

def test_identity_load_checks_the_adapter_and_a_plain_load_does_not(tmp_path, monkeypatch):
    fake_stack(monkeypatch, make_torch())
    asked = []

    def check(data, name, quick=False):
        asked.append((name, quick))
        return [(None, "not fetched")] if name == "ipadapter" else [(True, "ok")]
    monkeypatch.setattr(models, "check", check)
    assert render.load(tmp_path, min_free_gib=8).identity is False  # no adapter needed
    assert asked == [("realvisxl", True), ("nsfw", True)]
    asked.clear()
    assert reason(tmp_path, identity=True) == "model"
    assert asked == [("realvisxl", True), ("nsfw", True), ("ipadapter", True)]


def test_identity_load_builds_the_adapter_into_the_pipeline_before_the_gpu(tmp_path, monkeypatch):
    log = fake_stack(monkeypatch, make_torch())
    r = render.load(tmp_path, min_free_gib=8, identity=True)
    assert r.identity is True
    ip = models.model_dir(tmp_path, "ipadapter")
    assert ("encoder", (ip / "models" / "image_encoder",
                        {"torch_dtype": "fp16", "local_files_only": True})) in log
    assert ("clip_processor", {"size": 224, "crop_size": 224}) in log
    pipe_kw = next(v for k, v in log if k == "pipe")
    assert pipe_kw == {"torch_dtype": "fp16", "variant": "fp16", "use_safetensors": True,
                       "local_files_only": True, "image_encoder": "encoder",
                       "feature_extractor": "clip-processor"}
    assert ("adapter", (str(ip), {"subfolder": "sdxl_models",
                                  "weight_name": "ip-adapter-plus-face_sdxl_vit-h.safetensors",
                                  "image_encoder_folder": None,
                                  "local_files_only": True})) in log
    keys = [k for k, _ in log]
    assert keys.index("encoder") < keys.index("pipe") < keys.index("adapter") < keys.index("to")


def test_a_missing_adapter_is_model_and_too_little_vram_retries_with_it(tmp_path, monkeypatch):
    fake_stack(monkeypatch, make_torch(free_gib=1), adapter_error=OSError("no file named"))
    assert reason(tmp_path, identity=True) == "model"  # the file wins over the VRAM
    torch = make_torch()
    free = [7.9]
    torch.cuda.mem_get_info = lambda: (int(free[0] * 2**30), 2**36)
    log = fake_stack(monkeypatch, torch)
    monkeypatch.setattr(render, "RETRY_WAIT_S", 0.0)
    with pytest.raises(render.ReadyError) as e:
        render.load(tmp_path, min_free_gib=8, identity=True)
    assert e.value.reason == "vram"
    free[0] = 14.0
    assert e.value.retry().identity is True
    assert [k for k, _ in log].count("adapter") == 1  # loaded once, on the CPU


# -- the identity render -------------------------------------------------------------------------

def portrait(ref, *, out, scale=0.6, crop=(0.0, 0.0, 1.0, 1.0), **over):
    out = Path(out)
    d = dict(id="p", phrases=(("a woman", 0), ("at a cafe", 3)), negative=(("blurry", 1),),
             seed=9, steps=30, cfg=5.0, size=1024, out=str(out),
             thumb=str(out.with_name(out.stem + ".thumb.jpg")), thumb_size=384, nsfw_max=0.2,
             identity=Identity(str(ref), crop, scale), shape="portrait")
    d.update(over)
    return Render(**d)


def grid(*, tiles, out, tile=256, gutter=8, thumb_size=128):
    out = Path(out)
    return Grid("g", tuple(str(t) for t in tiles), tile, gutter, str(out),
                str(out.with_name(out.stem + ".thumb.jpg")), thumb_size)


@pytest.fixture
def fake_renderer(monkeypatch):
    stub_pil(monkeypatch)
    return make_renderer(identity=True)


@pytest.fixture
def ref(tmp_path):
    path = tmp_path / "ref.jpg"
    path.write_bytes(JPEG)
    return path


def test_ref_box_is_the_square_the_crop_names():
    assert render.ref_box(1024, 1024, (0.125, 0.05, 0.875, 0.8)) == (128, 51, 896, 819)
    assert render.ref_box(1024, 1024, (0.0, 0.0, 1.0, 1.0)) == (0, 0, 1024, 1024)
    assert render.ref_box(800, 600, (0.25, 0.0, 0.75, 0.5)) == (200, 0, 600, 300)
    assert render.ref_box(100, 100, (-0.1, -0.2, 1.3, 1.1)) == (0, 0, 100, 100)  # clamped


def test_embeds_are_computed_once_per_reference_and_again_when_it_changes(tmp_path, fake_renderer):
    ref = tmp_path / "ref.jpg"
    ref.write_bytes(JPEG)
    for _ in range(2):
        fake_renderer.render(portrait(ref, out=tmp_path / "a.jpg"))
    assert fake_renderer.pipe.embed_calls == 1
    os.utime(ref, ns=(1, 1))
    fake_renderer.render(portrait(ref, out=tmp_path / "b.jpg"))
    assert fake_renderer.pipe.embed_calls == 2 and fake_renderer.pipe.calls[-1]["width"] == 832


def test_an_identity_render_is_a_portrait_conditioned_on_the_cropped_reference(
        tmp_path, fake_renderer, ref):
    answer = fake_renderer.render(portrait(ref, out=tmp_path / "a.jpg",
                                           crop=(0.125, 0.05, 0.875, 0.8)))
    pipe = fake_renderer.pipe
    (kw,) = pipe.embedded
    face = kw.pop("ip_adapter_image")
    assert face.box == (128, 51, 896, 819) and face.parent.parent.path == str(ref)
    assert face.parent.mode == "RGB"
    assert kw == {"ip_adapter_image_embeds": None, "device": "cuda", "num_images_per_prompt": 1,
                  "do_classifier_free_guidance": True, "grad_off": True}
    call = pipe.calls[-1]
    assert (call["width"], call["height"]) == (832, 1216)
    assert call["ip_adapter_image_embeds"] == ["embeds-1"]
    assert (answer["width"], answer["height"]) == (832, 1216) and answer["flagged"] is False
    assert (tmp_path / "a.jpg").read_bytes() == b"jpg"
    assert (tmp_path / "a.thumb.jpg").read_bytes() == b"jpg"


def test_a_portrait_thumbnail_keeps_the_aspect_with_its_long_side_the_thumb_size(
        tmp_path, fake_renderer, ref, monkeypatch):
    thumbs = []
    original = FakeImage.resize

    def spy(self, size, resample):
        t = original(self, size, resample)
        thumbs.append(t)
        return t

    monkeypatch.setattr(FakeImage, "resize", spy)
    fake_renderer.render(portrait(ref, out=tmp_path / "a.jpg", thumb_size=384))
    assert [(t.size, t.resample) for t in thumbs] == [((263, 384), "LANCZOS")]
    assert thumbs[0].saved == [(str(tmp_path / "a.thumb.jpg.tmp"), "JPEG", 85)]


def test_a_square_render_keeps_its_size_when_it_carries_an_identity(tmp_path, fake_renderer, ref):
    answer = fake_renderer.render(portrait(ref, out=tmp_path / "a.jpg", shape="square"))
    call = fake_renderer.pipe.calls[-1]
    assert (call["width"], call["height"]) == (1024, 1024)
    assert (answer["width"], answer["height"]) == (1024, 1024)


def test_an_unreadable_reference_answers_missing_ref(tmp_path, monkeypatch):
    image = stub_pil(monkeypatch)
    r = make_renderer(identity=True)
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"\x89PNG not a jpeg")
    for path in (tmp_path / "gone.jpg", bad):
        with pytest.raises(RenderFailed, match="missing_ref"):
            r.render(portrait(path, out=tmp_path / "a.jpg"))
    png = FakeImage()
    png.format = "PNG"  # it opens, as a PNG named .jpg would
    image.open = lambda path: png
    with pytest.raises(RenderFailed, match="missing_ref"):
        r.render(portrait(bad, out=tmp_path / "a.jpg"))
    assert r.pipe.calls == [] and r.pipe.embed_calls == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bad.jpg"]


def test_a_cfg_1_identity_render_passes_embeds_without_the_negative_and_caches_them_apart(
        tmp_path, fake_renderer, ref):
    # diffusers turns CFG off at guidance 1.0; CFG embeds there would fold the negative in
    for cfg, name in ((1.0, "a"), (5.0, "b"), (1.0, "c"), (5.0, "d")):
        fake_renderer.render(portrait(ref, out=tmp_path / f"{name}.jpg", cfg=cfg))
    pipe = fake_renderer.pipe
    assert [kw["do_classifier_free_guidance"] for kw in pipe.embedded] == [False, True]
    assert [(c["guidance_scale"], c["ip_adapter_image_embeds"]) for c in pipe.calls] == [
        (1.0, ["embeds-1"]), (5.0, ["embeds-2"]), (1.0, ["embeds-1"]), (5.0, ["embeds-2"])]


def test_the_scale_is_set_per_request(tmp_path, fake_renderer, ref):
    fake_renderer.render(portrait(ref, out=tmp_path / "a.jpg", scale=0.6))
    fake_renderer.render(portrait(ref, out=tmp_path / "b.jpg", scale=0.45))
    assert fake_renderer.pipe.scales == [0.6, 0.45]
    assert fake_renderer.pipe.embed_calls == 1  # the same reference: its embeds are kept


def test_the_cache_holds_the_last_REF_CACHE_references(tmp_path, fake_renderer, ref):
    crops = [(0.0, i / 100, 0.5, 0.5 + i / 100) for i in range(render.REF_CACHE + 1)]
    for crop in crops:
        fake_renderer.render(portrait(ref, out=tmp_path / "a.jpg", crop=crop))
    assert fake_renderer.pipe.embed_calls == render.REF_CACHE + 1
    fake_renderer.render(portrait(ref, out=tmp_path / "a.jpg", crop=crops[-1]))
    assert fake_renderer.pipe.embed_calls == render.REF_CACHE + 1  # the newest is still held
    fake_renderer.render(portrait(ref, out=tmp_path / "a.jpg", crop=crops[0]))
    assert fake_renderer.pipe.embed_calls == render.REF_CACHE + 2  # the oldest was dropped


def test_an_out_of_memory_encoding_the_reference_answers_oom_and_caches_nothing(
        tmp_path, fake_renderer, ref):
    pipe = fake_renderer.pipe
    prepare = pipe.prepare_ip_adapter_image_embeds

    def oom(**kw):
        raise OutOfMemoryError("out of memory")

    pipe.prepare_ip_adapter_image_embeds = oom
    with pytest.raises(RenderFailed, match="oom"):
        fake_renderer.render(portrait(ref, out=tmp_path / "a.jpg"))
    assert pipe.calls == [] and not (tmp_path / "a.jpg").exists()
    pipe.prepare_ip_adapter_image_embeds = prepare
    fake_renderer.render(portrait(ref, out=tmp_path / "a.jpg"))
    assert pipe.embed_calls == 1  # nothing was kept from the failed one: encoded now


def test_a_flagged_identity_render_writes_nothing(tmp_path, monkeypatch, ref):
    stub_pil(monkeypatch)
    r = make_renderer(make_torch(nsfw=0.5), identity=True)
    answer = r.render(portrait(ref, out=tmp_path / "a.jpg"))
    assert answer["flagged"] is True and (answer["width"], answer["height"]) == (832, 1216)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["ref.jpg"]


def test_a_render_never_ignores_an_identity_and_an_identity_renderer_needs_one(
        tmp_path, monkeypatch, ref):
    stub_pil(monkeypatch)
    plain, ident = make_renderer(), make_renderer(identity=True)
    with pytest.raises(RenderFailed, match="identity_unavailable"):
        plain.render(portrait(ref, out=tmp_path / "a.jpg"))
    with pytest.raises(RenderFailed, match="bad_request"):
        ident.render(request(tmp_path))
    assert plain.pipe.calls == [] == ident.pipe.calls
    assert plain.pipe.scales == [] and plain.pipe.embed_calls == 0


# -- the grid ------------------------------------------------------------------------------------

def test_a_grid_with_a_missing_tile_writes_nothing(tmp_path, fake_renderer):
    (tmp_path / "t0.jpg").write_bytes(JPEG)
    answer = fake_renderer.grid(grid(tiles=[tmp_path / "t0.jpg", tmp_path / "gone.jpg"],
                                     out=tmp_path / "g.jpg"))
    assert answer == {"ok": False, "error": "missing_tile"} and not (tmp_path / "g.jpg").exists()
    (tmp_path / "t1.jpg").write_bytes(b"\x89PNG")  # there, but not a JPEG
    answer = fake_renderer.grid(grid(tiles=[tmp_path / "t0.jpg", tmp_path / "t1.jpg"],
                                     out=tmp_path / "g.jpg"))
    assert answer == {"ok": False, "error": "missing_tile"}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["t0.jpg", "t1.jpg"]


def test_a_grid_pastes_each_tile_resized_at_its_box_on_the_ground(tmp_path, monkeypatch):
    image = stub_pil(monkeypatch)
    resized = []
    original = FakeImage.resize

    def spy(self, size, resample):
        t = original(self, size, resample)
        resized.append(t)
        return t

    monkeypatch.setattr(FakeImage, "resize", spy)
    tiles = []
    for i in range(5):
        tiles.append(tmp_path / f"t{i}.jpg")
        tiles[-1].write_bytes(JPEG)
    answer = make_renderer().grid(grid(tiles=tiles, out=tmp_path / "g.jpg", tile=256, gutter=8,
                                       thumb_size=200))
    width, height = grids.canvas(5, 256, 8)
    assert (width, height) == (800, 536)
    assert set(answer) == {"ok", "width", "height", "seconds", "error", "similarity"}
    assert answer["similarity"] == {"code": "encoder_unavailable"}
    assert answer["ok"] is True and answer["error"] == "" and type(answer["seconds"]) is float
    assert (answer["width"], answer["height"]) == (width, height)
    (canvas,) = image.made
    assert (canvas.mode, canvas.size, canvas.color) == ("RGB", (width, height), grids.GROUND)
    assert [box for _, box in canvas.pasted] == grids.boxes(5, 256, 8)
    assert [(t.size, t.resample) for t, _ in canvas.pasted] == [((256, 256), "LANCZOS")] * 5
    assert [t.parent.parent.path for t, _ in canvas.pasted] == [str(p) for p in tiles]
    assert canvas.saved == [(str(tmp_path / "g.jpg.tmp"), "JPEG", 92)]
    (thumb,) = [t for t in resized if t.parent is canvas]
    assert (thumb.size, thumb.resample) == ((200, 134), "LANCZOS")
    assert thumb.saved == [(str(tmp_path / "g.thumb.jpg.tmp"), "JPEG", 85)]
    assert (tmp_path / "g.jpg").read_bytes() == (tmp_path / "g.thumb.jpg").read_bytes() == b"jpg"
    assert not list(tmp_path.glob("*.tmp"))
