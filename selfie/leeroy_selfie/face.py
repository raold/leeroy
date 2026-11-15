"""The reference face of an identity render. The mind sends a coarse square of the reference cut
by the photo's framing; YuNet (OpenCV Zoo's face detector, MIT, the `yunet` pin) finds the face
in the reference and the square is cut tight around it, then the crop's colour cast is evened out
(gray world), so the adapter takes her face and not the room's light (a nightclub's purple
tinted every scene before). Without the model, without OpenCV, or with no face found, the mind's
square is used as it is. Every failure here falls back to that square: a render never fails for
the detector. cv2 and numpy are imported inside FaceRef and gray_world."""
from pathlib import Path

MODEL_FILE = "face_detection_yunet_2023mar.onnx"
MARGIN = 1.8  # the square's side over the face box's longer side: the face, its hair, no room
MIN_SCORE = 0.6  # YuNet's confidence floor


def square(face, width: int, height: int, margin: float = MARGIN) -> tuple[int, int, int, int]:
    """The (l, t, r, b) pixel square centred on `face` (x, y, w, h, ...), its side `margin`
    times the face's longer side (at most the image's shorter side), shifted to stay inside."""
    x, y, w, h = (float(v) for v in face[:4])
    side = min(float(width), float(height), margin * max(w, h))
    cx, cy = x + w / 2, y + h / 2
    left = min(max(0.0, cx - side / 2), width - side)
    top = min(max(0.0, cy - side / 2), height - side)
    return round(left), round(top), round(left + side), round(top + side)


def pick(faces, box):
    """The largest face whose centre lies in `box` (the mind's square, pixels), else the
    largest face; None for none."""
    faces = list(faces or ())
    if not faces:
        return None
    left, top, right, bottom = box

    def inside(f) -> bool:
        cx, cy = f[0] + f[2] / 2, f[1] + f[3] / 2
        return left <= cx <= right and top <= cy <= bottom
    pool = [f for f in faces if inside(f)] or faces
    return max(pool, key=lambda f: f[2] * f[3])


def gray_world(pixels):
    """The H x W x 3 uint8 array with its channel means made equal (the colour cast removed);
    an array with a channel at mean 0 is returned as it is."""
    import numpy as np
    a = pixels.astype(np.float32)
    means = a.reshape(-1, 3).mean(axis=0)
    if (means <= 0).any():
        return pixels
    return np.clip(a * (means.mean() / means), 0, 255).astype(np.uint8)


class FaceRef:
    """Called with the reference (a PIL RGB image) and the mind's square in pixels: (the crop to
    encode, whether a face was found)."""

    def __init__(self, model: Path):
        import cv2  # noqa: F401  (OpenCV missing: the loader keeps the plain crop)
        self.model = str(model)

    def faces(self, img) -> list:
        import cv2
        import numpy as np
        bgr = np.ascontiguousarray(np.asarray(img)[:, :, ::-1])
        h, w = bgr.shape[:2]
        det = cv2.FaceDetectorYN.create(self.model, "", (w, h), MIN_SCORE, 0.3, 50)
        _, found = det.detect(bgr)
        return [] if found is None else [tuple(float(v) for v in row[:5]) for row in found]

    def __call__(self, img, box):
        from PIL import Image
        import numpy as np
        try:
            face = pick(self.faces(img), box)
        except Exception:  # the detector's failure keeps the mind's square
            face = None
        if face is not None:
            box = square(face, *img.size)
        crop = img.crop(box)
        try:
            crop = Image.fromarray(gray_world(np.asarray(crop)))
        except Exception:
            pass
        return crop, face is not None
