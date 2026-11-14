import io
import json

import pytest

from leeroy_selfie import caption
from leeroy_selfie.protocol import Caption, RenderFailed, parse
from leeroy_selfie.render import Renderer
from leeroy_selfie.worker import serve


def chars(text, size):  # a stand-in measure: every character half the font size wide
    return len(text) * size // 2


def line(tmp_path, **over):
    d = {"id": "m1", "op": "caption", "base_path": str(tmp_path / "base.jpg"), "top": "WHEN THE TANK",
         "bottom": "PULLS EARLY", "out_path": str(tmp_path / "meme.jpg"), "nsfw_max": 0.2}
    d.update(over)
    return json.dumps(d).encode()


def test_a_caption_request_parses_in_its_one_strict_form(tmp_path):
    assert parse(line(tmp_path)) == Caption("m1", str(tmp_path / "base.jpg"), "WHEN THE TANK",
                                            "PULLS EARLY", str(tmp_path / "meme.jpg"), 0.2)
    assert parse(line(tmp_path, bottom="")).bottom == ""


@pytest.mark.parametrize("over", [
    {"top": "", "bottom": ""}, {"top": "x" * 61}, {"top": " padded"}, {"top": "tab\there"},
    {"bottom": 5}, {"base_path": "rel.jpg"}, {"out_path": "/abs/meme.png"}, {"nsfw_max": 0.95},
    {"extra": 1}, {"out_path": None},
])
def test_bad_caption_requests_are_refused(tmp_path, over):
    if over.get("out_path", 1) is None:
        over = {"out_path": str(tmp_path / "base.jpg")}  # never writes over its own base
    with pytest.raises(ValueError):
        parse(line(tmp_path, **over))


def test_layout_keeps_every_line_inside_the_margins_and_wraps_long_text():
    boxes = caption.layout("one does not simply pull the boss before the healer is ready",
                           "again", (768, 768), chars)
    assert boxes and all(0 <= l and r <= 768 for _, _, (l, _, r, _) in boxes)
    top = [b for b in boxes if b[2][1] < 384]
    bottom = [b for b in boxes if b[2][1] >= 384]
    assert len(top) == 2 and len(bottom) == 1 and bottom[0][0] == "AGAIN"
    assert all(t == t.upper() for t, _, _ in boxes)
    # the smaller block is not shrunk to the longer one's size
    assert bottom[0][1] > top[0][1]
    assert caption.layout("", "", (768, 768), chars) == []


def test_a_word_too_wide_even_at_the_smallest_size_breaks_between_characters():
    boxes = caption.layout("x" * 60, "", (128, 128), chars)
    assert len(boxes) > 1 and all(r <= 128 for _, _, (_, _, r, _) in boxes)
    assert "".join(t for t, _, _ in boxes) == "X" * 60


def test_caption_draws_both_lines_within_width(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    assert caption.FONT.is_file() and caption.FONT.with_name("OFL.txt").is_file()
    base = Image.new("RGB", (768, 768), (120, 120, 120))
    out, boxes = caption.draw(base, "WHEN THE TANK PULLS", "BEFORE THE HEALS")
    assert out.size == base.size and base.getpixel((384, 30)) == (120, 120, 120)  # a copy
    assert {b[2][1] < 384 for b in boxes} == {True, False}
    for text, size, (l, t, r, b) in boxes:
        assert 0 < l < r < 768 and 0 <= t < b <= 768
        seen = {out.getpixel((x, y)) for x in range(l, r) for y in range(t, b, 3)}
        assert any(min(p) > 230 for p in seen) and any(max(p) < 25 for p in seen)  # white, outlined
    middle = {out.getpixel((x, 384)) for x in range(0, 768, 7)}
    assert middle == {(120, 120, 120)}


class Scored:
    """A renderer with only its classifier stubbed: the real caption path otherwise."""

    def __init__(self, nsfw):
        self.nsfw = nsfw

    caption = Renderer.caption

    def score(self, image):
        return self.nsfw


def test_the_captioned_image_is_scored_and_a_flagged_one_is_never_written(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    Image.new("RGB", (512, 512), (40, 90, 60)).save(tmp_path / "base.jpg", format="JPEG")
    req = parse(line(tmp_path))
    flagged = Scored(0.5).caption(req)
    assert flagged["flagged"] is True and not (tmp_path / "meme.jpg").exists()
    ok = Scored(0.01).caption(req)
    assert ok["flagged"] is False and (ok["width"], ok["height"]) == (512, 512) and ok["lines"] == 2
    with Image.open(tmp_path / "meme.jpg") as written:
        assert written.format == "JPEG" and written.size == (512, 512)
    assert (tmp_path / "base.jpg").exists()  # the base is never touched
    with pytest.raises(RenderFailed, match="missing_base"):
        Scored(0.01).caption(parse(line(tmp_path, base_path=str(tmp_path / "gone.jpg"))))


class Fake:
    info = {"device": "cuda", "free_gib": 13.1, "versions": {}}

    def __init__(self):
        self.seen = []

    def caption(self, req):
        self.seen.append(req.id)
        if req.top == "BOOM":
            raise RenderFailed("missing_base")
        return {"flagged": False, "nsfw": 0.01, "width": 768, "height": 768, "lines": 2,
                "seconds": 0.1}


def test_the_worker_answers_a_caption_and_goes_on_after_a_failure(tmp_path):
    fake, out = Fake(), io.BytesIO()
    data = line(tmp_path, id="a", top="BOOM") + b"\n" + line(tmp_path, id="b") + b"\n"
    assert serve(lambda: fake, stdin=io.BytesIO(data), out=out, on_eof=lambda: None) == 0
    answers = [json.loads(x) for x in out.getvalue().splitlines()][1:]
    assert answers[0] == {"id": "a", "ok": False, "error": "missing_base"}
    assert answers[1]["ok"] is True and answers[1]["flagged"] is False and fake.seen == ["a", "b"]
