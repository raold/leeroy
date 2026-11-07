import test from "node:test";
import assert from "node:assert/strict";
import { serverRows } from "../../../leeroy_mind/console/static/js/gameserver.mjs";

test("the game server panel words each row", () => {
  const at = new Date(2026, 10, 6, 21, 5).getTime() / 1000;
  const r = serverRows({ world: true, auth: false, bridge: true, why: null,
    game: { server: "Acme", version: "3.3.5", hello_at: at, cast: 5, cast_online: 3, human_online: true } });
  assert.deepEqual(r, { world: "listening on 8085", auth: "not listening", bridge: "listening on 7878",
    server: "Acme 3.3.5, hello at 21:05", players: "You're in game · 3 of 5 cast online", why: "" });
  assert.equal(serverRows({ game: { server: "A", version: "1", hello_at: at, cast: 2, cast_online: 0, human_online: false } }).players,
    "You're not in game · 0 of 2 cast online");
});
test("a missing table or mind reads as unknown, with the reason", () => {
  const r = serverRows({ world: null, auth: null, bridge: null, game: null, why: "The mind is stopped, so the cast count is unknown." });
  assert.equal(r.world, "unknown (no TCP table)");
  assert.equal(r.bridge, "unknown (no TCP table)");
  assert.equal(r.server, "unknown");
  assert.equal(r.players, "unknown");
  assert.equal(r.why, "The mind is stopped, so the cast count is unknown.");
  assert.equal(serverRows(undefined).world, "unknown (no TCP table)");
});
