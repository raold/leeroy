import re
import tomllib

import pytest

from leeroy_mind.__main__ import main
from leeroy_mind.config import DEFAULT_TOML, ConfigError, load_config, write_default_config


def test_defaults_match_roadmap(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    assert (cfg.models.live, cfg.models.live_fallback, cfg.models.reflection, cfg.models.embedding) == (
        "hf.co/bartowski/TheDrummer_Orion-26B-A4B-v1.1-GGUF:IQ4_XS",
        "hf.co/bartowski/TheDrummer_Cydonia-24B-v4.3-GGUF:IQ4_XS", "qwen3.8:27b-q4_K_M", "qwen3-embedding:0.6b")
    assert (cfg.talk.chattiness, cfg.talk.raid_rule) == (4, "leaders_and_urgent")
    assert (cfg.romance.dial, cfg.romance.jealousy) == (3, 5)
    assert (cfg.cast.female_share, cfg.cast.introvert_share) == (0.7, 0.7)
    assert (cfg.memory.depth_days, cfg.memory.relationship_speed) == (5, 10)
    assert (cfg.bridge.host, cfg.bridge.port) == ("127.0.0.1", 7878)
    assert (cfg.ollama.url, cfg.ollama.max_inflight) == ("http://127.0.0.1:11434", 3)


def test_written_default_round_trips_and_never_overwrites(tmp_path):
    p = tmp_path / "mind.toml"; write_default_config(p)
    assert load_config(p) == load_config(tmp_path / "missing.toml")
    with pytest.raises(FileExistsError): write_default_config(p)


def test_the_public_default_dial_is_three_and_the_template_says_so(tmp_path):
    """Fix wave G4: the template `init` writes (and a mind.toml without the key) is the public
    dial, 3; the owner's own mind.toml keeps whatever he set."""
    assert re.search(r"^dial = 3 +# 1 milquetoast \.\. 10 filth$", DEFAULT_TOML, re.MULTILINE)
    assert main(["init", "--data-dir", str(tmp_path)]) == 0
    written = tmp_path / "mind.toml"
    assert tomllib.loads(written.read_text(encoding="utf-8"))["romance"]["dial"] == 3
    assert load_config(written).romance.dial == 3
    (tmp_path / "bare.toml").write_text("[romance]\njealousy = 7\n", encoding="utf-8")
    assert load_config(tmp_path / "bare.toml").romance.dial == 3


def test_init_never_rewrites_an_existing_dial(tmp_path):
    own = "[romance]\ndial = 9\n"
    (tmp_path / "mind.toml").write_text(own, encoding="utf-8")
    assert main(["init", "--data-dir", str(tmp_path)]) == 0
    assert (tmp_path / "mind.toml").read_text(encoding="utf-8") == own
    assert load_config(tmp_path / "mind.toml").romance.dial == 9


@pytest.mark.parametrize("snippet,key", [
    ("[talk]\nchattiness = 11", "talk.chattiness"), ("[romance]\ndial = 0", "romance.dial"),
    ("[romance]\njealousy = -1", "romance.jealousy"), ("[cast]\nfemale_share = 1.5", "cast.female_share"),
    ("[memory]\ndepth_days = 0", "memory.depth_days"), ('[talk]\nraid_rule = "chaos"', "talk.raid_rule"),
    ('[bridge]\nhost = "0.0.0.0"', "bridge.host"), ("[ollama]\nmax_inflight = 5", "ollama.max_inflight")])
def test_bad_value_names_the_key(tmp_path, snippet, key):
    p = tmp_path / "m.toml"; p.write_text(snippet)
    with pytest.raises(ConfigError, match=re.escape(key)): load_config(p)


def test_unknown_key_warns(tmp_path, caplog):
    p = tmp_path / "m.toml"; p.write_text("[talk]\nchatiness = 3")
    assert load_config(p).talk.chattiness == 4 and "talk.chatiness" in caplog.text


def test_extension_sections_are_not_warned(tmp_path, caplog):
    p = tmp_path / "m.toml"; p.write_text("[guild]\nfriends_cap = 30\n[dkp]\nkill = 12.0\n")
    load_config(p); assert "guild" not in caplog.text and "dkp" not in caplog.text


def test_init_creates_data_dir_and_keeps_existing_toml(tmp_path):
    assert main(["init", "--data-dir", str(tmp_path)]) == 0
    assert {p.name for p in tmp_path.iterdir()} == {"mind.toml", "personas", "prompts", "logs", "snapshots"}
    (tmp_path / "mind.toml").write_text("[talk]\nchattiness = 2\n")
    main(["init", "--data-dir", str(tmp_path)])
    assert load_config(tmp_path / "mind.toml").talk.chattiness == 2


@pytest.mark.parametrize("key", ["real_name", "nickname", "pronouns"])
def test_a_bad_human_value_is_never_echoed(tmp_path, key):
    """Privacy minor 1: doctor, the service log and the login reload print this error."""
    value = "Zebulon Quintavius Montgomery"  # invented, over the 24-character limit
    p = tmp_path / "m.toml"
    p.write_text(f'[human]\n{key} = "{value}"\n', encoding="utf-8")
    with pytest.raises(ConfigError) as e:
        load_config(p)
    assert f"human.{key}" in str(e.value) and "Zebulon" not in str(e.value)
    p.write_text(f"[human]\n{key} = 7\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=rf"human\.{key}"):
        load_config(p)


def test_an_unreadable_or_undecodable_file_is_a_config_error(tmp_path):
    """Correctness minor 1: the login reload catches only ConfigError."""
    p = tmp_path / "m.toml"
    p.write_bytes('[human]\nnickname = "Zo\xe9"\n'.encode("cp1252"))  # saved in a legacy code page
    with pytest.raises(ConfigError, match="UTF-8"):
        load_config(p)
    d = tmp_path / "dir.toml"
    d.mkdir()
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(d)


@pytest.mark.parametrize("url", ["https://127.0.0.1:11434", "http://192.0.2.10:11434",
                                 "http://ollama.example:11434", "127.0.0.1:11434", "http://"])
def test_ollama_must_be_plain_http_on_loopback(tmp_path, url):
    """Carry L61: the client's select-based reads are wrong for TLS, and chat must not leave
    the machine."""
    p = tmp_path / "m.toml"
    p.write_text(f'[ollama]\nurl = "{url}"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="ollama.url"):
        load_config(p)


@pytest.mark.parametrize("url", ["http://127.0.0.1:11434", "http://localhost:11434",
                                 "http://[::1]:11434", "http://127.0.0.1:11434/"])
def test_loopback_ollama_urls_pass(tmp_path, url):
    p = tmp_path / "m.toml"
    p.write_text(f'[ollama]\nurl = "{url}"\n', encoding="utf-8")
    assert load_config(p).ollama.url == url
