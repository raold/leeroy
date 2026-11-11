import test from "node:test";
import assert from "node:assert/strict";
import {api, on} from "../../../leeroy_mind/console/static/js/core.mjs";

for (const path of ["/api/persona?guid=101", "/api/selfies/member?guid=101", "/api/selfies/images?guid=101&set=1"]) {
  test("documented absence resolves and clears poll health: " + path, async t => {
    const events = [];
    on("request-health", e => { if (e.path === path) events.push(e); });
    t.mock.method(globalThis, "fetch", async () => new Response('{"error":"no photos"}',
      {status:404, headers:{"content-type":"application/json"}}));
    const r = await api(path, {poll:true});
    assert.equal(r.status,404);
    assert.equal((await r.json()).error,"no photos");
    assert.equal(events.at(-1).ok,true);
  });
}

test("read errors keep the server's JSON and status", async t => {
  for (const status of [400,409,503]) {
    t.mock.method(globalThis,"fetch",async () => new Response('{"error":"mind.db could not be read (DatabaseError)"}',
      {status, headers:{"content-type":"application/json"}}));
    const r = await api("/api/guild", {poll:true});
    assert.equal(r.status,status);
    assert.equal((await r.json()).error,"mind.db could not be read (DatabaseError)");
  }
});

test("one-shot failures never advertise an automatic retry", async t => {
  const events = [];
  on("request-health", e => events.push(e));
  t.mock.method(globalThis,"fetch",async () => {throw new Error("offline");});
  for (const path of ["/api/library?q=fixture", "/api/selfies/gallery?guid=101", "/api/selfies/images?guid=101&set=1"]) {
    await assert.rejects(api(path), /offline/);
  }
  assert.equal(events.length,0);
});
