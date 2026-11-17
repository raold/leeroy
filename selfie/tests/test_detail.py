"""The detailer (detail.py): which boxes get a pass, the crops, the feathered paste, the order
and the codes, its request keys and the renderer's refine, without a model or the GPU."""
import io
import json
import types

import pytest

from leeroy_selfie import detail, render
from leeroy_selfie.__main__ import main
from leeroy_selfie.detail import Detail
from leeroy_selfie.protocol import Identity, Render, parse
from leeroy_selfie.worker import serve

from fakes import JPEG, OutOfMemoryError, make_renderer, make_torch, stub_pil

W, H = 832, 1216  # a portrait


def select(boxes, **over):
    kw = dict(pad=detail.FACE_PAD, min_px=detail.MIN_FACE_PX, max_frac=detail.MAX_FACE_FRAC,
              most=detail.MAX_FACES)
    kw.update(over)
    return detail.select(boxes, W, H, **kw)


# -- which boxes, and their crops ---------------------------------------------------------------

def test_a_small_face_gets_a_padded_square_inside_the_frame():
    crops, skipped = select([(380, 200, 90, 120)])
    assert crops == [(305, 140, 545, 380)] and skipped == 0  # 2 x the longer side, centred
    left, top, right, bottom = crops[0]
    assert right - left == bottom - top == 240


def test_a_crop_at_the_edge_is_shifted_inside_and_stays_square():
    crops, _ = select([(0, 0, 100, 100), (760, 1130, 70, 80)])
    assert crops == [(0, 0, 200, 200), (672, 1056, 832, 1216)]


def test_a_face_too_small_or_as_large_as_a_headshots_is_passed_over():
    tiny = (100, 100, 20, 31)  # under MIN_FACE_PX
    headshot = (200, 300, 280, 300)  # 300 > 0.35 x 832
    ok = (300, 300, 200, 290)  # 290 <= 291.2
    crops, skipped = select([headshot, ok, tiny])
    assert len(crops) == 1 and skipped == 2
    assert crops[0][2] - crops[0][0] == 580


def test_at_most_two_faces_and_two_hands_in_the_order_given():
    boxes = [(100 + 150 * i, 100, 60, 60) for i in range(4)]
    crops, skipped = select(boxes)
    assert len(crops) == detail.MAX_FACES == 2 and skipped == 2
    assert [c[0] for c in crops] == [70, 220]  # the first two given
    assert detail.MAX_HANDS == 2


def test_a_crop_that_would_not_be_enlarged_is_passed_over():
    box = (300, 300, 200, 280)  # its crop is 560 wide
    assert select([box], size=1024)[0] and select([box], size=768)[0]
    crops, skipped = detail.select([box], W, H, pad=2.0, min_px=32, max_frac=0.9, most=2,
                                   size=512)
    assert crops == [] and skipped == 1


def test_palm_rows_become_boxes_with_the_surest_first():
    rows = [[10, 20, 50, 70] + [0] * 14 + [0.6], [100, 100, 160, 150] + [0] * 14 + [0.9]]
    assert detail.palm_boxes(rows) == [(100.0, 100.0, 60.0, 50.0, 0.9),
                                       (10.0, 20.0, 40.0, 50.0, 0.6)]


def test_the_hand_crop_is_three_palms_wide():
    crops, _ = detail.select([(400, 600, 60, 50, 0.9)], W, H, pad=detail.HAND_PAD,
                             min_px=detail.MIN_HAND_PX, max_frac=detail.MAX_HAND_FRAC,
                             most=detail.MAX_HANDS)
    assert crops == [(340, 535, 520, 715)]


def test_every_crop_has_its_own_seed_and_the_same_image_the_same_ones():
    seeds = {detail.seed_for(42, kind, i) for kind in ("face", "hand") for i in range(2)}
    assert len(seeds) == 4 and 42 not in seeds
    assert detail.seed_for(42, "face", 0) == detail.seed_for(42, "face", 0)
    assert detail.seed_for(43, "face", 0) != detail.seed_for(42, "face", 0)
    assert all(0 <= detail.seed_for(2**32 - 1, k, i) < 2**32
               for k in ("face", "hand") for i in range(2))


# -- the mask and the paste ---------------------------------------------------------------------

def test_the_mask_is_one_in_the_middle_and_falls_to_nothing_at_every_edge():
    np = pytest.importorskip("numpy")
    m = detail.feather_mask(80, 80, 10)
    assert m.shape == (80, 80) and m.dtype == np.float32
    assert m[40, 40] == 1.0 and (m[10:70, 10:70] == 1.0).all()
    assert m[40, 0] == pytest.approx(0.05) and m[0, 40] == pytest.approx(0.05)
    assert m[40, 79] == pytest.approx(0.05) and m[79, 40] == pytest.approx(0.05)
    assert (np.diff(m[40, :11]) > 0).all()  # a ramp, not a step
    assert m[0, 0] == pytest.approx(0.05) and m.min() > 0 and m.max() == 1.0


def test_an_edge_that_is_the_images_own_has_no_ramp():
    pytest.importorskip("numpy")
    m = detail.feather_mask(80, 60, 10, (True, False, False, True))
    assert m[30, 0] == 1.0 and m[59, 40] == 1.0  # left and bottom are open
    assert m[0, 40] == pytest.approx(0.05) and m[30, 79] == pytest.approx(0.05)
    assert detail.open_edges((0, 10, 200, 210), 832, 1216) == (True, False, False, False)
    assert detail.open_edges((632, 1016, 832, 1216), 832, 1216) == (False, False, True, True)


def test_the_paste_changes_only_its_box_and_blends_at_the_rim():
    np = pytest.importorskip("numpy")
    base = np.full((100, 120, 3), 40, dtype=np.uint8)
    patch = np.full((40, 40, 3), 200, dtype=np.uint8)
    box = (30, 20, 70, 60)
    out = detail.paste(base, patch, box, detail.feather_mask(40, 40, 8))
    assert out.dtype == np.uint8 and out.shape == base.shape
    assert (base == 40).all()  # the base is not written
    outside = np.ones((100, 120), dtype=bool)
    outside[20:60, 30:70] = False
    assert (out[outside] == 40).all()
    assert (out[28:52, 38:62] == 200).all()  # the middle is the patch
    assert 40 < out[40, 30, 0] < 60 and 40 < out[20, 50, 0] < 60  # the rim is nearly the base
    assert (np.diff(out[40, 30:39, 0].astype(int)) > 0).all()


# -- the pass -----------------------------------------------------------------------------------

class Detectors:
    def __init__(self, faces=(), palms=(), face="yunet", palm="palm"):
        self._faces, self._palms, self.face, self.palm = list(faces), list(palms), face, palm

    def faces(self, bgr):
        return self._faces

    def palms(self, bgr):
        return self._palms


def palm(x, y, side, score=0.9):
    return [x, y, x + side, y + side] + [0] * 14 + [score]


def portrait():
    np = pytest.importorskip("numpy")
    from PIL import Image
    return Image.fromarray(np.full((H, W, 3), 90, dtype=np.uint8))


def painter(seen, colour=(250, 10, 10)):
    def refine(kind, index, crop):
        from PIL import Image
        seen.append((kind, index, crop.size))
        return Image.new("RGB", crop.size, colour)
    return refine


def test_hands_go_first_and_faces_last_each_at_the_pass_size_and_pasted_back():
    np = pytest.importorskip("numpy")
    image = portrait()
    det = Detectors(faces=[(380, 200, 90, 120)], palms=[palm(400, 800, 60)])
    seen = []
    out, fields = detail.run(image, det, Detail(True, True, size=896), painter(seen))
    assert seen == [("hand", 0, (896, 896)), ("face", 0, (896, 896))]
    assert {k: fields[k] for k in ("detail_faces", "detail_hands", "detail_skipped",
                                   "detail_code")} == {
        "detail_faces": 1, "detail_hands": 1, "detail_skipped": 0, "detail_code": ""}
    assert type(fields["detail_ms"]) is int and "detail_error" not in fields
    assert out.size == image.size and out is not image
    px = np.asarray(out)
    assert tuple(px[260, 425]) == (250, 10, 10) and tuple(px[830, 430]) == (250, 10, 10)
    assert tuple(px[20, 20]) == (90, 90, 90) and tuple(px[600, 100]) == (90, 90, 90)
    assert (np.asarray(image) == 90).all()  # the image it was given is not written


def test_only_what_the_request_asks_for_is_looked_for():
    det = Detectors(faces=[(380, 200, 90, 120)], palms=[palm(400, 800, 60)])
    seen = []
    _, fields = detail.run(portrait(), det, Detail(True, False), painter(seen))
    assert [s[0] for s in seen] == ["face"] and fields["detail_hands"] == 0
    seen.clear()
    _, fields = detail.run(portrait(), det, Detail(False, True), painter(seen))
    assert [s[0] for s in seen] == ["hand"] and fields["detail_faces"] == 0


def test_nothing_found_returns_the_image_as_it_was():
    image = portrait()
    out, fields = detail.run(image, Detectors(), Detail(True, True), painter([]))
    assert out is image
    assert (fields["detail_faces"], fields["detail_hands"], fields["detail_code"]) == (0, 0, "")


@pytest.mark.parametrize("det, opts, code", [
    (Detectors(face=None), Detail(True, False), "faces_unavailable"),
    (Detectors(palm=None), Detail(False, True), "hands_unavailable"),
    (Detectors(face=None, palm=None), Detail(True, True), "detectors_unavailable"),
    (None, Detail(True, True), "detectors_unavailable"),
])
def test_a_missing_detector_is_a_code_and_never_an_error(det, opts, code):
    image = portrait()
    out, fields = detail.run(image, det, opts, painter([]))
    assert out is image and fields["detail_code"] == code


def test_a_missing_hand_detector_still_lets_the_faces_pass():
    det = Detectors(faces=[(380, 200, 90, 120)], palm=None)
    _, fields = detail.run(portrait(), det, Detail(True, True), painter([]))
    assert (fields["detail_faces"], fields["detail_code"]) == (1, "hands_unavailable")


def test_out_of_memory_ends_the_pass_and_keeps_what_was_pasted():
    np = pytest.importorskip("numpy")
    det = Detectors(faces=[(380, 200, 90, 120)], palms=[palm(400, 800, 60)])
    paint = painter([])

    def refine(kind, index, crop):
        if kind == "face":
            raise detail.Skip("oom")
        return paint(kind, index, crop)
    out, fields = detail.run(portrait(), det, Detail(True, True), refine)
    assert (fields["detail_hands"], fields["detail_faces"], fields["detail_code"]) == (1, 0, "oom")
    px = np.asarray(out)
    assert tuple(px[830, 430]) == (250, 10, 10) and tuple(px[260, 425]) == (90, 90, 90)


def test_any_other_error_is_the_code_error_with_its_class_name_and_the_image_survives():
    image = portrait()

    def refine(kind, index, crop):
        raise KeyError("a prompt must never be in here")
    out, fields = detail.run(image, Detectors(faces=[(380, 200, 90, 120)]), Detail(), refine)
    assert out is image
    assert (fields["detail_code"], fields["detail_error"]) == ("error", "KeyError")
    assert "prompt" not in json.dumps(fields)


def test_a_detector_that_raises_is_unavailable():
    class Broken(Detectors):
        def faces(self, bgr):
            raise RuntimeError("cv2")
    image = portrait()
    out, fields = detail.run(image, Broken(), Detail(), painter([]))
    assert out is image and fields["detail_code"] == "faces_unavailable"


# -- the request --------------------------------------------------------------------------------

def line(tmp_path, **over):
    d = {"id": "r1", "op": "render", "phrases": [["a woman", 0]], "negative": [["blurry", 1]],
         "seed": 7, "steps": 30, "cfg": 5, "size": 1024, "out": str(tmp_path / "a.jpg"),
         "thumb": str(tmp_path / "t.jpg"), "thumb_size": 256, "nsfw_max": 0.2}
    d.update(over)
    return json.dumps(d).encode()


def test_a_render_without_the_keys_has_no_pass_and_is_the_request_it_always_was(tmp_path):
    r = parse(line(tmp_path))
    assert r.detail is None
    assert r == Render("r1", (("a woman", 0),), (("blurry", 1),), 7, 30, 5.0, 1024,
                       str(tmp_path / "a.jpg"), str(tmp_path / "t.jpg"), 256, 0.2)
    assert parse(line(tmp_path, detail_faces=False, detail_hands=False)).detail is None


def test_the_detail_keys_parse_with_their_defaults(tmp_path):
    assert parse(line(tmp_path, detail_faces=True)).detail == Detail(True, False, 0.35, 0.45, 1024)
    assert parse(line(tmp_path, detail_hands=True)).detail == Detail(False, True, 0.35, 0.45, 1024)
    got = parse(line(tmp_path, detail_faces=True, detail_hands=True, detail_face_denoise=0.3,
                     detail_hand_denoise=0.5, detail_size=896)).detail
    assert got == Detail(True, True, 0.3, 0.5, 896)


@pytest.mark.parametrize("over", [
    {"detail_faces": 1}, {"detail_hands": "yes"}, {"detail_faces": True, "detail_size": 512},
    {"detail_faces": True, "detail_size": 1024.0}, {"detail_faces": True, "detail_face_denoise": 0.05},
    {"detail_faces": True, "detail_face_denoise": 0.8}, {"detail_hands": True, "detail_hand_denoise": "0.4"},
    {"detail_size": 800}, {"detail_face_denoise": True}, {"detail": {"faces": True}},
    {"detail_faces": True, "detail_steps": 10},
])
def test_a_bad_detail_key_is_a_bad_request(tmp_path, over):
    with pytest.raises(ValueError):
        parse(line(tmp_path, **over))


# -- the renderer -------------------------------------------------------------------------------

def request(tmp_path, **over):
    d = dict(id="r", phrases=(("a woman", 0), ("smiling", 3)), negative=(("blurry", 1),),
             seed=42, steps=20, cfg=6.0, size=1024, out=str(tmp_path / "o.jpg"),
             thumb=str(tmp_path / "t.jpg"), thumb_size=200, nsfw_max=0.2)
    d.update(over)
    return Render(**d)


def test_a_render_without_a_pass_never_touches_the_detailer(tmp_path, monkeypatch):
    stub_pil(monkeypatch)
    monkeypatch.setattr(detail, "run", lambda *a, **k: pytest.fail("no pass was asked for"))
    r = make_renderer()
    answer = r.render(request(tmp_path))
    assert not [k for k in answer if k.startswith("detail")]
    assert r._i2i is None and r._detectors is None


def test_a_render_with_a_pass_scores_and_writes_the_image_after_it(tmp_path, monkeypatch):
    stub_pil(monkeypatch)
    from fakes import FakeImage
    after = FakeImage((1024, 1024))
    seen = {}

    def run(image, detectors, opts, refine):
        seen.update(image=image, opts=opts)
        return after, {"detail_faces": 1, "detail_hands": 0, "detail_skipped": 0,
                       "detail_ms": 12, "detail_code": ""}
    monkeypatch.setattr(detail, "run", run)
    scored = []
    r = make_renderer(make_torch(nsfw=0.05))
    r.processor = lambda images, return_tensors: scored.append(images) or {}
    opts = Detail(True, False)
    answer = r.render(request(tmp_path, detail=opts))
    assert seen["image"] is r.pipe.image and seen["opts"] is opts
    assert scored == [after]  # the NSFW gate reads the final image
    assert after.saved == [(str(tmp_path / "o.jpg.tmp"), "JPEG", 92)] and not r.pipe.image.saved
    assert {k: v for k, v in answer.items() if k.startswith("detail")} == {
        "detail_faces": 1, "detail_hands": 0, "detail_skipped": 0, "detail_ms": 12,
        "detail_code": ""}


class FakeI2I:
    def __init__(self, oom=False):
        self.calls, self.oom = [], oom

    def __call__(self, **kw):
        self.calls.append(kw)
        if self.oom:
            raise OutOfMemoryError("out of memory")
        return types.SimpleNamespace(images=["drawn"])


def refiner(r, opts=Detail(True, True), identity=None, cfg=6.0):
    return r.refiner(opts, seed=42, steps=20, cfg=cfg, face_prompt="detailed face, a woman",
                     hand_prompt="a natural hand", negative="blurry", identity=identity)


def test_the_refine_draws_each_kind_at_its_strength_with_its_seed_and_prompt():
    r = make_renderer()
    r._i2i = FakeI2I()
    refine = refiner(r, Detail(True, True, 0.3, 0.5, 1024))
    assert refine("hand", 0, "crop-h") == "drawn" and refine("face", 1, "crop-f") == "drawn"
    hand, face = r._i2i.calls
    assert (hand["prompt"], hand["strength"], hand["image"]) == ("a natural hand", 0.5, "crop-h")
    assert (face["prompt"], face["strength"], face["image"]) == ("detailed face, a woman", 0.3,
                                                                 "crop-f")
    assert hand["generator"].seed == detail.seed_for(42, "hand", 0)
    assert face["generator"].seed == detail.seed_for(42, "face", 1)
    for call in (hand, face):
        assert (call["negative_prompt"], call["num_inference_steps"],
                call["guidance_scale"]) == ("blurry", 20, 6.0)
        assert "ip_adapter_image_embeds" not in call  # a plain pipeline has no adapter
    assert r.pipe.calls == []  # the base pipeline renders nothing here


def test_on_an_identity_pipeline_only_the_largest_face_keeps_her_reference(tmp_path, monkeypatch):
    stub_pil(monkeypatch)
    ref = tmp_path / "ref.jpg"
    ref.write_bytes(JPEG)
    r = make_renderer(identity=True)
    r._i2i = FakeI2I()
    refine = refiner(r, identity=Identity(str(ref), (0.0, 0.0, 1.0, 1.0), 0.45))
    refine("hand", 0, "c")
    refine("face", 0, "c")
    refine("face", 1, "c")
    hand, first, second = r._i2i.calls
    assert r.pipe.scales == [0.0, 0.45, 0.0]
    assert first["ip_adapter_image_embeds"] != hand["ip_adapter_image_embeds"]
    assert hand["ip_adapter_image_embeds"] == second["ip_adapter_image_embeds"]  # the blank
    # and without a reference every crop carries the blank at scale 0
    r.pipe.scales.clear()
    refiner(r)("face", 0, "c")
    assert r.pipe.scales == [0.0]


def test_cuda_out_of_memory_in_a_refine_frees_the_cache_and_skips_the_pass():
    torch = make_torch()
    freed = []
    torch.cuda.empty_cache = lambda: freed.append(1)
    r = make_renderer(torch)
    r._i2i = FakeI2I(oom=True)
    with pytest.raises(detail.Skip, match="oom"):
        refiner(r)("face", 0, "c")
    assert freed == [1]


def test_the_img2img_pipeline_is_built_once_from_the_loaded_components(monkeypatch):
    import sys
    built = []

    class Pipeline:
        def __init__(self, **kw):
            built.append(kw)

        def set_progress_bar_config(self, **kw):
            self.bar = kw
    monkeypatch.setitem(sys.modules, "diffusers",
                        types.SimpleNamespace(StableDiffusionXLImg2ImgPipeline=Pipeline))
    r = make_renderer()
    r.pipe.components = {"unet": "u", "vae": "v"}
    r.pipe.config = types.SimpleNamespace(force_zeros_for_empty_prompt=False)
    first = r._img2img()
    assert r._img2img() is first and len(built) == 1
    assert built[0] == {"unet": "u", "vae": "v", "force_zeros_for_empty_prompt": False,
                        "add_watermarker": False}
    assert first.bar == {"disable": True}


def test_the_face_prompt_leads_with_the_detail_words_and_keeps_her_looks(tmp_path, monkeypatch):
    stub_pil(monkeypatch)
    seen = {}

    def detail_pass(image, opts, **kw):
        seen.update(kw)
        return image, {"detail_faces": 0, "detail_hands": 0, "detail_skipped": 0,
                       "detail_ms": 0, "detail_code": ""}
    r = make_renderer()
    monkeypatch.setattr(r, "detail_pass", detail_pass)
    r.render(request(tmp_path, detail=Detail()))
    assert seen["face_prompt"] == detail.FACE_LEAD + ", a woman, smiling"
    assert seen["hand_prompt"] == detail.HAND_LEAD and seen["negative"] == "blurry"
    assert (seen["seed"], seen["steps"], seen["cfg"], seen["identity"]) == (42, 20, 6.0, None)


# -- the worker's answer and the CLI ------------------------------------------------------------

def test_the_answer_carries_the_counts_and_the_log_the_error_class(tmp_path, capsys):
    class Fake:
        info = {"device": "cuda"}

        def render(self, req):
            return {"flagged": False, "nsfw": 0.004, "prompt": "p", "dropped": [], "tokens": 3,
                    "seconds": 0.1, "peak_gib": 1.0, "width": 768, "height": 768,
                    "detail_faces": 1, "detail_hands": 0, "detail_skipped": 2, "detail_ms": 2100,
                    "detail_code": "error", "detail_error": "KeyError"}
    out = io.BytesIO()
    serve(Fake, stdin=io.BytesIO(line(tmp_path, detail_faces=True) + b"\n"), out=out,
          on_eof=lambda: None)
    answer = json.loads(out.getvalue().splitlines()[1])
    assert answer["ok"] is True and answer["detail_code"] == "error"
    assert (answer["detail_faces"], answer["detail_skipped"], answer["detail_ms"]) == (1, 2, 2100)
    assert "detail_error" not in answer
    err = capsys.readouterr().err
    assert "detail_faces=1 detail_hands=0 detail_skipped=2 detail_ms=2100 detail_code=error" in err
    assert "detail_error=KeyError" in err


def test_the_detail_cli_refuses_bad_arguments_before_any_model(tmp_path, capsys):
    import sys
    src = tmp_path / "in.jpg"
    src.write_bytes(JPEG)
    out = str(tmp_path / "out.jpg")
    assert main(["detail", "--in", str(src), "--out", str(src)]) == 1
    assert main(["detail", "--in", str(src), "--out", out, "--no-faces"]) == 1
    assert main(["detail", "--in", str(src), "--out", out, "--denoise", "0.9"]) == 1
    assert main(["detail", "--in", str(src), "--out", out, "--size", "512"]) == 1
    assert main(["detail", "--in", str(src), "--out", out, "--ref", str(tmp_path / "no.jpg")]) == 1
    assert main(["detail", "--in", str(tmp_path / "gone.jpg"), "--out", out]) == 1
    assert "torch" not in sys.modules and not (tmp_path / "out.jpg").exists()
    assert capsys.readouterr().err.count("detail:") == 6


def test_the_detail_cli_boxes_runs_on_the_cpu_and_writes_nothing(tmp_path, capsys):
    pytest.importorskip("numpy")
    import sys
    from PIL import Image
    src = tmp_path / "in.jpg"
    Image.new("RGB", (416, 608), (90, 90, 90)).save(src, format="JPEG")
    out = tmp_path / "out.jpg"
    assert main(["detail", "--in", str(src), "--out", str(out), "--hands", "--boxes"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got == {"width": 416, "height": 608, "faces": [], "hands": [], "skipped": 0,
                   "code": "detectors_unavailable"}  # nothing fetched in the test's data dir
    assert "torch" not in sys.modules and not out.exists()
