"""The photo critic's measurements of one rendered image (the worker's `critic` op): the faces
YuNet finds (the `yunet` pin), the hands MediaPipe's palm detector and hand landmarker find (the
`palm` and `handpose` pins, OpenCV Zoo's ONNX exports run by cv2.dnn) with the distinct fingers
of each, how the eyes sit (YuNet's five points and where each iris is in its eye), and how close
her face is to her reference (cosine of the IP-Adapter image encoder's embeds of the two face
crops). Numbers and codes only: no path, prompt or pixel leaves here. A model not fetched, or
OpenCV missing, reports that part "unavailable"; the critic never fails a render.

The hand landmarker always draws 21 points, five fingers, whatever the hand looks like: fingers
here are the fingertips that stand apart from every other tip (merged fingers share a tip), and
a hand is implausible when a finger is far longer than the palm or folds back through it. These
are measurements, not verdicts; the mind decides (selfies/critic.py). cv2 and numpy are imported
inside the functions that need them."""
import math
import time
from pathlib import Path

PALM_FILE = "palm_detection_mediapipe_2023feb.onnx"
HAND_FILE = "handpose_estimation_mediapipe_2023feb.onnx"
FACE_FILE = "face_detection_yunet_2023mar.onnx"
PALM_INPUT = 192
HAND_INPUT = 224
PALM_SCORE = 0.5
PALM_NMS = 0.3
HAND_CONF = 0.8  # the landmarker's hand-presence floor
FACE_SCORE = 0.6
MAX_HANDS = 6  # reported one by one; more are only counted
FACE_MARGIN = 1.8  # the face crop's side over the face box's longer side (face.MARGIN)
TIP_APART = 0.18  # a fingertip closer than this many palm sizes to another is merged with it
EYE_MIN_IOD = 24.0  # pixels between the eyes below which the irises are not read
FINGERS = ((1, 2, 3, 4), (5, 6, 7, 8), (9, 10, 11, 12), (13, 14, 15, 16), (17, 18, 19, 20))


def anchors():
    """The palm detector's 2016 SSD anchors (192 x 192 input: a 24 x 24 grid with 2 anchors a
    cell, then 12 x 12 with 6), centres in fractions of the input, in the model's order."""
    import numpy as np
    out = []
    for grid, per in ((24, 2), (12, 6)):
        for y in range(grid):
            for x in range(grid):
                out += [((x + 0.5) / grid, (y + 0.5) / grid)] * per
    return np.array(out, dtype=np.float32)


def _net(path: Path):
    import cv2
    return cv2.dnn.readNet(str(path))


class Detectors:
    """The fetched detectors of `models_dir` (<data>/models), each None when not fetched or not
    loadable."""

    def __init__(self, models_dir: Path, *, check=None):
        self.face = self.palm = self.hand = None
        self._anchors = None
        models_dir = Path(models_dir)
        fetched = check or (lambda name: True)
        try:
            import cv2  # noqa: F401
        except Exception:
            return
        if fetched("yunet") and (models_dir / "yunet" / FACE_FILE).is_file():
            self.face = str(models_dir / "yunet" / FACE_FILE)
        try:
            if fetched("palm") and fetched("handpose"):
                palm = models_dir / "palm" / PALM_FILE
                hand = models_dir / "handpose" / HAND_FILE
                if palm.is_file() and hand.is_file():
                    self.palm, self.hand = _net(palm), _net(hand)
                    self._anchors = anchors()
        except Exception:
            self.palm = self.hand = None

    # -- faces ------------------------------------------------------------------------------

    def faces(self, bgr) -> list[list[float]]:
        """YuNet's rows (x, y, w, h, 5 points as x, y pairs, score), largest first."""
        import cv2
        h, w = bgr.shape[:2]
        det = cv2.FaceDetectorYN.create(self.face, "", (w, h), FACE_SCORE, 0.3, 50)
        _, found = det.detect(bgr)
        rows = [] if found is None else [[float(v) for v in r[:15]] for r in found]
        return sorted(rows, key=lambda r: r[2] * r[3], reverse=True)

    # -- hands ------------------------------------------------------------------------------

    def palms(self, bgr):
        """(N, 19): box x1 y1 x2 y2, 7 palm points as x, y, score; in image pixels."""
        import cv2
        import numpy as np
        h, w = bgr.shape[:2]
        ratio = min(PALM_INPUT / h, PALM_INPUT / w)
        rh, rw = int(h * ratio), int(w * ratio)
        img = cv2.resize(bgr, (rw, rh))
        left, top = (PALM_INPUT - rw) // 2, (PALM_INPUT - rh) // 2
        img = cv2.copyMakeBorder(img, top, PALM_INPUT - rh - top, left, PALM_INPUT - rw - left,
                                 cv2.BORDER_CONSTANT, None, (0, 0, 0))
        blob = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32)[np.newaxis] / 255.0
        self.palm.setInput(blob)
        outs = self.palm.forward(self.palm.getUnconnectedOutLayersNames())
        reg = next(o for o in outs if o.shape[-1] == 18)[0]
        score = next(o for o in outs if o.shape[-1] == 1)[0, :, 0].astype(np.float64)
        score = 1 / (1 + np.exp(-np.clip(score, -80, 80)))
        scale = float(max(w, h))
        bias = np.array([left, top], dtype=np.float64) / ratio
        cxy = reg[:, :2] / PALM_INPUT + self._anchors
        wh = reg[:, 2:4] / PALM_INPUT
        xy1 = (cxy - wh / 2) * scale - bias
        xy2 = (cxy + wh / 2) * scale - bias
        rects = [[float(a), float(b), float(c - a), float(d - b)]
                 for (a, b), (c, d) in zip(xy1, xy2)]
        keep = cv2.dnn.NMSBoxes(rects, score.tolist(), PALM_SCORE, PALM_NMS)
        keep = np.array(keep).reshape(-1)
        if keep.size == 0:
            return np.empty((0, 19))
        points = (reg[keep, 4:].reshape(-1, 7, 2) / PALM_INPUT + self._anchors[keep][:, None, :]) \
            * scale - bias
        return np.c_[xy1[keep], xy2[keep], points.reshape(-1, 14), score[keep]]

    def landmarks(self, bgr, palm):
        """(conf, 21 x 2 landmarks in image pixels) for one palm row, or None below HAND_CONF.
        The palm's square is cut out, turned so the hand points up, read at 224 x 224, and the
        points turned back (MediaPipe's own steps)."""
        import cv2
        import numpy as np
        box = palm[0:4].reshape(2, 2)
        pts = palm[4:18].reshape(7, 2)
        crop, bias = _square(bgr, box, (0.0, 0.0), 4.0, diagonal=True)
        pts = pts - bias
        box = box - bias
        p0, p2 = pts[0], pts[2]  # the palm's base, the middle finger's base
        rad = math.pi / 2 - math.atan2(-(p2[1] - p0[1]), p2[0] - p0[0])
        rad -= 2 * math.pi * math.floor((rad + math.pi) / (2 * math.pi))
        angle = math.degrees(rad)
        centre = tuple(float(v) for v in box.sum(axis=0) / 2)
        rot = cv2.getRotationMatrix2D(centre, angle, 1.0)
        turned = cv2.warpAffine(crop, rot, (crop.shape[1], crop.shape[0]))
        tp = np.c_[pts, np.ones(len(pts))] @ rot.T
        tbox = np.array([tp.min(axis=0), tp.max(axis=0)])
        hand, hbias = _square(turned, tbox, (0.0, -0.4), 3.0)
        if hand.size == 0:
            return None
        side = hand.shape[0]
        blob = cv2.resize(cv2.cvtColor(hand, cv2.COLOR_BGR2RGB), (HAND_INPUT, HAND_INPUT),
                          interpolation=cv2.INTER_AREA).astype(np.float32)[np.newaxis] / 255.0
        self.hand.setInput(blob)
        outs = self.hand.forward(self.hand.getUnconnectedOutLayersNames())
        screen = outs[0].reshape(-1)[:63].reshape(21, 3)
        conf = float(np.asarray(outs[1]).reshape(-1)[0])
        if not conf >= HAND_CONF:
            return conf, None
        xy = screen[:, :2] * (side / HAND_INPUT) + hbias  # in the turned crop
        inv = cv2.invertAffineTransform(rot)
        back = np.c_[xy, np.ones(len(xy))] @ inv.T + bias
        return conf, back

    def hands(self, bgr) -> list[tuple[float, object]]:
        """(conf, landmarks) of every hand the landmarker accepts, best first."""
        out = []
        for palm in self.palms(bgr):
            got = self.landmarks(bgr, palm)
            if got is not None and got[1] is not None:
                out.append(got)
        return sorted(out, key=lambda h: h[0], reverse=True)


def _square(img, box, shift, enlarge, *, diagonal=False):
    """MediaPipe's crop of a palm box: shifted by `shift` of its size, enlarged `enlarge` times,
    clipped to the image and padded to a square (its diagonal when it will be turned), with the
    (x, y) of the square's corner in `img`."""
    import cv2
    import numpy as np
    wh = box[1] - box[0]
    box = box + np.array(shift) * wh
    centre = box.sum(axis=0) / 2
    half = (box[1] - box[0]) * enlarge / 2
    b = np.array([centre - half, centre + half]).astype(np.int64)
    b[:, 0] = np.clip(b[:, 0], 0, img.shape[1])
    b[:, 1] = np.clip(b[:, 1], 0, img.shape[0])
    crop = img[b[0][1]:b[1][1], b[0][0]:b[1][0]]
    side = int(np.linalg.norm(crop.shape[:2])) if diagonal else max(crop.shape[:2])
    ph, pw = side - crop.shape[0], side - crop.shape[1]
    left, top = pw // 2, ph // 2
    crop = cv2.copyMakeBorder(crop, top, ph - top, left, pw - left, cv2.BORDER_CONSTANT, None,
                              (0, 0, 0))
    return crop, (b[0] - [left, top]).astype(np.float64)


def finger_stats(lm) -> dict:
    """The distinct fingers of one hand's 21 points and whether its shape is plausible: palm
    size from the knuckles; a tip within TIP_APART palm sizes of another counts once; a finger
    longer than 2.6 palm sizes, or with a joint bent back past 160 degrees, is implausible."""
    import numpy as np
    lm = np.asarray(lm, dtype=np.float64)
    palm = max(float(np.linalg.norm(lm[5] - lm[17])), 0.6 * float(np.linalg.norm(lm[0] - lm[9])),
               1e-6)
    tips = [lm[f[-1]] for f in FINGERS]
    distinct = 0
    for i, t in enumerate(tips):
        if all(float(np.linalg.norm(t - u)) >= TIP_APART * palm for j, u in enumerate(tips) if j < i):
            distinct += 1
    plausible = True
    for f in FINGERS:
        chain = [lm[0]] + [lm[i] for i in f]
        length = sum(float(np.linalg.norm(b - a)) for a, b in zip(chain[1:], chain[2:]))
        if length > 2.6 * palm:
            plausible = False
        for a, b, c in zip(chain, chain[1:], chain[2:]):
            u, v = a - b, c - b
            nu, nv = float(np.linalg.norm(u)), float(np.linalg.norm(v))
            if nu > 0 and nv > 0:
                cos = float(np.dot(u, v)) / (nu * nv)
                if math.degrees(math.acos(max(-1.0, min(1.0, cos)))) < 20:  # folded back
                    plausible = False
    spread = float(np.mean([np.linalg.norm(t - lm[0]) for t in tips])) / palm
    return {"fingers": distinct, "plausible": plausible, "spread": round(spread, 3)}


def _iris(gray, x: float, y: float, hw: float, hh: float):
    """Where the darkest part of the eye patch around (x, y) is, as offsets in half-widths and
    half-heights of the patch; None when the patch leaves the image."""
    import numpy as np
    x0, x1, y0, y1 = int(x - hw), int(x + hw), int(y - hh), int(y + hh)
    if x0 < 0 or y0 < 0 or x1 > gray.shape[1] or y1 > gray.shape[0] or x1 - x0 < 6 or y1 - y0 < 4:
        return None
    patch = gray[y0:y1, x0:x1].astype(np.float64)
    cut = np.percentile(patch, 12)
    weight = np.clip(cut - patch + 1, 0, None)
    if weight.sum() <= 0:
        return None
    ys, xs = np.mgrid[y0:y1, x0:x1]
    cx = float((weight * xs).sum() / weight.sum())
    cy = float((weight * ys).sum() / weight.sum())
    return (cx - x) / hw, (cy - y) / hh


def eye_stats(bgr, face) -> dict:
    """The eyes of one YuNet row: `tilt`, the eye line's angle against the mouth line's
    (degrees); `level`, one eye above the other across the face's own axis, in eye distances;
    `gaze_gap`, how far apart the two irises sit in their eyes (0: both look the same way).
    gaze_gap is None for a face too small to read the irises."""
    import cv2
    re, le, nose, rm, lm = ((face[4 + 2 * i], face[5 + 2 * i]) for i in range(5))
    ex, ey = le[0] - re[0], le[1] - re[1]
    mx, my = lm[0] - rm[0], lm[1] - rm[1]
    iod = math.hypot(ex, ey)
    tilt = math.degrees(math.atan2(ey, ex) - math.atan2(my, mx))
    tilt = (tilt + 180) % 360 - 180
    mouth = math.atan2(my, mx)
    level = 0.0 if iod <= 0 else abs(-math.sin(mouth) * ex + math.cos(mouth) * ey) / iod
    gap = None
    if iod >= EYE_MIN_IOD:
        gray = cv2.GaussianBlur(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY), (3, 3), 0)
        a = _iris(gray, re[0], re[1], 0.26 * iod, 0.14 * iod)
        b = _iris(gray, le[0], le[1], 0.26 * iod, 0.14 * iod)
        if a is not None and b is not None:
            gap = math.hypot(a[0] - b[0], 0.5 * (a[1] - b[1]))
    return {"tilt": round(abs(tilt), 2), "level": round(level, 3),
            "gaze_gap": None if gap is None else round(gap, 3), "iod": round(iod, 1)}


def face_box(face, width: int, height: int) -> tuple[int, int, int, int]:
    """The square of FACE_MARGIN around one YuNet row, inside the image."""
    from .face import square
    return square(face, width, height, FACE_MARGIN)


def cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return max(-1.0, min(1.0, dot / (na * nb))) if na > 0 and nb > 0 else 0.0


def load_detectors(data) -> Detectors:
    """The detectors fetched into the data dir `data` (None: none), each checked quickly as the
    worker's start checks its models (records and revisions, no hash)."""
    from . import models
    if data is None:
        return Detectors(Path("."), check=lambda name: False)

    def fetched(name: str) -> bool:
        try:
            return all(ok is True for ok, _ in models.check(data, name, quick=True))
        except Exception:
            return False
    return Detectors(Path(data) / "models", check=fetched)


def reference_crop(detectors: Detectors, ref: str, crop):
    """Her reference's face crop: the mind's square `crop` of the JPEG at `ref`, cut to
    FACE_MARGIN around the largest face whose centre lies in it (else the square as it is).
    RenderFailed("missing_ref") when the file is gone or not a JPEG."""
    import numpy as np
    from . import jpeg
    from .face import pick
    from .protocol import RenderFailed
    from .render import ref_box
    try:
        img = jpeg.open_rgb(ref)
    except Exception as e:  # Pillow's errors share no narrower base
        raise RenderFailed("missing_ref") from e
    box = ref_box(*img.size, crop)
    if detectors.face is not None:
        try:
            face = pick(detectors.faces(np.ascontiguousarray(np.asarray(img)[:, :, ::-1])), box)
            if face is not None:
                box = face_box(face, *img.size)
        except Exception:
            pass
    return img.crop(box)


def answer(detectors: Detectors, req, encode) -> dict:
    """The critic op's fields for one request (protocol.Critic)."""
    ref = None if req.ref is None else reference_crop(detectors, req.ref, req.crop)
    return measure(detectors, req.path, ref_crop=ref, encode=encode)


def measure(detectors: Detectors, path: str, *, ref_crop=None, encode=None) -> dict:
    """The measurements of the JPEG at `path`: faces, hands, eyes and, with `ref_crop` (the
    reference's face crop, a PIL image) and `encode` (PIL image -> vector, or None), the
    identity cosine. Raises RenderFailed("missing_image") when the file is gone or not a JPEG."""
    import numpy as np
    from . import jpeg
    from .protocol import RenderFailed
    started = time.perf_counter()
    try:
        img = jpeg.open_rgb(path)
    except Exception as e:  # Pillow's errors share no narrower base
        raise RenderFailed("missing_image") from e
    bgr = np.ascontiguousarray(np.asarray(img)[:, :, ::-1])
    out: dict = {"faces": None, "face_score": None, "hands": None, "hand": [], "eyes": None,
                 "identity": None, "identity_code": "no_ref" if ref_crop is None else "",
                 "detectors": {"faces": "unavailable", "hands": "unavailable"}}
    faces = []
    if detectors.face is not None:
        try:
            faces = detectors.faces(bgr)
            out["faces"] = len(faces)
            out["face_score"] = round(faces[0][14], 4) if faces else None
            out["detectors"]["faces"] = "ok"
            if faces:
                out["eyes"] = eye_stats(bgr, faces[0])
        except Exception:
            out["detectors"]["faces"] = "failed"
    if detectors.palm is not None and detectors.hand is not None:
        try:
            hands = detectors.hands(bgr)
            out["hands"] = len(hands)
            out["hand"] = [{"conf": round(c, 4), **finger_stats(lm)} for c, lm in hands[:MAX_HANDS]]
            out["detectors"]["hands"] = "ok"
        except Exception:
            out["detectors"]["hands"] = "failed"
    if ref_crop is not None:
        if encode is None:
            out["identity_code"] = "encoder_unavailable"
        elif not faces:
            out["identity_code"] = "no_face"
        else:
            try:
                mine = encode(img.crop(face_box(faces[0], *img.size)))
                theirs = encode(ref_crop)
                out["identity"] = round(cosine(mine, theirs), 4)
                out["identity_code"] = "ok"
            except Exception:
                out["identity_code"] = "encoder_failed"
    out["seconds"] = round(time.perf_counter() - started, 3)
    return out


def encoder(image_encoder, processor, torch):
    """PIL image -> the image encoder's embeds as a list of floats, gradients off; None when the
    worker holds no image encoder."""
    if image_encoder is None or processor is None:
        return None

    def encode(image):
        with torch.no_grad():
            pixels = processor(images=image, return_tensors="pt").pixel_values
            pixels = pixels.to(device=image_encoder.device, dtype=image_encoder.dtype)
            return image_encoder(pixels).image_embeds[0].float().cpu().tolist()
    return encode
