"""The guild photo's grid: a pure layout of 1-16 square tiles on a dark ground, row-major, with
a gutter around and between them. compose() (Task 2) draws it."""

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


def compose(req) -> dict:
    raise NotImplementedError("the grid compositor")
