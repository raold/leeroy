"""The photo critic's measurements (critic.py) and its op on the wire, without a model; the
`engines` tests read the fetched detectors."""
import io
import json
import math

import pytest

from leeroy_selfie import critic, models
from leeroy_selfie.protocol import Critic, RenderFailed, parse
from leeroy_selfie.worker import serve

np = pytest.importorskip("numpy")


def hand(spread=1.0, fused=()):
    """An open right hand, palm 100 px wide, fingers pointing up; `fused` fingers share the
    middle finger's tip."""
    lm = np.zeros((21, 2))
    lm[0] = (200, 400)
    bases = [(130, 330), (150, 300), (190, 295), (230, 300), (265, 310)]
    for f, (bx, by) in enumerate(bases):
        chain = critic.FINGERS[f]
        dx = (bx - 200) * 0.3 * spread
        for k, i in enumerate(chain):
            lm[i] = (bx + dx * k, by - 35 * k)
    for f in fused:
        lm[critic.FINGERS[f][-1]] = lm[critic.FINGERS[2][-1]]
    return lm


def test_the_palm_anchors_are_the_ssd_grid_in_the_models_order():
    a = critic.anchors()
    assert a.shape == (2016, 2)
    assert np.allclose(a[0], (0.5 / 24, 0.5 / 24)) and np.allclose(a[1], a[0])
    assert np.allclose(a[2], (1.5 / 24, 0.5 / 24))
    assert np.allclose(a[24 * 24 * 2], (0.5 / 12, 0.5 / 12))
    assert np.allclose(a[-1], (11.5 / 12, 11.5 / 12))


def test_an_open_hand_has_five_fingers_and_merged_tips_count_once():
    assert critic.finger_stats(hand()) == {"fingers": 5, "plausible": True,
                                           "spread": critic.finger_stats(hand())["spread"]}
    assert critic.finger_stats(hand(fused=(3,)))["fingers"] == 4
    assert critic.finger_stats(hand(fused=(1, 3, 4)))["fingers"] == 2


def test_a_finger_far_longer_than_the_palm_is_implausible():
    lm = hand()
    lm[12] = (190, -400)  # the middle fingertip miles above its knuckle
    assert critic.finger_stats(lm)["plausible"] is False


def test_level_eyes_over_a_level_mouth_read_as_aligned():
    face = [100, 100, 200, 240, 150, 180, 250, 180, 200, 230, 160, 280, 240, 280, 0.9]
    bgr = np.full((600, 600, 3), 200, dtype=np.uint8)
    got = critic.eye_stats(bgr, face)
    assert got["tilt"] == 0.0 and got["level"] == 0.0 and got["iod"] == 100.0
    tilted = list(face)
    tilted[7] = 210  # her left eye 30 px lower than her right over a level mouth
    got = critic.eye_stats(bgr, tilted)
    assert got["tilt"] > 15 and got["level"] > 0.25


def test_a_dark_iris_off_centre_in_one_eye_only_opens_the_gaze_gap():
    face = [100, 100, 200, 240, 150, 180, 250, 180, 200, 230, 160, 280, 240, 280, 0.9]
    bgr = np.full((600, 600, 3), 200, dtype=np.uint8)
    bgr[176:184, 146:154] = 20  # her right iris centred
    bgr[176:184, 246:254] = 20  # and her left
    same = critic.eye_stats(bgr, face)["gaze_gap"]
    bgr[176:184, 246:254] = 200
    bgr[176:184, 266:272] = 20  # her left iris drifts outwards
    drift = critic.eye_stats(bgr, face)["gaze_gap"]
    assert same < 0.2 < drift


def test_a_small_face_has_no_gaze_reading():
    face = [10, 10, 30, 40, 15, 20, 25, 20, 20, 25, 16, 30, 24, 30, 0.9]
    assert critic.eye_stats(np.zeros((100, 100, 3), dtype=np.uint8), face)["gaze_gap"] is None


def test_cosine_is_bounded_and_zero_for_a_zero_vector():
    assert critic.cosine([1, 0], [1, 0]) == 1.0
    assert critic.cosine([1, 0], [0, 1]) == 0.0
    assert critic.cosine([0, 0], [1, 0]) == 0.0


def test_without_models_every_detector_is_unavailable_and_a_missing_file_fails(tmp_path):
    det = critic.load_detectors(tmp_path)
    assert (det.face, det.palm, det.hand) == (None, None, None)
    with pytest.raises(RenderFailed, match="missing_image"):
        critic.measure(det, str(tmp_path / "gone.jpg"))


def test_the_critic_op_parses_with_and_without_a_reference():
    plain = parse(json.dumps({"id": "k1", "op": "critic", "path": "C:/x/a.jpg"}).encode())
    assert plain == Critic("k1", "C:/x/a.jpg")
    full = parse(json.dumps({"id": "k2", "op": "critic", "path": "C:/x/a.jpg",
                             "ref": "C:/x/r.jpg", "crop": [0, 0, 0.5, 0.5]}).encode())
    assert full == Critic("k2", "C:/x/a.jpg", "C:/x/r.jpg", (0.0, 0.0, 0.5, 0.5))
    for bad in ({"id": "k", "op": "critic", "path": "C:/x/a.jpg", "ref": "C:/x/r.jpg"},
                {"id": "k", "op": "critic", "path": "a.jpg"},
                {"id": "k", "op": "critic", "path": "C:/x/a.jpg", "extra": 1},
                {"id": "k", "op": "critic", "path": "C:/x/a.jpg", "ref": "C:/x/r.jpg",
                 "crop": [0, 0, 0.5, 0.4]}):
        with pytest.raises(ValueError):
            parse(json.dumps(bad).encode())


class Measuring:
    info = {"device": "cpu", "free_gib": 0.0, "versions": {}}

    def __init__(self, *, embedding_only=False, fail=None):
        self.embedding_only = embedding_only
        self.fail = fail
        self.seen = []

    def critic(self, req):
        self.seen.append(req)
        if self.fail is not None:
            raise self.fail
        return {"faces": 1, "hands": 2, "hand": [], "identity": 0.8, "seconds": 0.1}


def run(renderer, *lines):
    out = io.BytesIO()
    data = b"".join(json.dumps(x).encode() + b"\n" for x in lines)
    serve(lambda: renderer, stdin=io.BytesIO(data), out=out, on_eof=lambda: None)
    return [json.loads(x) for x in out.getvalue().splitlines()][1:]


def test_render_and_embed_only_workers_answer_the_critic_and_a_failure_names_its_code():
    line = {"id": "k1", "op": "critic", "path": "C:/x/a.jpg"}
    for renderer in (Measuring(), Measuring(embedding_only=True)):
        got = run(renderer, line)
        assert got == [{"id": "k1", "ok": True, "error": "", "faces": 1, "hands": 2, "hand": [],
                        "identity": 0.8, "seconds": 0.1}]
    assert run(Measuring(fail=RenderFailed("missing_image")), line) == [
        {"id": "k1", "ok": False, "error": "missing_image"}]
    assert run(Measuring(fail=KeyError("x")), line) == [
        {"id": "k1", "ok": False, "error": "KeyError"}]


def test_a_renderer_without_a_critic_answers_bad_request():
    class Old:
        info = {"device": "cpu", "free_gib": 0.0, "versions": {}}
    assert run(Old(), {"id": "k1", "op": "critic", "path": "C:/x/a.jpg"}) == [
        {"id": "k1", "ok": False, "error": "bad_request"}]


def _fetched(data, names) -> bool:
    return all(ok is True for name in names for ok, _ in models.check(data, name, quick=True))


@pytest.mark.engines
def test_the_fetched_detectors_find_nothing_in_a_blank_image(fetched_data_dir, tmp_path):
    if not _fetched(fetched_data_dir, ("yunet",) + models.CRITIC_PINS):
        pytest.skip("the critic's detectors are not fetched")
    from PIL import Image
    path = tmp_path / "blank.jpg"
    Image.new("RGB", (832, 1216), (128, 128, 128)).save(path, "JPEG")
    got = critic.measure(critic.load_detectors(fetched_data_dir), str(path))
    assert got["faces"] == 0 and got["hands"] == 0 and got["eyes"] is None
    assert got["detectors"] == {"faces": "ok", "hands": "ok"}
    assert math.isfinite(got["seconds"])
