// The Mind tab's "Game server" panel in words: pure, so a node test can run it.
function listen(v, port) {
  return v === true ? "listening on " + port : v === false ? "not listening" : "unknown (no TCP table)";
}
function clock(sec) {
  var d = new Date(sec * 1000);
  return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}
export function serverRows(server) {
  var s = server && typeof server === "object" ? server : {};
  var g = s.game && typeof s.game === "object" ? s.game : null;
  var hello = g ? g.server + " " + g.version + ", hello at " + clock(g.hello_at) : "unknown";
  var players = "unknown";
  if (g) {
    players = (g.human_online ? "You're in game" : "You're not in game") + " · " + g.cast_online
      + " of " + g.cast + " cast online";
  }
  return {
    world: listen(s.world, 8085), auth: listen(s.auth, 3724), bridge: listen(s.bridge, 7878),
    server: hello, players: players, why: typeof s.why === "string" ? s.why : "",
  };
}
