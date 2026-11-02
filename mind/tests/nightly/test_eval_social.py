"""Synthetic social measurements: exact denominators and no secret text in reports."""
import json
from dataclasses import replace

import pytest

from leeroy_mind.eval import social
from leeroy_mind.nightly.knowledge import add_fact, learn
from leeroy_mind.nightly.mood import octant
from leeroy_mind.nightly.settings import night_of_ts

DAY = 86400


def test_texture_counts_ties_explanations_moves_and_caps(h):
    for a, b, value in [(101, 102, 20), (102, 103, -30), (103, 104, 5), (1, 101, 80)]:
        h.rel(a, b, affinity=value)
    h.store.conn.execute("INSERT INTO rel_delta(a,b,dim,delta,evidence,ts) VALUES (101,102,'affinity',2,'[]',?)", (h.T,))
    h.store.conn.execute("INSERT INTO rel_delta(a,b,dim,delta,evidence,ts) VALUES (101,1,'romance_stage',1,'[\"chat:1\"]',?)", (h.T,))
    h.store.conn.execute("INSERT INTO rel_cap(night,a,b,dim,used) VALUES (?,101,102,'affinity',11)", (h.night,))
    result = social.texture(h.store, (h.T - 1, h.T + 1), human=1, speed=10)
    assert result == {"pairs": 3, "non_neutral_share": 2 / 3, "negative_ties": 1,
                      "romance_moves": 1, "unexplained_deltas": 1, "cap_breaches": 1}


def test_callbacks_only_count_sent_cast_lines_and_old_event_time(h):
    old = h.mem(101, "The raid ten days ago.", t=h.T - 10 * DAY, evidence=())
    h.utter(h.T, 101, "Remember that old raid?", [old])
    h.utter(h.T + 1, 102, "A normal line.")
    h.utter(h.T + 2, 101, "Dropped callback.", [old], dropped="policy")
    h.utter(h.T + 3, h.human, "The owner's text.", [old])
    h.utter(h.T + 4, 104, "Missing historical reference.", [9999])
    assert social.callbacks(h.store, (h.T - 1, h.T + 5)) == {
        "lines": 3, "callbacks": 1, "share": 1 / 3, "incomplete_memory_refs": 1}


def test_noise_uses_shipped_filter_distinct_days_and_merged_online_hours(h):
    h.session(h.T - 3 * DAY, h.T - 3 * DAY + 7200)
    h.session(h.T - 3 * DAY + 1800, h.T - 3 * DAY + 3600)
    for day in range(3):
        h.utter(h.T - day * DAY, 101, "These boots demand better wages.")
    h.utter(h.T + 1, 101, "Happy to help! Let me know.")
    h.utter(h.T + 2, 102, "absolutely, see you there")
    result = social.noise(h.store, (h.T - 4 * DAY, h.T + 3), chattiness=4, names={101: "Arkon", 102: "Belra"})
    assert result["lines"] == 5 and result["online_hours"] == 2
    assert result["lines_per_online_hour"] == 2.5 and result["target_per_hour"] == 25
    assert [101, "these boots demand better", 3] in result["repeated_phrases"]
    assert result["assistant_speak"] == result["soft_tells"] == 1


def test_empty_denominators_are_unavailable(h):
    bounds = (h.T - 1, h.T + 1)
    assert social.texture(h.store, bounds, human=1, speed=10)["non_neutral_share"] is None
    assert social.callbacks(h.store, bounds)["share"] is None
    assert social.noise(h.store, bounds, chattiness=4, names={})["lines_per_online_hour"] is None
    assert social.signals(h.store, bounds, human=1)["bids"]["flattery_share"] is None


def test_cast_mix_keeps_owner_targets_and_men_as_data(h):
    for guid, stance in [(101, "lust"), (102, "drawn"), (103, "fond"), (104, "none")]:
        h.woman(guid, stance)
    h.persona(105, player_gender="male", stance="fond")
    h.rel(101, h.human, romance_stage="notices")
    h.store.conn.execute("INSERT INTO away(guid,reason,left_at,returns_at) VALUES (102,'rest',?,?)", (h.T, h.T + DAY))
    ctx = h.ctx()
    ctx.settings = replace(ctx.settings, wow_vs_life=5)
    result = social.cast_mix(ctx)
    assert result["female_share"] == .8 and result["female_target"] == ctx.settings.female_share
    assert result["attitudes"] == {"lust": .25, "drawn": .25, "fond": .25, "none": .25}
    assert result["attitude_target"] == {"lust": .2, "drawn": .3, "fond": .4, "none": .1}
    assert result["rebalance_start"] == {"lust": 13, "drawn": 19, "fond": 26, "none": 6}
    assert result["men_with_stance"] == [105] and result["stages"]["lust"]["notices"] == 1
    assert result["away"] == [102] and result["dials"]["wow_vs_life"] == 5


def seed_signals(h):
    h.woman(101, "fond")
    for act, gain in [("flirt", 2), ("answered", 1), ("answered", 1)]:
        h.store.conn.execute("INSERT INTO attraction_act(guid,act,gain,mult,ref,ts,live) VALUES (101,?,?,.5,'chat:1',?,1)", (act, gain, h.T))
    h.store.conn.execute("INSERT INTO attraction_log VALUES (?,101,0,3,1,0,6,35,32,'drawn','fond')", (h.night,))
    for guid in range(101, 106):
        h.store.conn.execute("INSERT INTO lands(guid,kind,n,replies) VALUES (?,'story',5,?)", (guid, guid - 101))
    h.store.conn.execute("INSERT INTO lands(guid,kind,n,replies) VALUES (101,'*',99,99)")
    h.store.conn.execute("INSERT INTO bandit(guid,arm,context,alpha,beta,n) VALUES (101,'flattery','solo',9,1,10)")
    for ident, status in [(1, "active"), (2, "dormant"), (3, "vetoed")]:
        h.store.conn.execute("INSERT INTO lexicon(id,kind,term,origin_ref,originator,born,last_used,status) VALUES (?,'joke',?,'chat:1',101,?,?,?)", (ident, f"boots {ident}", h.T, h.T, status))
    h.store.conn.execute("INSERT INTO lexicon_adopter(term,guid,first_use) VALUES (1,101,?)", (h.T,))
    h.store.conn.execute("INSERT INTO rumor(id,root,about,canonical,created,last_told) VALUES (1,'chat:1',102,'secret canonical text',?,?)", (h.T, h.T))
    for op in ("sharpen", "correct"):
        h.store.conn.execute("INSERT INTO rumor_version(rumor,teller,hearer,op,text,ts) VALUES (1,101,103,?,'secret altered text',?)", (op, h.T))
    h.store.conn.execute("INSERT INTO feud(a,b,started,cause,sides) VALUES (101,102,?,'private cause',?)", (h.T, json.dumps({"101": [103], "102": [104]})))
    h.store.conn.execute("INSERT INTO pad_mood VALUES (101,.5,.2,-.1,?)", (h.T,))
    h.store.conn.execute("INSERT INTO critic_score VALUES (?,101,2,5)", (h.night,))
    h.store.conn.execute("INSERT INTO critic_score VALUES ('2026-10-28',101,8,2)")


def test_signals_aggregate_without_rumour_or_feud_private_text(h):
    seed_signals(h)
    result = social.signals(h.store, (h.T - 2 * DAY, h.T + 1), human=h.human)
    acts = result["attraction"][101]
    assert acts["acts"]["answered"] == {"n": 2, "gain": 2, "effective_gain": 1}
    assert acts["decay"] == 3 and acts["neglected_nights"] == 1
    assert acts["stance_changes"][0]["reason"] == "decay"
    assert result["lands"]["top"][0]["guid"] == 105 and result["lands"]["bottom"][0]["guid"] == 101
    assert result["bids"]["flattery_share"] == 1 and result["bids"]["flattery_members"] == 1
    assert result["language"]["dormant"] == [2] and result["language"]["vetoed"] == [3]
    assert result["language"]["revived"] is None and result["language"]["adopter_share"] == .2
    assert result["rumours"] == [{"id": 1, "status": "live", "versions": 2, "ops": {"sharpen": 1, "correct": 1}, "corrected": True}]
    assert result["feuds"][0]["sides"] == {"101": [103], "102": [104]}
    assert result["moods"]["members"][101] == octant(.5, .2, -.1)
    assert result["critic"]["members"][0]["mean"] == pytest.approx(2.6)
    assert "secret canonical" not in json.dumps(result) and "private cause" not in json.dumps(result)


def test_weekly_bid_rates_use_frozen_reactions_not_lifetime_counters(h):
    h.store.conn.execute("INSERT INTO bandit(guid,arm,context,alpha,beta,n) VALUES (101,'story','solo',90,10,100)")
    for ident, reaction, ts in [(1, "laugh", h.T), (2, "ignored", h.T + 1), (3, "laugh", h.T - 10 * DAY)]:
        h.store.set_meta(f"nightly:human-world:reaction:{ident}", json.dumps({"line_id": ident, "speaker": 101, "arm": "story", "reaction": reaction, "ts": ts}))
        h.store.set_meta(f"nightly:human-world:bandit:{ident}", "done")
    result = social.signals(h.store, (h.T - 1, h.T + 2), human=1)["bids"]
    assert result["weekly_receipted_bids"] == 2
    assert result["members"][101]["story"] == {"n": 2, "answered": 1, "answer_rate": .5}
    assert result["flattery_share"] == 0 and "not stored" in result["flattery_basis"]


def test_language_overuse_uses_original_logical_nights(h):
    for i in range(6):
        line = h.say(h.T + i, 101, "The same old boots joke.")
        h.store.set_meta(f"nightly:lexicon-line:{line}", "[7]")
    result = social.signals(h.store, (h.T - 1, h.T + 10), human=1)["language"]
    assert result["overuse"] == [{"guid": 101, "term": 7, "night": night_of_ts(h.T, h.clock.local().tzinfo), "uses": 6}]


def seed_secret(h):
    line = h.say(h.T - 60, 102, "between us, I failed my driving exam", channel="whisper", to=101)
    root = f"chat:{line}"
    add_fact(h.store, kind="said_about", root=root, about=102, text="Belra failed her driving exam.", secret=1, now=h.T - 60)
    learn(h.store, root, 101, "confided", ts=h.T - 60, may_tell=False)
    return root


def test_leaks_count_distinct_sent_lines_and_preserve_secret_text(h):
    seed_secret(h)
    line = h.say(h.T, 101, "belra failed the driving exam lol")
    h.store.conn.execute("UPDATE chat_line SET audience='[101,103]' WHERE id=?", (line,))
    h.say(h.T + 1, 101, "A harmless guild line.")
    h.say(h.T + 2, 104, "heard belra failed driving")
    source = h.say(h.T - 10, h.human, "I am quitting the bakery.", channel="whisper", to=102)
    h.mem(102, "Testy whispered me he is quitting the bakery.", t=h.T - 10,
          scope="private", scope_with=1, evidence=(f"chat:{source}",))
    h.say(h.T + 3, 102, "testy is quitting the bakery lol")
    h.say(h.T + 4, 102, "testy is quitting the bakery", channel="whisper", to=1)
    h.store.conn.execute("INSERT INTO probe_run(ts,model,n,leaks,rate) VALUES (?,?,36,0,0)", (h.T, h.LIVE))
    result = social.leaks(h.store, (h.T, h.T + 5), names=(h.ident.called,))
    assert result["secret_leaks"][101] == 1 and result["secret_score"][101] == -5
    assert result["omniscience"][104] == 1 and result["private_leaks"][102] == 1
    assert result["scope_leaks"] == 0 and result["probe"]["gate_open"] is True
    assert result["probe"]["n"] == 36 and result["probe"]["rate"] == 0
    assert "driving" not in json.dumps(result) and "bakery" not in json.dumps(result)


def test_private_report_follows_legacy_open_memory_ancestry(h):
    source = h.say(h.T - 20, h.human, "I am quitting the bakery.", channel="whisper", to=102)
    parent = h.mem(102, "quitting the bakery", t=h.T - 20, scope="private", scope_with=1, evidence=(f"chat:{source}",))
    h.mem(102, "quitting the bakery", t=h.T - 10, scope="open", evidence=(f"memory:{parent}",))
    h.say(h.T, 102, "quitting the bakery", channel="whisper", to=103)
    result = social.leaks(h.store, (h.T, h.T + 1), names=(h.ident.called,))
    assert result["private_leaks"][102] == 1


def test_unretained_echo_does_not_claim_a_measured_zero_secret_audience(h):
    seed_secret(h)
    h.utter(h.T, 101, "Belra failed the driving exam.")
    result = social.leaks(h.store, (h.T, h.T + 1))
    assert result["secret_leaks"][101] == 0 and result["unassessed_audience_lines"] == 1


def test_future_knowledge_and_future_memories_do_not_rewrite_historical_leaks(h):
    root = seed_secret(h)
    h.say(h.T, 104, "Belra failed driving.")
    learn(h.store, root, 104, "confided", ts=h.T + DAY)
    h.mem(104, "Belra failed driving.", t=h.T + DAY, scope="secret", evidence=(root,))
    result = social.leaks(h.store, (h.T, h.T + 1))
    assert result["omniscience"][104] == 1 and result["private_leaks"][104] == 0


def test_all_social_reports_are_read_only(h):
    seed_signals(h)
    before = h.store.conn.total_changes
    bounds = (h.T - DAY, h.T + 1)
    social.texture(h.store, bounds, human=1, speed=10)
    social.callbacks(h.store, bounds)
    social.noise(h.store, bounds, chattiness=4, names={})
    social.cast_mix(h.ctx())
    social.signals(h.store, bounds, human=1)
    social.leaks(h.store, bounds)
    assert h.store.conn.total_changes == before
