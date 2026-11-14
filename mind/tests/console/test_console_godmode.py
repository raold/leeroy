"""Godmode in the console: strings relay under the usual gates, her strings in the member
detail only with [godmode] on, and her edits in the tuning view."""
import asyncio
import json
import logging

from console.rig import http, make_app, running
from console.test_console_tuning import FakeLink
from conftest import seed_test_cast
from leeroy_mind.console.control import Controls
from leeroy_mind.console.personaview import member_view
from leeroy_mind.console.tuneview import tuning_view
from leeroy_mind.godmode import intents as I
from leeroy_mind.human import HumanIdentity
from leeroy_mind.personas.io import load_persona
from leeroy_mind.tuning import changes as C
from leeroy_mind.tuning import edits as E

ABOUT = "the invented kitten parade"
BODY = {"action": "create", "member": 102, "kind": "post", "args": {"channel": "irl", "about": ABOUT}}
HUMAN = HumanIdentity(1, "Testmage", "Zan", "Quillon", True)


def test_string_actions_keep_key_origin_and_no_control_gates(tmp_path, caplog):
    async def go():
        link = FakeLink({"ok": True, "id": 4, "message": "Saved as string #4."})
        app = make_app(tmp_path, mind=running(tmp_path), link=link)
        await app.start()
        try:
            assert (await http(app, "POST", "/api/string", body=BODY)).status == 403
            assert (await http(app, "POST", "/api/string", body=BODY, key=False, origin=True)).status == 403
            assert not link.calls
            bad = await http(app, "POST", "/api/string", body={"action": "create", "member": 102,
                                                              "kind": "post", "args": {}}, origin=True)
            assert bad.status == 400 and not link.calls
            ok = await http(app, "POST", "/api/string", body=BODY, origin=True)
            assert ok.status == 200 and link.calls[-1][1] == "string" and link.calls[-1][2] == BODY
        finally:
            await app.close()
        off = Controls(enabled=False, reason="control off (--no-control)", mind=running(tmp_path))
        app = make_app(tmp_path, mind=running(tmp_path), link=link, controls=off)
        await app.start()
        try:
            assert (await http(app, "POST", "/api/string", body=BODY, origin=True)).status == 403
        finally:
            await app.close()
    with caplog.at_level(logging.DEBUG):
        asyncio.run(go())
    assert ABOUT not in caplog.text


def test_a_refusal_carries_its_code(tmp_path):
    async def go():
        link = FakeLink({"ok": False, "code": "explicit", "error": "Keep it non-explicit."})
        app = make_app(tmp_path, mind=running(tmp_path), link=link)
        await app.start()
        try:
            r = await http(app, "POST", "/api/string", body=BODY, origin=True)
            assert r.status == 409 and json.loads(r.body)["code"] == "explicit"
        finally:
            await app.close()
    asyncio.run(go())


def test_member_detail_shows_strings_only_with_godmode(store):
    seed_test_cast(store)
    store.set_meta("human_guid", "1")
    store.conn.commit()
    pic = C.picture(store, {g: load_persona(store, g) for g in store.persona_versions()})
    I.create(store, member=102, kind="post", args={"channel": "irl", "about": ABOUT}, human=HUMAN,
             now=store.clock.now(), pic=pic)
    now = store.clock.now()
    assert "strings" not in member_view(store.conn, 102, now=now)
    view = member_view(store.conn, 102, now=now, godmode_enabled=True)
    assert [i["kind"] for i in view["strings"]] == ["post"]
    E.set_edit(store, {"kind": "mood", "args": {"member": 102, "offset": 0.3, "seconds": 3600}},
               human=HUMAN, source="console", now=now, pic=pic)
    t = tuning_view(store.conn, now=now)
    assert [e["kind"] for e in t["edits"]["active"]] == ["mood"]
    assert t["history"][0]["edits"][0]["kind"] == "mood"
