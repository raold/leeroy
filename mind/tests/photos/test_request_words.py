import pytest

from leeroy_mind.photos.request_words import asks_for_selfie, specific_ask
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
    # A pet name or her own name before the ask (the owner's DMs, 2026-11-14).
    'babe can you send me a pic?', 'baby send me a selfie', 'Arkon send me a pic',
    'hey gorgeous, show me a pic of you', 'lover can i get a selfie?',
    "sneak away when you're free and send me a selfie", 'go change then send me a pic',
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
    'babe I took a selfie', 'baby got it, nice pic', 'Arkon let me take a pic',
    "i'll take a pic and send it to you", 'i want to take a pic and send you one',
])
def test_non_requests_and_previous_photo_questions(text):
    assert not asks_for_selfie(text, names=NAMES)


# The owner's DMs, 2026-11-17..25, that the recognizer missed (paraphrased): each got a words-only
# answer in which the live model, told of no photo, said she couldn't send one right now.
@pytest.mark.parametrize('text', [
    'DM me a pic', 'dm me a selfie, a cute one', 'text me a photo', 'message me a selfie',
    'DM me a really cute little selfie', 'gimme a pic', 'give me a selfie',
    'i want you to send me the cutest pic you can', 'i need a selfie of you',
    "i'd love a pic", 'i wanna see a selfie', 'can you retry sending me a selfie?',
    'keep sending me selfies of your outfit', 'would you mind sending a pic?',
    'go ahead and send me a pic', 'hurry up and send me a selfie',
    'send me a selfie of your new haircut', 'snap a pic of your nails',
    'im ready baby, send me a selfie of your look today',
    'show me everything baby send me a cute selfie', 'NSFW - send me a selfie',
    'what are you wearing?', 'what r u wearing', "show me what you're wearing",
    'Arkon: send me a pic', 'can i have a pic?', 'shoot me a selfie',
])
def test_his_other_ways_of_asking(text):
    assert asks_for_selfie(text, names=NAMES)


@pytest.mark.parametrize('text', [
    # His offers of his own photo, and "stop".
    'want to see a pic?', 'want me to send you a pic?', 'can i send you a pic?',
    'i want to send you a pic', 'stop sending me pics', 'never dm me a selfie again',
    # Other subjects of hers, and her old photos.
    'dm me a pic of your cat', 'send me a pic of your lunch', 'text me a photo of your setup',
    'i love your selfies', 'your last selfie was great', 'you never sent me that pic',
    'i know what you are wearing', 'dm me later', 'i need it',
])
def test_his_offers_other_subjects_and_old_photos_are_not_asks(text):
    assert not asks_for_selfie(text, names=NAMES)


@pytest.mark.parametrize('text,specific', [
    ('send me a pic', False), ('selfie?', False), ('DM me a selfie', False),
    ('what are you wearing?', False), ('pls send pics', False), ('send me a selfie of you', False),
    ('send me a sexy selfie', True), ('send me a selfie from the beach', True),
    ('send a pic of you in that dress', True), ('bathroom mirror selfie?', True),
    ('send me a selfie of your outfit', True), ('pic of you at work?', True),
])
def test_specific_asks_describe_the_photo(text, specific):
    assert specific_ask(text) is specific


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
