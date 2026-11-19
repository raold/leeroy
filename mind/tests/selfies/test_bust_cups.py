"""Owner-selected C-H visual presets: storage, undo, queued photos and prompt fit."""
from dataclasses import replace

import pytest

from conftest import run
from leeroy_mind.endpoint import looks_args
from leeroy_mind.personas.io import load_persona
from leeroy_mind.personas.looks_edit import LooksService
from leeroy_mind.personas.schema import persona_from_dict, persona_to_dict, validate_persona
from leeroy_mind.selfies import words_scene as W
from leeroy_mind.selfies.prompt import BUST_WORDS, build_prompt, build_words, over_fit
from leeroy_mind.selfies.scene import build_scene_prompt
from leeroy_mind.selfies.snaps import looks_hash
from leeroy_mind.selfies.tokens import estimate
from selfies import clip_bpe
from selfies.test_looks_edit import app_for, looks, save_woman
from selfies.test_romance_styling import original
from selfies.test_weight_bust import _worker_fit, woman

CUPS = ('c_cup', 'd_cup', 'e_cup', 'f_cup', 'g_cup', 'h_cup')


@pytest.mark.parametrize('cup', CUPS)
def test_each_cup_round_trips_and_changes_only_the_selected_proportion(cup):
    before = woman(build='curvy')
    after = replace(before, appearance=replace(before.appearance, bust=cup))
    assert persona_from_dict(persona_to_dict(after)) == after
    assert validate_persona(after) == []
    assert looks_hash(before.appearance) != looks_hash(after.appearance)
    assert replace(after.appearance, bust=None) == before.appearance
    words = build_words(after)
    assert BUST_WORDS[cup] in words and 'full bust' not in words


@pytest.mark.parametrize('cup', CUPS)
def test_owner_edit_queues_new_photos_and_undo_restores_prior_size(store, clock, tmp_path, cup):
    app = app_for(store, clock, tmp_path)
    save_woman(store, 102, bust='medium')
    before = load_persona(store, 102)
    args = looks_args(looks(weight_kg=64, bust=cup))
    reply = run(LooksService(app).handle(args))
    assert reply['ok'] and reply['set'] == 1
    after = load_persona(store, 102)
    assert after.appearance.bust == cup
    assert replace(after.appearance, bust='medium') == before.appearance
    row = store.conn.execute('SELECT text FROM persona_ledger WHERE id=?', (reply['ledger'],)).fetchone()
    assert row[0] == f'bust medium -> {cup[0].upper()} cup'
    same = run(LooksService(app).handle(args))
    assert same['ok'] and same['ledger'] is None and same['set'] is None
    undone = run(LooksService(app).handle({'action': 'undo', 'ledger': reply['ledger']}))
    assert undone['ok'] and load_persona(store, 102).appearance == before.appearance


@pytest.mark.parametrize('cup', CUPS)
def test_cup_and_clothing_survive_profile_scene_and_words_prompt_fit(cup):
    fit = _worker_fit()
    count = clip_bpe.count if clip_bpe.tokenizers() else estimate
    for build, weight in (('average', 64), ('curvy', 88), ('voluptuous', 100)):
        p = woman(build=build, weight_kg=weight, bust=cup)
        words = W.Words('a quiet cafe', 'timer', 'casual',
                        'a navy fitted t-shirt and jeans', '', 'day', 'daylight', '', ())
        specs = (build_prompt(p), build_scene_prompt(p, original()),
                 W.build_words_prompt(p, W.scene_of(p, words, audience='him')))
        for spec in specs:
            assert not over_fit(spec)
            prompt, dropped = fit(spec.phrases, count, 75)
            assert BUST_WORDS[cup] in prompt
            assert 'wearing ' in prompt
            assert not [t for t, priority in spec.phrases if priority == 0 and t in dropped]
            assert count(prompt) <= 75
