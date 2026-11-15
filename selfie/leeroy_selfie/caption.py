"""A meme's classic caption (the mind's culture memes): outlined capitals at the top and the
bottom of an existing image, each block sized to the width, drawn with Pillow in the bundled
Anton (SIL Open Font License 1.1; its licence text is fonts/OFL.txt beside the font).

layout() is pure arithmetic over a measuring function, so it is tested without Pillow; draw()
imports Pillow inside. The text is the mind's screened caption, drawn in capitals as given; it
never reaches a diffusion prompt."""
from pathlib import Path
from typing import Callable

FONT = Path(__file__).with_name("fonts") / "Anton-Regular.ttf"
MARGIN = 0.04       # of the width on each side, and of the height above the top block and below the bottom
MAX_SIZE = 0.11     # of the height: the largest font a block may take
MIN_SIZE = 10       # px: the smallest; a word still too wide then breaks between characters
MAX_LINES = 2       # a block wraps to at most this many lines (one more only at MIN_SIZE)
STROKE = 0.08       # the black outline, as a share of the font size, at least 1 px
LEADING = 1.12      # line spacing, as a multiple of the font size
FILL, OUTLINE = (255, 255, 255), (0, 0, 0)

Measure = Callable[[str, int], int]  # (text, font size) -> its drawn width in px, outline included


def stroke(size: int) -> int:
    return max(1, round(size * STROKE))


def _wrap(words: list[str], size: int, measure: Measure, width: int, most: int) -> list[str] | None:
    """Words greedily into at most `most` lines no wider than `width`; None when they do not fit."""
    lines: list[str] = []
    for word in words:
        if measure(word, size) > width:
            return None
        if lines and measure(lines[-1] + " " + word, size) <= width:
            lines[-1] += " " + word
        else:
            lines.append(word)
            if len(lines) > most:
                return None
    return lines


def _broken(text: str, size: int, measure: Measure, width: int) -> list[str]:
    """At the smallest size: the words wrapped, a word wider than the line split by characters."""
    pieces: list[str] = []
    for word in text.split():
        while word and measure(word, size) > width:
            cut = max(1, max(n for n in range(1, len(word) + 1) if n == 1 or measure(word[:n], size) <= width))
            pieces.append(word[:cut])
            word = word[cut:]
        if word:
            pieces.append(word)
    return _wrap(pieces, size, measure, width, len(pieces)) or pieces


def fit(text: str, measure: Measure, *, width: int, height: int) -> tuple[int, list[str]]:
    """(font size, lines) for one block: the largest size from MAX_SIZE of the height down to
    MIN_SIZE whose words wrap into MAX_LINES lines within `width`; at MIN_SIZE anything that
    still does not fit breaks between characters. An empty text has no lines."""
    words = text.upper().split()
    if not words:
        return 0, []
    for size in range(max(MIN_SIZE, int(height * MAX_SIZE)), MIN_SIZE - 1, -1):
        lines = _wrap(words, size, measure, width, MAX_LINES)
        if lines is not None:
            return size, lines
    return MIN_SIZE, _broken(" ".join(words), MIN_SIZE, measure, width)


def layout(top: str, bottom: str, size: tuple[int, int], measure: Measure
           ) -> list[tuple[str, int, tuple[int, int, int, int]]]:
    """Each drawn line as (text, font size, its box: left, top, right, bottom in px): the top
    block from the top margin down, the bottom block ending at the bottom margin, every line
    centred and within the width's margins."""
    w, h = size
    inner = max(1, int(w * (1 - 2 * MARGIN)))
    margin_y = int(h * MARGIN)
    out = []
    for text, at_top in ((top, True), (bottom, False)):
        font, lines = fit(text, measure, width=inner, height=h)
        if not lines:
            continue
        step = round(font * LEADING)
        block = step * (len(lines) - 1) + font
        y = margin_y if at_top else h - margin_y - block
        for line in lines:
            lw = measure(line, font)
            left = (w - lw) // 2
            out.append((line, font, (left, y, left + lw, y + font)))
            y += step
    return out


def _font(size: int):
    from PIL import ImageFont
    return ImageFont.truetype(str(FONT), size)


def draw(img, top: str, bottom: str):
    """`img` (a Pillow RGB image) with the caption drawn on a copy, and the layout's boxes."""
    from PIL import ImageDraw
    fonts: dict[int, object] = {}

    def font(size: int):
        if size not in fonts:
            fonts[size] = _font(size)
        return fonts[size]

    def measure(text: str, size: int) -> int:
        left, _, right, _ = font(size).getbbox(text, stroke_width=stroke(size))
        return right - left

    out = img.copy()
    pen = ImageDraw.Draw(out)
    boxes = layout(top, bottom, out.size, measure)
    for text, size, (left, y, _, _) in boxes:
        f = font(size)
        offset = f.getbbox(text, stroke_width=stroke(size))[0]
        pen.text((left - offset, y), text, font=f, fill=FILL, stroke_width=stroke(size),
                 stroke_fill=OUTLINE, anchor="lt")
    return out, boxes
