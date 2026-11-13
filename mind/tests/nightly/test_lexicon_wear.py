import pytest
from dataclasses import replace
from nightly.test_night_lexicon import term, row, spoken
from leeroy_mind.nightly import lexicon
from leeroy_mind.culture.config import load_culture_config


def enable(h):
    (h.settings.data_dir / "mind.toml").write_text("[server]\nenabled=true\n[culture]\nenabled=true\n")
    h.settings = replace(h.settings, culture_enabled=True)


def test_culture_defaults_and_invalid(tmp_path):
    path=tmp_path / "mind.toml"
    assert not load_culture_config(path).enabled
    path.write_text("[culture]\nenabled=true\n")
    assert not load_culture_config(path).enabled
    path.write_text("[server]\nenabled=true\n[culture]\nenabled=true\nmemes_per_week=15\n")
    assert not load_culture_config(path).enabled


def test_retired_jokes_never_return(h):
    ident=term(h)
    enable(h)
    h.store.conn.execute("UPDATE lexicon SET uses_total=12 WHERE id=?",(ident,))
    lexicon.fade(h.ctx())
    assert row(h,ident)["retired"] == h.clock.now()
    spoken(h,"floor inspector",guid=1)
    lexicon.spread(h.ctx())
    assert row(h,ident)["status"] == "dormant"


def test_culture_off_keeps_todays_lexicon(h):
    ident=term(h)
    h.store.conn.execute("UPDATE lexicon SET uses_total=12 WHERE id=?",(ident,))
    lexicon.fade(h.ctx())
    assert row(h,ident)["status"] == "active"
    assert row(h,ident)["retired"] is None


def test_cold_jokes_retire_and_spread_counts_once(h):
    ident=term(h)
    enable(h)
    spoken(h,"floor inspector",guid=1)
    lexicon.spread(h.ctx())
    lexicon.spread(h.ctx())
    assert row(h,ident)["uses_total"] == 1
    h.clock.advance(61*86400)
    from dataclasses import replace
    lexicon.fade(replace(h.ctx(), night=h.clock.local().date().isoformat()))
    assert row(h,ident)["retired"] is not None


def test_banter_legacy_fold_is_once_and_damage_never_resets(h):
    from leeroy_mind.banter.topics import joke_uses
    ident=term(h)
    h.store.set_meta(f"banter:joke-use:joke:{ident}", '{"at": 5, "count": 3}')
    assert joke_uses(h.store,f"joke:{ident}",durable=True)[1] == 3
    assert joke_uses(h.store,f"joke:{ident}",durable=True)[1] == 3


@pytest.mark.parametrize("count", ["1e999", "-1e999", "NaN", "9223372036854775808", "9" * 400])
def test_damaged_or_oversized_legacy_count_retires_without_overflow(h, count):
    from leeroy_mind.banter.topics import joke_uses
    ident = term(h)
    enable(h)
    h.store.set_meta(f"banter:joke-use:joke:{ident}", '{"at": 5, "count": ' + count + '}')
    lexicon.fade(h.ctx())
    assert row(h, ident)["uses_total"] == 12
    assert row(h, ident)["status"] == "dormant"
    assert row(h, ident)["retired"] is not None
    assert joke_uses(h.store, f"joke:{ident}", durable=True)[1] == 12


@pytest.mark.parametrize('channel', ['officer', 'discord:officer-chat', 'whisper', 'dm:101'])
def test_restricted_historical_terms_and_memories_stay_frozen(h, channel):
    ident = term(h)
    source = int(row(h,ident)['origin_ref'].split(':')[1])
    h.store.conn.execute('UPDATE chat_line SET channel=? WHERE id=?', (channel,source))
    h.store.conn.execute('UPDATE lexicon SET uses_total=12,heat=0 WHERE id=?', (ident,))
    h.store.set_meta(f'banter:joke-use:joke:{ident}', '{"at": 5, "count": 3}')
    before = dict(row(h,ident))
    memories = [dict(r) for r in h.store.conn.execute('SELECT * FROM memory')]
    enable(h)
    h.clock.advance(61*86400)
    lexicon.fade(replace(h.ctx(), night=h.clock.local().date().isoformat()))
    assert dict(row(h,ident)) == before
    assert [dict(r) for r in h.store.conn.execute('SELECT * FROM memory')] == memories
    assert h.store.get_meta(f'nightly:lexicon-heat:{ident}') is None


@pytest.mark.parametrize('channel', ['guild', 'discord:general'])
def test_culture_keeps_rewarded_public_joke_births(h, channel):
    from banter.test_jokes import reward
    from leeroy_mind.banter.night import reward_jokes
    enable(h)
    line = h.say(h.T,102,'pulling with my face again',channel=channel)
    reward(h,line)
    assert reward_jokes(h.ctx()) == 1
    made, = h.store.conn.execute('SELECT * FROM lexicon')
    assert made['origin_kind'] == 'line' and made['origin_ref'] == f'chat:{line}'


def test_culture_keeps_candidate_joke_births(h):
    from conftest import run
    from nightly.test_night_language_births import moment, context, answer
    from leeroy_mind.nightly import language_births
    enable(h)
    line, _ = moment(h)
    ctx = context(h, answer(kind='joke',term='ice inspector',**{'for':None}))
    run(language_births.candidates(ctx))
    assert not ctx.failures
    made, = h.store.conn.execute('SELECT * FROM lexicon')
    assert made['kind'] == 'joke' and made['origin_ref'] == f'chat:{line}'


@pytest.mark.parametrize('enabled', [False,True])
def test_culture_is_a_startup_choice_and_hot_checks_never_read_config(h, monkeypatch, enabled):
    from conftest import run
    from nightly.test_night_wiring import app_for, context
    from leeroy_mind.culture.config import app_enabled, enabled as night_enabled
    from leeroy_mind.culture import config
    path = h.settings.data_dir / 'mind.toml'
    def write(value):
        path.write_text(f'[server]\nenabled=true\n[culture]\nenabled={str(value).lower()}\n')
    write(enabled)
    app = app_for(h)
    try:
        write(not enabled)
        ctx = context(app)
        monkeypatch.setattr(config, 'load_culture_config', lambda *_: pytest.fail('hot config read'))
        for _ in range(10):
            assert app_enabled(app) is enabled
            assert night_enabled(ctx) is enabled
        assert context(app).settings.culture_enabled is enabled
    finally:
        run(app.stop())
    monkeypatch.undo()
    restarted = app_for(h)
    try:
        assert app_enabled(restarted) is not enabled
        assert night_enabled(context(restarted)) is not enabled
    finally:
        run(restarted.stop())
