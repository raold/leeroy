"""The reference face's geometry and colour (face.py), without OpenCV or a model."""
import pytest

from leeroy_selfie import face


def test_the_square_is_centred_on_the_face_and_sized_by_its_longer_side():
    # Aima's reference: the face at x 343, y 76, 149 x 199 in a 1024 image
    assert face.square((343, 76, 149, 199), 1024, 1024) == (238, 0, 597, 358)
    assert face.square((400, 400, 100, 100), 1024, 1024, margin=2.0) == (350, 350, 550, 550)


def test_the_square_stays_inside_the_image_and_never_outgrows_it():
    assert face.square((0, 0, 100, 100), 1024, 1024, margin=2.0) == (0, 0, 200, 200)
    assert face.square((950, 950, 70, 70), 1000, 1000, margin=2.0) == (860, 860, 1000, 1000)
    assert face.square((100, 100, 900, 900), 800, 600) == (200, 0, 800, 600)


def test_pick_prefers_the_largest_face_inside_the_minds_square():
    small_in, big_out = (100, 100, 50, 50, 0.9), (600, 600, 200, 200, 0.95)
    assert face.pick([small_in, big_out], (0, 0, 400, 400)) == small_in
    assert face.pick([small_in, big_out], (500, 500, 900, 900)) == big_out
    assert face.pick([small_in, big_out], (950, 0, 1000, 50)) == big_out  # none inside: largest
    assert face.pick([], (0, 0, 10, 10)) is None
    assert face.pick(None, (0, 0, 10, 10)) is None


def test_gray_world_evens_out_a_colour_cast():
    np = pytest.importorskip("numpy")
    purple = np.zeros((4, 4, 3), np.uint8) + np.array([120, 60, 180], np.uint8)
    means = face.gray_world(purple).reshape(-1, 3).mean(axis=0)
    assert np.allclose(means, means.mean(), atol=1.0)
    black = np.zeros((2, 2, 3), np.uint8)
    assert face.gray_world(black) is black


def test_a_face_ref_without_a_face_cuts_the_minds_square(monkeypatch):
    pytest.importorskip("cv2")
    from PIL import Image
    img = Image.new("RGB", (100, 100), (120, 60, 180))
    ref = face.FaceRef.__new__(face.FaceRef)
    monkeypatch.setattr(face.FaceRef, "faces", lambda self, i: [])
    crop, found = ref(img, (10, 10, 60, 60))
    assert found is False and crop.size == (50, 50)
    r, g, b = crop.getpixel((5, 5))
    assert abs(r - g) <= 1 and abs(g - b) <= 1  # the cast evened out


def test_a_face_ref_whose_detector_fails_keeps_the_minds_square(monkeypatch):
    pytest.importorskip("cv2")
    from PIL import Image

    def boom(self, i):
        raise RuntimeError("detector")
    ref = face.FaceRef.__new__(face.FaceRef)
    monkeypatch.setattr(face.FaceRef, "faces", boom)
    crop, found = ref(Image.new("RGB", (100, 100), (90, 90, 90)), (0, 0, 40, 40))
    assert found is False and crop.size == (40, 40)
