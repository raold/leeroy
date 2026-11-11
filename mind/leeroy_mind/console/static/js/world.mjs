import { api, arr, clear, el, str } from "./core.mjs";
import { face } from "./faces.mjs";

const states = new WeakMap();
export function worldCards(box, view, reload) {
  clear(box);
  box.appendChild(el("p", "note", "World personas are read-only. " + view.total + " members."));
  const list = el("div", "cards wide");
  arr(view.members).forEach(m => {
    const item = el("details", "panel"), head = el("summary", "who");
    head.appendChild(face(m.guid, str(m.name), "list"));
    head.appendChild(el("span", "", str(m.name) + " · " + str(m.guild_name) + " · level " + m.level + (m.retired ? " · retired" : "")));
    item.appendChild(head);
    item.appendChild(el("p", "", str(m.card)));
    list.appendChild(item);
  });
  box.appendChild(list);
  if (!arr(view.members).length) box.appendChild(el("p", "note", "No world personas have been generated yet."));
  const pager = el("div", "acts pager");
  [["Previous", Math.max(0, view.offset - view.page_size), view.offset === 0], ["Next", view.next, view.next === null]].forEach(([label, offset, disabled]) => {
    const b = el("button", "btn", label); b.type = "button"; b.disabled = disabled;
    b.addEventListener("click", () => reload(offset)); pager.appendChild(b);
  });
  box.appendChild(pager);
}

export function pollWorld(box, offset) { return loadWorld(box,offset); }
async function loadWorld(box, offset) {
  let state = states.get(box);
  if (!state) { state = {offset:0, seq:0}; states.set(box,state); }
  if (offset !== undefined) state.offset = offset;
  const seq = ++state.seq;
  try {
    const response = await api("/api/world/personas?offset=" + state.offset);
    if (response.status !== 200) throw new Error("unavailable");
    const data = await response.json();
    if (seq === state.seq) worldCards(box,data, n => pollWorld(box,n));
  } catch (_) {
    if (seq === state.seq && !box.firstChild) box.appendChild(el("p", "why", "World personas could not be read."));
  }
}
