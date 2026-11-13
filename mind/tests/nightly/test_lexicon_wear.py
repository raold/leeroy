import pytest
from nightly.test_night_lexicon import term, row, spoken
from leeroy_mind.nightly import lexicon
from leeroy_mind.culture.config import load_culture_config


def enable(h):
    (h.settings.data_dir / "mind.toml").write_text("[server]\nenabled=true\n[culture]\nenabled=true\n")


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
