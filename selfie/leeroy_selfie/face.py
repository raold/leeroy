"""The reference face of an identity render. The mind sends a coarse square of the reference cut
by the photo's framing; YuNet (OpenCV Zoo's face detector, MIT, the `yunet` pin) finds the face
in the reference and the square is cut tight around it, then the crop's colour cast is evened out
(gray world, the gains read from the whole reference and held to GAIN_RANGE), so the adapter takes
her face and complexion and not the room's light (a nightclub's purple tinted every scene before).
Without the model, without OpenCV, or with no face found, the mind's
square is used as it is. Every failure here falls back to that square: a render never fails for
the detector. cv2 and numpy are imported inside FaceRef and gray_world."""
from pathlib import Path

MODEL_FILE = "face_detection_yunet_2023mar.onnx"
MARGIN = 1.8  # the square's side over the face box's longer side: the face, its hair, no room
MIN_SCORE = 0.6  # YuNet's confidence floor
GAIN_RANGE = (0.85, 1.18)  # each channel's correction at most: a skin-filled crop stays skin


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


def gray_gains(pixels):
    """The per-channel gains that make the H x W x 3 array's channel means equal, each held to
    GAIN_RANGE; None for an array with a channel at mean 0."""
    import numpy as np
    means = pixels.astype(np.float32).reshape(-1, 3).mean(axis=0)
    if (means <= 0).any():
        return None
    return np.clip(means.mean() / means, *GAIN_RANGE)


def gray_world(pixels, gains=None):
    """The H x W x 3 uint8 array times `gains` (by default its own gray_gains): the colour cast
    evened out; returned as it is when there are no gains."""
    import numpy as np
    gains = gray_gains(pixels) if gains is None else gains
    if gains is None:
        return pixels
    return np.clip(pixels.astype(np.float32) * gains, 0, 255).astype(np.uint8)


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
        try:  # the cast read from the whole reference, not from the skin the crop is full of
            gains = gray_gains(np.asarray(img))
            if gains is not None:
                crop = Image.fromarray(gray_world(np.asarray(crop), gains))
        except Exception:
            pass
        return crop, face is not None
