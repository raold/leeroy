"""CPU only; needs the fetched tokenizers (fetch-models --only realvisxl) and transformers."""
import pytest

from leeroy_selfie import models
from leeroy_selfie.fit import fit

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
