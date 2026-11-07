import json
import os

import pytest

from leeroy_selfie.protocol import Grid, Identity, Quit, Render, Retry, parse

ABS_JPG = os.path.abspath("/abs/ref.jpg")


def request(tmp_path, **over):
    d = {"id": "r1", "op": "render", "phrases": [["a woman", 0], ["smiling", 3]],
         "negative": [["blurry", 1]], "seed": 7, "steps": 30, "cfg": 5, "size": 1024,
         "out": str(tmp_path / "a.jpg"), "thumb": str(tmp_path / "t.jpg"),
         "thumb_size": 256, "nsfw_max": 0.2}
    d.update(over)
    return json.dumps(d).encode()


def test_a_render_request_parses_in_its_one_strict_form(tmp_path):
    r = parse(request(tmp_path))
    assert r == Render("r1", (("a woman", 0), ("smiling", 3)), (("blurry", 1),), 7, 30, 5.0,
                       1024, str(tmp_path / "a.jpg"), str(tmp_path / "t.jpg"), 256, 0.2)
    assert parse(b'{"id": "q", "op": "quit"}') == Quit("q")


@pytest.mark.parametrize("over", [
    {"extra": 1}, {"seed": -1}, {"seed": 2**32}, {"seed": True}, {"steps": 9}, {"steps": 61},
    {"cfg": 12.5}, {"cfg": "5"}, {"size": 512}, {"size": 800}, {"thumb_size": 64},
    {"nsfw_max": 0.95}, {"nsfw_max": 0}, {"id": ""}, {"id": "x" * 33}, {"id": 5},
    {"out": "relative/a.jpg"}, {"thumb": "t.jpg"}, {"out": "/abs/a.png"},
    {"phrases": [["a", 10]]}, {"phrases": [["a"]]}, {"phrases": [[1, 0]]},
    {"phrases": "a"}, {"negative": [["", 0]]}, {"op": "other"},
])
def test_unknown_keys_wrong_types_and_relative_paths_are_bad_requests(tmp_path, over):
    with pytest.raises(ValueError):
        parse(request(tmp_path, **over))


def test_a_missing_key_a_bad_quit_and_non_json_are_bad_requests(tmp_path):
    d = json.loads(request(tmp_path))
    del d["cfg"]
    for line in (json.dumps(d).encode(), b'{"id": "q", "op": "quit", "x": 1}', b"nope",
                 b"[1]", b"\xff", b"x" * 65537):
        with pytest.raises(ValueError):
            parse(line)


def test_an_identity_render_parses_with_a_square_crop_and_a_portrait_shape(tmp_path):
    r = parse(request(tmp_path, identity={"ref": ABS_JPG, "crop": [0.125, 0.05, 0.875, 0.8],
                                          "scale": 0.6}, shape="portrait"))
    assert r.identity == Identity(ABS_JPG, (0.125, 0.05, 0.875, 0.8), 0.6)
    assert r.shape == "portrait"
    plain = parse(request(tmp_path))
    assert plain.identity is None and plain.shape == "square"


@pytest.mark.parametrize("bad", [
    {"ref": "rel.jpg", "crop": [0, 0, 1, 1], "scale": 0.6},
    {"ref": ABS_JPG, "crop": [0, 0, 1, 0.5], "scale": 0.6},
    {"ref": ABS_JPG, "crop": [0, 0, 1, 1], "scale": 1.2},
    {"ref": ABS_JPG, "crop": [0, 0, 1, 1]},
    {"ref": ABS_JPG, "crop": [0, 0, 1, 1], "scale": 0.6, "x": 1},
    {"ref": ABS_JPG, "crop": [0.5, 0, 0.5, 0], "scale": 0.6},
    {"ref": ABS_JPG, "crop": [0, 0, 1], "scale": 0.6},
    {"ref": ABS_JPG, "crop": [0, 0, 1, 1.5], "scale": 0.6},
    {"ref": ABS_JPG, "crop": [0, 0, 1, 1], "scale": 0.2},
    {"ref": ABS_JPG, "crop": [float("nan"), 0, 1, 1], "scale": 0.6},
    {"ref": ABS_JPG, "crop": [0, 0, float("inf"), 1], "scale": 0.6},
    {"ref": ABS_JPG, "crop": [False, False, True, True], "scale": 0.6},
    {"ref": ABS_JPG, "crop": [None, 0, 1, 1], "scale": 0.6},
    {"ref": ABS_JPG, "crop": None, "scale": 0.6},
    {"ref": ABS_JPG, "crop": [0, 0, 1, 1], "scale": float("nan")},
    {"ref": ABS_JPG, "crop": [0, 0, 1, 1], "scale": True},
    {"ref": None, "crop": [0, 0, 1, 1], "scale": 0.6},
    "abc", None])
def test_a_bad_identity_is_a_bad_request(tmp_path, bad):
    with pytest.raises(ValueError):
        parse(request(tmp_path, identity=bad))


def test_nan_and_null_in_the_raw_json_are_bad_requests(tmp_path):
    good = request(tmp_path, identity={"ref": ABS_JPG, "crop": [0, 0, 1, 1], "scale": 0.6})
    assert parse(good).identity is not None
    for old, new in ((b"[0, 0, 1, 1]", b"[NaN, 0, 1, 1]"), (b"[0, 0, 1, 1]", b"[0, 0, 1, null]"),
                     (b'"scale": 0.6', b'"scale": NaN')):
        assert old in good
        with pytest.raises(ValueError):
            parse(good.replace(old, new))
    with pytest.raises(ValueError):
        parse(request(tmp_path, shape=None))


def test_portrait_needs_size_1024(tmp_path):
    assert parse(request(tmp_path, shape="portrait")).shape == "portrait"
    for over in ({"shape": "portrait", "size": 896}, {"shape": "wide"}):
        with pytest.raises(ValueError):
            parse(request(tmp_path, **over))


def grid_line(tmp_path, **over):
    d = {"id": "g1", "op": "grid", "tiles": [str(tmp_path / f"{i}.jpg") for i in range(3)],
         "tile": 512, "gutter": 8, "out": str(tmp_path / "g.jpg"),
         "thumb": str(tmp_path / "gt.jpg"), "thumb_size": 384}
    d.update(over)
    return json.dumps(d).encode()


def test_a_grid_request_parses_and_17_tiles_do_not(tmp_path):
    g = parse(grid_line(tmp_path))
    assert g == Grid("g1", tuple(str(tmp_path / f"{i}.jpg") for i in range(3)), 512, 8,
                     str(tmp_path / "g.jpg"), str(tmp_path / "gt.jpg"), 384)
    for over in ({"tiles": [str(tmp_path / "a.jpg")] * 17}, {"tiles": []}, {"tiles": ["a.jpg"]},
                 {"tile": 255}, {"gutter": 33}, {"thumb_size": 127}, {"extra": 1},
                 {"out": "g.jpg"}):
        with pytest.raises(ValueError):
            parse(grid_line(tmp_path, **over))


def test_deep_nesting_and_unencodable_strings_are_value_errors(tmp_path):
    with pytest.raises(ValueError):
        parse(b"[" * 5000)
    with pytest.raises(ValueError):
        parse(rb'{"id": "\ud800", "op": "quit"}')
    with pytest.raises(ValueError):
        parse(request(tmp_path, phrases=[["bad \ud800", 0]]))


def test_a_retry_after_a_vram_refusal_parses_in_its_one_strict_form():
    assert parse(b'{"id": "ready", "op": "retry"}') == Retry("ready")
    for line in (b'{"id": "ready", "op": "retry", "x": 1}', b'{"op": "retry"}'):
        with pytest.raises(ValueError):
            parse(line)
