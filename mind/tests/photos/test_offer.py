from leeroy_mind.photos.offer import caption_ok


def test_caption_screen_preserves_existing_photo_rules():
    assert caption_ok('the soup survived somehow')
    for text in ('a mesh outfit', 'my new pajamas', 'a school uniform', 'a teenage look'):
        assert not caption_ok(text)
