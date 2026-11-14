import pytest

from leeroy_mind.photos.request_words import asks_for_selfie
from leeroy_mind.photos.config import load_photos_config

NAMES = ('Arkon',)


@pytest.mark.parametrize('text', [
    'send me a selfie', 'show me a pic of you', 'post a picture of yourself',
    'take a photo for me', 'drop some pics', 'share a snap', 'snap a selfie',
    'got a photo?', 'can I see a selfie?', 'can I get a pic?', 'let me see a picture',
    'selfie?', 'pic?', '@Arkon selfie?', '@Belra, can you send me a photo at the cafe?',
    'please selfie? please', 'could you show us a photo of yourself?',
    'can you send me a selfie?', 'could u take a pic for me?', 'would you send a photo of you in that dress?',
    'pls send pics', 'ok, send me a pic of yourself', 'show me a picture of your face',
    'got a pic for me?', 'you got any pics?', 'send me a selfie from the beach', 'lol send a selfie',
    'send me a pic of Arkon', 'send me a pic of @Arkon', 'will you snap a selfie for me later?',
    "Send me a selfie. I'll send one back", 'hey, can you send a pic of ur face?',
    'Can I get a selfie of you at the inn?',
])
def test_explicit_selfie_requests(text):
    assert asks_for_selfie(text, names=NAMES)


@pytest.mark.parametrize('text', [
    'nice pic', 'that photo', 'I took a selfie', 'send me the loot list',
    'can I see that photo again?', 'show me the previous picture', 'where did you take that photo?',
    'can I see the photo you sent?', 'look at my photo', 'show me a pic of your cat',
    'send a photo of me', 'show a picture of the raid', 'do not send a pic',
    "don't post a selfie", 'what do you think of my selfie?', 'I shared a picture',
    'the last selfie was great', 'send it to me', 'a photograph of the boss', '', None,
    # Another subject: only her, by "you", "yourself", her face or her own name, is allowed.
    'send me a photo of a cat', 'show me a picture of Stormwind', 'send me a pic of Arthas',
    'take a pic of your setup', 'drop a pic of your build', 'send me a pic of @Belra',
    'take a pic of the sunset for me', 'share the pics from the raid',
    # His acknowledgements of a photo she already sent.
    'got it, nice pic', 'omg got it! love the pic', 'I got your pic, thanks', 'haha got your selfie',
    'I got a pic for you',
    # His own plans and questions about his own photo.
    "I'll send you a pic later", "I'll post a selfie", 'let me take a pic', 'can I post a pic here?',
    "i can't take a selfie, my phone died", 'should I take a selfie?', 'send you a pic later',
    'do you want me to send a pic?',
    # The ask and the noun are in different clauses.
    'send me the loot list, nice pic btw',
])
def test_non_requests_and_previous_photo_questions(text):
    assert not asks_for_selfie(text, names=NAMES)


def test_her_name_counts_only_when_it_is_hers():
    assert asks_for_selfie('send me a pic of Arkon', names=('Arkon',))
    assert not asks_for_selfie('send me a pic of Arkon', names=('Belra',))
    assert not asks_for_selfie('send me a pic of Arkon')


def test_requests_default_off_and_invalid_table_disables(tmp_path):
    path = tmp_path / 'mind.toml'
    assert not load_photos_config(path).requests
    path.write_text('[photos]\nsend=true\nrequests=true\n')
    assert load_photos_config(path).requests
    path.write_text('[photos]\nsend=true\nrequests="yes"\n')
    assert not load_photos_config(path).requests
