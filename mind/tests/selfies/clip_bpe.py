"""CLIP's byte-pair tokenizer, as the selfie worker's text encoders count tokens, in plain Python
for tests only: the vocab.json and merges.txt of a local RealVisXL download (`tokenizer` and
`tokenizer_2`), read from disk, never fetched. Tests that need it skip when the files are not
there (CI has none). The worker counts the larger of the two."""
import json
import os
import re
from functools import lru_cache
from pathlib import Path

# CLIP's pre-tokenizer: letters runs, single digits, punctuation runs (`regex`'s \p{L} and
# \p{N}, written for `re`); each piece is byte-pair encoded on its own
_PIECES = re.compile(r"'s|'t|'re|'ve|'m|'ll|'d|[^\W\d_]+|\d|[^\s\w]+|_+", re.IGNORECASE)


def _bytes_to_unicode() -> dict[int, str]:
    bs = (list(range(ord("!"), ord("~") + 1)) + list(range(ord("¡"), ord("¬") + 1))
          + list(range(ord("®"), ord("ÿ") + 1)))
    cs, n = bs[:], 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return dict(zip(bs, map(chr, cs)))


class ClipBpe:
    def __init__(self, folder: Path):
        self.encoder = json.loads((folder / "vocab.json").read_text(encoding="utf-8"))
        lines = (folder / "merges.txt").read_text(encoding="utf-8").split("\n")[1:]
        merges = [tuple(m.split()) for m in lines if len(m.split()) == 2]
        self.ranks = {m: i for i, m in enumerate(merges)}
        self.byte = _bytes_to_unicode()
        self.cache: dict[str, int] = {}

    def _bpe(self, piece: str) -> int:
        if piece in self.cache:
            return self.cache[piece]
        word = tuple(piece[:-1]) + (piece[-1] + "</w>",)
        while len(word) > 1:
            pairs = {(word[i], word[i + 1]) for i in range(len(word) - 1)}
            best = min(pairs, key=lambda p: self.ranks.get(p, float("inf")))
            if best not in self.ranks:
                break
            out, i = [], 0
            while i < len(word):
                if i < len(word) - 1 and (word[i], word[i + 1]) == best:
                    out.append(word[i] + word[i + 1])
                    i += 2
                else:
                    out.append(word[i])
                    i += 1
            word = tuple(out)
        self.cache[piece] = len(word)
        return len(word)

    def count(self, text: str) -> int:
        text = " ".join(text.split()).lower()
        return sum(self._bpe("".join(self.byte[b] for b in piece.encode("utf-8")))
                   for piece in _PIECES.findall(text))


@lru_cache(maxsize=1)
def tokenizers() -> tuple[ClipBpe, ...] | None:
    """Both of RealVisXL's tokenizers, or None when the files are not on this machine. Looked up
    under LEEROY_SELFIE_DATA, then the checkout's selfie-data."""
    roots = [Path(os.environ["LEEROY_SELFIE_DATA"])] if os.environ.get("LEEROY_SELFIE_DATA") else []
    roots.append(Path(__file__).resolve().parents[3] / "selfie-data")
    for root in roots:
        base = root / "models" / "realvisxl"
        folders = [base / "tokenizer", base / "tokenizer_2"]
        if all((f / "vocab.json").is_file() and (f / "merges.txt").is_file() for f in folders):
            return tuple(ClipBpe(f) for f in folders)
    return None


def count(text: str) -> int:
    """The worker's count of `text`: the larger of the two tokenizers' (no start or end token)."""
    return max(t.count(text) for t in tokenizers())
