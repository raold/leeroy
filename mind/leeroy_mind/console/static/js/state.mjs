import { str } from "./core.mjs";

// What the page knows, shared by its modules: the console's status (the strip: the mind, the
// bridge, the game, voice and the control rules), the voice service's page state (null while it
// is down) and the open tab. S.status is replaced before a "status" or "snapshot" event goes out.
export var S = {
  status: { mind: { state: "unknown", detail: "connecting" }, bridge: null,
            game: { state: "unknown", reason: "connecting" },
            voice: { enabled: false, service: false, link: null }, control: { enabled: false } },
  voice: null,
  tab: "comms"
};

// What the voice service's state says about people and rooms
export function name(guid) {
  var n = S.voice && S.voice.names[guid];
  return typeof n === "string" && n ? n : "#" + guid;
}
export function roomName(id) {
  var tree = S.voice ? S.voice.tree : [];
  for (var i = 0; i < tree.length; i++) { if (tree[i].id === id) { return str(tree[i].name) || id; } }
  return id;
}
export function hereRoom() {
  if (!S.voice) { return ""; }
  return S.voice.room || (S.voice.human && typeof S.voice.where[S.voice.human] === "string" ? S.voice.where[S.voice.human] : "");
}
export function roomOf(guid) {
  if (!S.voice) { return ""; }
  var r = S.voice.where[String(guid)];
  return typeof r === "string" && r ? roomName(r) : "";
}
