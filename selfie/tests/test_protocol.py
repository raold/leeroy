import json

import pytest

from leeroy_selfie.protocol import IdentityUnavailable, Quit, Render, parse


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
    with pytest.raises(ValueError) as e:
        parse(request(tmp_path, **over))
    assert not isinstance(e.value, IdentityUnavailable)


def test_a_missing_key_a_bad_quit_and_non_json_are_bad_requests(tmp_path):
    d = json.loads(request(tmp_path))
    del d["cfg"]
    for line in (json.dumps(d).encode(), b'{"id": "q", "op": "quit", "x": 1}', b"nope",
                 b"[1]", b"\xff", b"x" * 65537):
        with pytest.raises(ValueError):
            parse(line)


def test_an_identity_reference_is_refused_until_the_adapter_exists(tmp_path):
    with pytest.raises(IdentityUnavailable):
        parse(request(tmp_path, identity="abc"))
