import { api, arr, clear, el, str } from "./core.mjs";
import { face } from "./faces.mjs";

const states = new WeakMap();
export function worldCards(box, view, reload) {
  const opened = new Set(Array.from(box.querySelectorAll("details[open]")).map(n => n.dataset.guid));
  const focused = box.contains(document.activeElement) && document.activeElement.closest("details")?.dataset.guid;
  clear(box);
  box.appendChild(el("p", "note", "World personas are read-only. " + view.total + " members."));
  const list = el("div", "cards wide");
  arr(view.members).forEach(m => {
    const item = el("details", "panel"), head = el("summary", "who");
    item.dataset.guid = String(m.guid); item.open = opened.has(String(m.guid));
    head.appendChild(face(m.guid, str(m.name), "list"));
    head.appendChild(el("span", "", str(m.name) + " · " + str(m.guild_name) + " · level " + m.level + (m.retired ? " · retired" : "")));
    item.appendChild(head);
    item.appendChild(el("p", "", str(m.card)));
    list.appendChild(item);
  });
  box.appendChild(list);
  if (focused) Array.from(list.children).find(n => n.dataset.guid === focused)?.querySelector("summary").focus();
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
  if (!state) { state = {offset:0, request:null, rendered:""}; states.set(box,state); }
  if (offset !== undefined) state.offset = offset;
  if (state.request) return state.request;
  const requested = state.offset;
  state.request = (async () => {
    try {
      const response = await api("/api/world/personas?offset=" + requested);
      if (response.status !== 200) throw new Error("unavailable");
      const data = await response.json(), fingerprint = JSON.stringify(data);
      if (requested === state.offset && fingerprint !== state.rendered) {
        worldCards(box,data,n => pollWorld(box,n));
        state.rendered = fingerprint;
      }
    } catch (_) {
      if (!box.firstChild) box.appendChild(el("p", "why", "World personas could not be read."));
    }
  })();
  try { await state.request; }
  finally {
    state.request = null;
    if (requested !== state.offset) return loadWorld(box);
  }
}
