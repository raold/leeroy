// His weight and figure edit in a member's Looks panel: a number and selects, saved through the
// mind's looks op (/api/looks). Each save is a new persona version with a ledger row (its Undo is
// in the ledger below) and queues a fresh set of profile photos. Drafts stay in this page's memory.
import { clear, el, emit, obj, post, str } from "./core.mjs";
import { BUST_UNSET, BUST_WORDS, SHAPES, looksArgs, looksForm, looksReply, looksUndoArgs } from "./looksmodel.mjs";

const drafts = new Map();
const said = new Map();  // the last answer per member, shown again after a redraw

function ask(args, what, guid, done) {
  return post("/api/looks", args).then(r => {
    if (r.status === 403 && !r.error) { emit("key-lost"); return r; }
    const words = looksReply(r);
    said.set(guid, { ok: r.ok === true, words });
    emit("note", what + (r.ok === true ? ": " : " refused: ") + words);
    if (r.ok === true && done) done();
    return r;
  }, () => { emit("note", what + ": the console could not be reached"); });
}

export function looksEditor(box, j, guid, live, why, done) {
  if (!box) return;
  const card = j && j.card ? j.card : null;
  if (box.dataset.guid === String(guid) && box.contains(document.activeElement)) return;  // he is typing
  box.dataset.guid = String(guid);
  clear(box);
  const form = looksForm(card, obj(j).looks_limits);
  if (!form) return;
  const draft = drafts.get(guid) || {};
  drafts.set(guid, draft);
  const weight = el("input");
  weight.type = "number"; weight.min = form.min; weight.max = form.max; weight.step = 1;
  weight.inputMode = "numeric";
  weight.value = draft.weight !== undefined ? draft.weight : String(form.weight);
  weight.addEventListener("input", () => { draft.weight = weight.value; });
  const wl = el("label", "godmode-field", `Weight (kg, ${form.min}-${form.max})`);
  wl.appendChild(weight);
  box.appendChild(wl);
  let bust = null;
  if (form.bustable) {
    bust = el("select");
    const unset = el("option", "", BUST_UNSET); unset.value = ""; bust.appendChild(unset);
    form.busts.forEach(b => { const o = el("option", "", BUST_WORDS[b] || b.replace(/_/g, " ")); o.value = b; bust.appendChild(o); });
    bust.value = draft.bust !== undefined ? draft.bust : form.bust;
    bust.addEventListener("change", () => { draft.bust = bust.value; });
    const bl = el("label", "godmode-field", "Bust");
    bl.appendChild(bust);
    box.appendChild(bl);
  }
  const shapes = {};  // glutes, waist and hips beside her bust, women only
  if (form.bustable) {
    SHAPES.forEach(sh => {
      const sel = el("select");
      const unset = el("option", "", sh.unset); unset.value = ""; sel.appendChild(unset);
      form.shapes[sh.name].options.forEach(v => { const o = el("option", "", v.replace(/_/g, " ")); o.value = v; sel.appendChild(o); });
      const key = "shape:" + sh.name;
      sel.value = draft[key] !== undefined ? draft[key] : form.shapes[sh.name].value;
      sel.addEventListener("change", () => { draft[key] = sel.value; });
      const lab = el("label", "godmode-field", sh.label);
      lab.appendChild(sel);
      box.appendChild(lab);
      shapes[sh.name] = sel;
    });
  }
  const status = el("p", "why"); status.setAttribute("role", "status");
  const last = said.get(guid);
  status.textContent = last ? last.words
    : "Saving makes a new persona version (undo it in the ledger) and queues a new set of photos.";
  if (last && !last.ok) status.classList.add("bad");
  const acts = el("div", "acts");
  const save = el("button", "", "Save looks");
  save.type = "button";
  [weight, bust, ...Object.values(shapes), save].forEach(x => { if (x) { x.disabled = !live; if (!live) x.title = why; } });
  save.addEventListener("click", () => {
    let args;
    const picked = Object.fromEntries(Object.entries(shapes).map(([k, sel]) => [k, sel.value]));
    try { args = looksArgs(guid, form, weight.value, bust ? bust.value : "", picked); }
    catch (e) { status.textContent = e.message; status.classList.add("bad"); return; }
    status.classList.remove("bad");
    status.textContent = "Saving…";
    save.disabled = true;
    ask(args, "Looks", guid, () => { drafts.delete(guid); }).then(r => {
      save.disabled = !live;
      status.textContent = looksReply(r);
      status.classList.toggle("bad", !(r && r.ok === true));
      if (r && r.ok === true && done) done();
    });
  });
  acts.appendChild(save);
  box.appendChild(acts);
  box.appendChild(status);
}

// The Undo of one weight or bust change, from the member's ledger.
export function looksUndoButton(row, guid, live, why, done) {
  const b = el("button", "", "Undo");
  b.type = "button";
  b.disabled = !live;
  if (!live) b.title = why;
  b.setAttribute("aria-label", "Undo ledger #" + str(row.id));
  b.addEventListener("click", () => {
    b.disabled = true;
    ask(looksUndoArgs(row.id), "Undo ledger #" + row.id, guid).then(r => {
      b.disabled = !live;
      if (r && r.ok === true && done) done();
    });
  });
  return b;
}
