"""The detailer: an optional second pass over a rendered image (a render request's detail_faces
and detail_hands). SDXL draws a small face or hand with too few pixels (crossed eyes, six
fingers), so each face YuNet finds and each palm the palm detector finds is cut out as a padded
square, enlarged to the pass's size, drawn again by img2img at a low strength, shrunk and pasted
back through a feathered mask. Hands go first and faces last, so a hand at her face never leaves
the face as a hand pass drew it.

This module is the geometry and the order; the diffusion call is the `refine` the renderer
passes in (render.Renderer.refiner). The pass never fails a render: without a detector, on CUDA
out of memory or any other error it keeps what it has and says why in a code. Numbers and codes
only leave here. cv2, numpy and Pillow are imported inside the functions that need them."""
import time
from dataclasses import dataclass

from .face import square

FACE_PAD = 2.0  # the crop's side over the face box's longer side: the face, hair, some neck
HAND_PAD = 3.0  # over the palm box's longer side: the fingers, whichever way they point
MIN_FACE_PX = 32  # a face box smaller than this is a background face or a false find
MIN_HAND_PX = 24
MIN_HAND_SCORE = 0.6  # the palm detector's own floor is 0.5: a doubtful palm is left alone
MAX_FACE_FRAC = 0.35  # of the frame's shorter side: a headshot's face has its pixels already
MAX_HAND_FRAC = 0.25
MAX_FACES = 2
MAX_HANDS = 2
FEATHER = 0.125  # of the crop's side: the mask's ramp from the old image to the new
FACE_DENOISE = 0.35
HAND_DENOISE = 0.45
DENOISE_RANGE = (0.1, 0.7)
SIZE = 1024
SIZES = (768, 896, 1024)
FACE_LEAD = "detailed face, natural eyes"
HAND_LEAD = "a natural hand, five fingers"
_SEED_STEP = 0x9E3779B1


@dataclass(frozen=True)
class Detail:
    """What a render request asks of the pass (protocol.parse)."""
    faces: bool = True
    hands: bool = False
    face_denoise: float = FACE_DENOISE
    hand_denoise: float = HAND_DENOISE
    size: int = SIZE


class Skip(Exception):
    """A refine that could not run; its message is the pass's code (oom)."""


def seed_for(seed: int, kind: str, index: int) -> int:
    """The crop's own seed, from the image's: a different one for every face and hand, the same
    for the same image."""
    n = 1 + 2 * index + (1 if kind == "hand" else 0)
    return (seed + _SEED_STEP * n) & 0xFFFFFFFF


def select(boxes, width: int, height: int, *, pad: float, min_px: int, max_frac: float,
           most: int, size: int = SIZE) -> tuple[list[tuple[int, int, int, int]], int]:
    """(the square crops (l, t, r, b) to draw again, how many boxes were passed over). `boxes`
    are (x, y, w, h, ...) in the order they should be taken. A box whose longer side is under
    min_px, or over max_frac of the frame's shorter side, is passed over, as is one whose crop
    would not be enlarged to `size`, and every box after the first `most` kept."""
    crops, skipped = [], 0
    short = min(width, height)
    for box in boxes:
        longer = max(float(box[2]), float(box[3]))
        if longer < min_px or longer > max_frac * short or len(crops) >= most:
            skipped += 1
            continue
        crop = square(box, width, height, pad)
        if crop[2] - crop[0] >= size or crop[2] - crop[0] < 8 or crop[3] - crop[1] < 8:
            skipped += 1
            continue
        crops.append(crop)
    return crops, skipped


def feather_mask(width: int, height: int, feather: int, open_edges=(False, False, False, False)):
    """The paste's weights, float32 height x width in 0-1: 1 in the middle, falling to 0 over
    `feather` pixels towards each edge. An edge in `open_edges` (left, top, right, bottom: the
    crop's edge is the image's own) has no ramp, since no old image lies beyond it."""
    import numpy as np
    feather = max(1, int(feather))

    def ramp(n: int, low: bool, high: bool):
        i = np.arange(n, dtype=np.float32)
        a = np.ones(n, dtype=np.float32) if low else np.clip((i + 0.5) / feather, 0, 1)
        b = np.ones(n, dtype=np.float32) if high else np.clip((n - i - 0.5) / feather, 0, 1)
        return np.minimum(a, b)
    left, top, right, bottom = open_edges
    return np.minimum.outer(ramp(height, top, bottom), ramp(width, left, right))


def open_edges(box, width: int, height: int) -> tuple[bool, bool, bool, bool]:
    """Which of the crop's edges (left, top, right, bottom) are the image's own."""
    left, top, right, bottom = box
    return left <= 0, top <= 0, right >= width, bottom >= height


def paste(base, patch, box, mask):
    """`base` (H x W x 3 uint8) with `patch` (the crop's size) blended into `box` by `mask`;
    a new array, `base` is not written."""
    import numpy as np
    left, top, right, bottom = box
    out = base.copy()
    old = out[top:bottom, left:right].astype(np.float32)
    m = mask[:, :, None]
    mixed = old * (1.0 - m) + patch.astype(np.float32) * m
    out[top:bottom, left:right] = np.clip(np.rint(mixed), 0, 255).astype(np.uint8)
    return out


def palm_boxes(palms) -> list[tuple[float, float, float, float, float]]:
    """The palm detector's rows (x1, y1, x2, y2, 7 points, score) as (x, y, w, h, score), the
    surest first."""
    rows = [(float(p[0]), float(p[1]), float(p[2] - p[0]), float(p[3] - p[1]), float(p[18]))
            for p in palms]
    return sorted(rows, key=lambda r: r[4], reverse=True)


def plan(image, detectors, opts: Detail) -> tuple[list, list, int, str]:
    """(the face crops, the hand crops, boxes passed over, a code) of a PIL image: the code
    names a detector the request needs and the worker lacks or that failed (faces_unavailable,
    hands_unavailable, detectors_unavailable), else ""."""
    import numpy as np
    width, height = image.size
    bgr = np.ascontiguousarray(np.asarray(image)[:, :, ::-1])
    faces, hands, skipped, missing = [], [], 0, []
    if opts.faces:
        try:
            if detectors is None or detectors.face is None:
                raise LookupError("face")
            faces, n = select(detectors.faces(bgr), width, height, pad=FACE_PAD,
                              min_px=MIN_FACE_PX, max_frac=MAX_FACE_FRAC, most=MAX_FACES,
                              size=opts.size)
            skipped += n
        except Exception:
            missing.append("faces")
    if opts.hands:
        try:
            if detectors is None or detectors.palm is None:
                raise LookupError("palm")
            palms = palm_boxes(detectors.palms(bgr))
            sure = [p for p in palms if p[4] >= MIN_HAND_SCORE]
            hands, n = select(sure, width, height, pad=HAND_PAD, min_px=MIN_HAND_PX,
                              max_frac=MAX_HAND_FRAC, most=MAX_HANDS, size=opts.size)
            skipped += n + len(palms) - len(sure)
        except Exception:
            missing.append("hands")
    code = ("" if not missing else "detectors_unavailable" if len(missing) == 2
            else f"{missing[0]}_unavailable")
    return faces, hands, skipped, code


def run(image, detectors, opts: Detail, refine) -> tuple[object, dict]:
    """(the image after the pass, its fields: detail_faces and detail_hands drawn again,
    detail_skipped, detail_ms, detail_code). `refine(kind, index, crop)` gets a size x size PIL
    image ("hand" or "face", its index among its kind) and returns one of that size; it raises
    Skip(code) to end the pass (oom). Any other error ends it with the code `error` (and
    detail_error, the exception's class name, for the log). What was pasted before stays."""
    import numpy as np
    from PIL import Image
    started = time.perf_counter()
    done = {"face": 0, "hand": 0}
    skipped, code, error, pixels = 0, "", "", None
    try:
        faces, hands, skipped, code = plan(image, detectors, opts)
        width, height = image.size
        for kind, crops in (("hand", hands), ("face", faces)):
            for index, box in enumerate(crops):
                if pixels is None:
                    pixels = np.asarray(image.convert("RGB"))
                side_w, side_h = box[2] - box[0], box[3] - box[1]
                crop = Image.fromarray(pixels[box[1]:box[3], box[0]:box[2]]).resize(
                    (opts.size, opts.size), Image.LANCZOS)
                new = refine(kind, index, crop).convert("RGB").resize((side_w, side_h),
                                                                      Image.LANCZOS)
                mask = feather_mask(side_w, side_h, round(FEATHER * min(side_w, side_h)),
                                    open_edges(box, width, height))
                pixels = paste(pixels, np.asarray(new), box, mask)
                done[kind] += 1
    except Skip as e:
        code = str(e) or "error"
    except Exception as e:
        code, error = "error", type(e).__name__
    if done["face"] or done["hand"]:
        image = Image.fromarray(pixels)
    out = {"detail_faces": done["face"], "detail_hands": done["hand"], "detail_skipped": skipped,
           "detail_ms": max(0, round((time.perf_counter() - started) * 1000)),
           "detail_code": code}
    if error:
        out["detail_error"] = error
    return image, out
