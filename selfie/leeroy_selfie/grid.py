"""The guild photo's grid: a pure layout of 1-16 square tiles on a dark ground, row-major, with
a gutter around and between them, and compose(), which draws it with Pillow (imported inside)."""
import time

from . import jpeg
from .protocol import Grid, RenderFailed

GROUND = (14, 20, 26)  # #0e141a

_LAYOUTS = ((1, 1, 1), (2, 2, 1), (4, 2, 2), (6, 3, 2), (9, 3, 3), (12, 4, 3), (16, 4, 4))


def layout(n: int) -> tuple[int, int]:
    """(cols, rows) for n tiles."""
    if type(n) is not int or not 1 <= n <= 16:
        raise ValueError("a grid holds 1-16 tiles")
    for most, cols, rows in _LAYOUTS:
        if n <= most:
            return cols, rows
    raise AssertionError("unreachable")


def canvas(n: int, tile: int, gutter: int) -> tuple[int, int]:
    cols, rows = layout(n)
    return gutter + cols * (tile + gutter), gutter + rows * (tile + gutter)


def boxes(n: int, tile: int, gutter: int) -> list[tuple[int, int, int, int]]:
    """Each tile's (left, top, right, bottom), row-major."""
    cols, _ = layout(n)
    out = []
    for i in range(n):
        c, r = i % cols, i // cols
        x0, y0 = gutter + c * (tile + gutter), gutter + r * (tile + gutter)
        out.append((x0, y0, x0 + tile, y0 + tile))
    return out


def compose(req: Grid) -> dict:
    """The grid's width, height and seconds, once `out` (quality 92) and `thumb` (its long side
    thumb_size, quality 85) are written, each through .tmp. Every tile is opened as a JPEG
    first: one that is missing or is not one raises RenderFailed("missing_tile") before
    anything is written. Each is resized LANCZOS to `tile` and pasted at its box on GROUND."""
    from PIL import Image
    started = time.perf_counter()
    tiles = []
    for path in req.tiles:
        try:
            img = jpeg.open_rgb(path)
        except Exception as e:  # Pillow's errors share no narrower base: a tile is there or not
            raise RenderFailed("missing_tile") from e
        tiles.append(img.resize((req.tile, req.tile), Image.LANCZOS))
    width, height = canvas(len(tiles), req.tile, req.gutter)
    out = Image.new("RGB", (width, height), GROUND)
    for img, box in zip(tiles, boxes(len(tiles), req.tile, req.gutter)):
        out.paste(img, box)
    jpeg.write(out, req.out, 92)
    jpeg.write(out.resize(jpeg.thumb_size(width, height, req.thumb_size), Image.LANCZOS),
               req.thumb, 85)
    return {"width": width, "height": height,
            "seconds": round(time.perf_counter() - started, 2)}
