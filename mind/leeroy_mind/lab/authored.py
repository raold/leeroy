"""Hand-written variants: `<data dir>/lab/variants/<piece>/<name>.txt`, one variant a file.

The local reflection model's rewordings are mostly a synonym swapped; an author who has read the
replies can aim a variant at one weakness. A file here is tried by the lab like the model's own:
the night's plan prefers a piece that has one waiting (propose.pick_piece's `prefer`), and the
propose unit takes the waiting files of tonight's piece first, in name order, before it asks the
model for the slots left. A file passes the proposer's own screen (propose.screen, `authored`:
the same placeholders, kept phrases, names, explicit and meta checks; only its length band is
its own) or is passed over with its code, and one whose text was already tried (a
prompt_variant row of that piece) is done: the files may stay where they are.

`lab add-variant --piece P --file F` (lab/cli.py) screens a file and copies it here; the folder
can also be filled by hand. The files are prompt wording only: no persona text, none of his
lines, no names (the screen refuses a known name). The folder is in the data dir, so it is in
the guild backup and never in the repo.

No I/O but the folder's files; no logging."""
import re
import unicodedata
from pathlib import Path

from leeroy_mind.talk import pieces

from . import propose

FOLDER = ("lab", "variants")
MAX_BYTES = pieces.MAX_CHARS * 4
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,59}")
META_PREFIX = "lab:authored:"   # meta lab:authored:<prompt_variant id> = the file's name


class AuthoredError(ValueError):
    """A file that cannot be added, in words (never its text)."""


def folder(data_dir, piece: str | None = None) -> Path:
    root = Path(data_dir).joinpath(*FOLDER)
    return root if piece is None else root / piece


def fold(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def read(data_dir, piece: str) -> list[tuple[str, str]]:
    """(name, text) of each readable `.txt` of the piece's folder, in name order, its text folded
    to one paragraph. A link, a file over MAX_BYTES or one that is not UTF-8 is passed over."""
    if piece not in pieces.PIECES:
        return []
    out = []
    try:
        entries = sorted(folder(data_dir, piece).iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    for path in entries:
        try:
            if path.suffix != ".txt" or path.is_symlink() or not path.is_file():
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        if len(raw) > MAX_BYTES:
            continue
        try:
            text = fold(raw.decode("utf-8-sig"))
        except UnicodeError:
            continue
        if text:
            out.append((path.stem, text))
    return out


def waiting(data_dir, piece: str, *, earlier: list[str] = (),
            names_rx=None) -> tuple[list[dict], list[str]]:
    """The piece's files that may be tried now ([{"name", "text", "diff"}], name order) and the
    refusal codes of the files the screen refused. A file already tried (`earlier`: the texts
    of the piece's prompt_variant rows) or the same as an earlier file is neither."""
    seen = {propose._norm(t) for t in earlier}
    ready, refused = [], []
    for name, text in read(data_dir, piece):
        code = propose.screen(piece, text, names_rx=names_rx, seen=seen, authored=True)
        if code is None:
            seen.add(propose._norm(text))
            ready.append({"name": name, "text": text,
                          "diff": propose.diff(pieces.builtin(piece), text)})
        elif code != "same":
            refused.append(code)
    return ready, refused


def pieces_waiting(data_dir, earlier_of, *, names_rx=None) -> set[str]:
    """The pieces with a file that may be tried now; `earlier_of(piece)` gives its tried texts."""
    return {name for name in pieces.NAMES
            if waiting(data_dir, name, earlier=earlier_of(name), names_rx=names_rx)[0]}


def add(data_dir, piece: str, name: str, text: str, *, earlier: list[str] = (),
        names_rx=None) -> Path:
    """Screen `text` and write it as the piece's `<name>.txt` (LF, one paragraph). AuthoredError
    in words for an unknown piece, a bad name, a refused text, or a name already taken by
    another text."""
    if piece not in pieces.PIECES:
        raise AuthoredError(f"unknown piece {piece!r}: one of {', '.join(pieces.NAMES)}")
    if not NAME_RE.fullmatch(name or ""):
        raise AuthoredError("a name is 1-60 letters, digits, dots, dashes or underscores")
    text = fold(text)
    taken = {n: t for n, t in read(data_dir, piece)}
    others = [t for n, t in taken.items() if n != name]
    code = propose.screen(piece, text, names_rx=names_rx, authored=True,
                          seen={propose._norm(t) for t in [*earlier, *others]})
    if code is not None:
        why = propose.problem(piece, text, code)
        if code == "length":
            low, high = propose.length_band(piece, authored=True)
            why = f"it is {len(text)} characters: at most {high}"
        if code == "same":
            why = "it is the built-in wording, another file's text or a variant already tried"
        raise AuthoredError(f"refused ({code}): {why}")
    path = folder(data_dir, piece) / f"{name}.txt"
    if name in taken and propose._norm(taken[name]) != propose._norm(text):
        raise AuthoredError(f"{path.name} is already there with another text: pick another name")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes((text + "\n").encode("utf-8"))
    return path


def names(conn, ids: list[int]) -> dict[int, str]:
    """The file names of the prompt_variant rows `ids` that were hand-written."""
    out = {}
    for vid in ids:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (f"{META_PREFIX}{vid}",)).fetchone()
        if row:
            out[vid] = row[0]
    return out
