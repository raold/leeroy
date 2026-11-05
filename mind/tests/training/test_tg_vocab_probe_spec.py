from dataclasses import replace
import json

import pytest

from leeroy_mind.protocol import parse_event
from leeroy_mind.training.protocol import Vocab as Event, register
from leeroy_mind.training.vocab import VocabCollector


def event(**changes):
    return replace(Event(1000, "probe", 8, 101, ("damage",), ("bolt",), ("bolt",),
                         True, ("often",), (), ()), **changes)


def collect(ev):
    collector = VocabCollector("probe", classes=(), probes=(101,))
    collector.feed(ev)
    collector.feed(event(class_=0, probe=0, spells_checked=False))
    return collector.result(at=1)


def test_authoritative_current_probe_binds_checked_spells_to_exact_spec():
    result = collect(event(spec_tab=2))
    assert result.complete
    assert result.probe_specs == {101: (8, 2)}
    assert result.probes[101].spells == frozenset({"bolt"})


@pytest.mark.parametrize("tab", [-1, 3, True, "1", None])
def test_unknown_or_invalid_tabs_never_poison_other_vocabulary(tab):
    result = collect(event(spec_tab=tab))
    assert result.complete and result.probes[101].actions
    assert not result.probe_specs


def test_default_old_producer_remains_unknown():
    assert event().spec_tab == -1
    assert not collect(event()).probe_specs
    register()
    parsed = parse_event(json.dumps(dict(v=1, type="vocab", ts=1000, id="probe", **{
        "class": 8, "probe": 101, "strategies": ["damage"], "actions": ["bolt"],
        "spells": ["bolt"], "spells_checked": True, "triggers": ["often"],
        "dc": [], "whitelist": []})))
    assert parsed.spec_tab == -1 and not collect(parsed).probe_specs


def test_class_slot_does_not_supply_probe_identity_even_with_same_guid():
    collector = VocabCollector("probe", classes=(8,), probes=(101,))
    collector.feed(event(spec_tab=2))
    collector.feed(event())
    collector.feed(event(class_=0, probe=0))
    result = collector.result(at=1)
    assert result.complete and not result.probe_specs


@pytest.mark.parametrize("changes", [{"id": "old"}, {"probe": 102}, {"spells_checked": False}, {"class_": 0}])
def test_wrong_request_target_or_unchecked_spells_cannot_bind_spec(changes):
    assert not collect(event(spec_tab=1, **changes)).probe_specs
