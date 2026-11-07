from leeroy_selfie.grid import GROUND, boxes, canvas, layout

import pytest


def test_layout_and_boxes_follow_the_grid():
    assert [layout(n) for n in (1, 2, 3, 5, 9, 10, 16)] == [
        (1, 1), (2, 1), (2, 2), (3, 2), (3, 3), (4, 3), (4, 4)]
    assert canvas(16, 512, 8) == (2088, 2088)
    assert boxes(5, 512, 8)[3] == (8, 528, 520, 1040)


def test_every_count_fits_and_nothing_outside_1_to_16_does():
    assert [layout(n) for n in (4, 6, 7, 8, 11, 12, 13, 15)] == [
        (2, 2), (3, 2), (3, 3), (3, 3), (4, 3), (4, 3), (4, 4), (4, 4)]
    for n in (0, 17, -1):
        with pytest.raises(ValueError):
            layout(n)
    w, h = canvas(7, 256, 0)
    assert all(r <= w and b <= h for _, _, r, b in boxes(7, 256, 0))


def test_the_ground_is_the_dark_blue_of_the_page():
    assert GROUND == (14, 20, 26)
