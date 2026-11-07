"""The JPEG files the renderer and the grid read and write. Pillow is imported inside each
function, so importing this module loads nothing heavy."""
import os


def open_rgb(path: str):
    """The image at `path` as RGB, read in full and the file closed; ValueError when it opens as
    anything but a JPEG, and Pillow's own error when it does not open."""
    from PIL import Image
    with Image.open(path) as src:
        if src.format != "JPEG":
            raise ValueError("not a JPEG")
        return src.convert("RGB")


def write(img, path: str, quality: int) -> None:
    """Saved beside `path` as .tmp, then renamed over it: a reader sees the old file or the new."""
    tmp = path + ".tmp"
    img.save(tmp, format="JPEG", quality=quality)
    os.replace(tmp, path)


def thumb_size(width: int, height: int, long_side: int) -> tuple[int, int]:
    """(width, height) scaled so the longer side is `long_side`, the aspect kept."""
    scale = long_side / max(width, height)
    return max(1, round(width * scale)), max(1, round(height * scale))
