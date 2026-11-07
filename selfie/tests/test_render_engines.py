"""CPU only; the tokenizer check needs the fetched tokenizers (fetch-models --only realvisxl) and
transformers, the grid check only Pillow (the gpu extra's)."""
import pytest

from leeroy_selfie import models
from leeroy_selfie.fit import fit
from leeroy_selfie.grid import GROUND, boxes, compose
from leeroy_selfie.protocol import Grid

pytestmark = pytest.mark.engines

NEGATIVE = [("child, teenager", 0), ("nude, nsfw, explicit", 0), ("deformed, blurry", 1),
            ("extra fingers, bad hands, bad anatomy", 2), ("watermark, text, logo", 3)]


@pytest.fixture(scope="module")
def tokenizers(fetched_data_dir):
    pytest.importorskip("transformers")
    from transformers import CLIPTokenizer
    if any(ok is not True for ok, _ in models.check(fetched_data_dir, "realvisxl", quick=True)):
        pytest.skip("realvisxl is not fetched")
    d = models.model_dir(fetched_data_dir, "realvisxl")
    return [CLIPTokenizer.from_pretrained(d / sub, local_files_only=True)
            for sub in ("tokenizer", "tokenizer_2")]


def test_the_negative_and_a_longest_invented_prompt_fit_both_tokenizers(tokenizers):
    limit = tokenizers[0].model_max_length - 2

    def count(text):
        return max(len(t(text, add_special_tokens=False).input_ids) for t in tokenizers)

    long_prompt = [("photo of a woman in her thirties", 0)] + [
        (f"detail number {i} with several descriptive words", 1 + i % 8) for i in range(40)]
    for phrases in (NEGATIVE, long_prompt):
        text, _ = fit(phrases, count, limit)
        assert 0 < count(text) <= limit
        assert all(len(t(text).input_ids) <= limit + 2 for t in tokenizers)


TOLERANCE = 6  # JPEG at quality 92 moved these by at most 2 levels (Pillow 13.0.0)


def near(pixel, colour):
    return all(abs(a - b) <= TOLERANCE for a, b in zip(pixel, colour))


def test_a_grid_composites_solid_tiles_onto_the_ground(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    colours = [(200, 40, 40), (40, 180, 60), (40, 70, 200), (210, 190, 40), (180, 60, 180)]
    tiles = []
    for i, colour in enumerate(colours):
        tiles.append(str(tmp_path / f"t{i}.jpg"))
        Image.new("RGB", (64, 64), colour).save(tiles[-1], format="JPEG", quality=95)
    out, thumb = tmp_path / "g.jpg", tmp_path / "g.thumb.jpg"
    answer = compose(Grid("g", tuple(tiles), 32, 4, str(out), str(thumb), 56))
    assert (answer["width"], answer["height"]) == (112, 76)
    with Image.open(out) as img:
        assert img.format == "JPEG" and img.size == (112, 76)
        rgb = img.convert("RGB")
    assert near(rgb.getpixel((1, 1)), GROUND)
    for (left, top, right, bottom), colour in zip(boxes(5, 32, 4), colours):
        assert near(rgb.getpixel(((left + right) // 2, (top + bottom) // 2)), colour)
    with Image.open(thumb) as small:
        assert small.format == "JPEG" and small.size == (56, 38)
    assert not list(tmp_path.glob("*.tmp"))
