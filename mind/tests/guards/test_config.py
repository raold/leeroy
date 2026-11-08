from leeroy_mind.config import load_config
from leeroy_mind.guards import load_guards_config, parse_config

import pytest


@pytest.mark.parametrize('table',[None,True,[],{'enabled':'true'},{'enabled':True,'unknown':1},
    {'enabled':True,'parent_cosine':float('nan')},{'enabled':True,'repeat_overlap':1.01},
    {'enabled':True,'repeat_seconds':0},{'enabled':True,'gag_daily_cap':True},
    {'enabled':True,'gag_caps':{'7':0}},{'enabled':True,'gag_caps':{'private':1}}])
def test_invalid_table_means_off(table):
    assert not parse_config(table).enabled


def test_defaults_overrides_and_extension_registration(tmp_path,caplog):
    path=tmp_path/'mind.toml'
    assert not load_guards_config(path).enabled
    path.write_text('[guards]\nenabled=true\nrepeat_overlap=0.8\n[guards.gag_caps]\n7=1\n')
    cfg=load_guards_config(path)
    assert cfg.enabled and not cfg.semantic_enabled and cfg.repeat_overlap==.8 and cfg.gag_caps=={7:1}
    load_config(path)
    assert 'unknown section' not in caplog.text
    path.write_text('NOT TOML [')
    assert not load_guards_config(path).enabled
