// His looks edit in a member's Looks panel, in three groups (Body: weight and figure; Hair;
// Style: wardrobe, palette, makeup and accessories): a number, selects and tick boxes, saved
// together through the mind's looks op (/api/looks). One save is one new persona version with
// one ledger row (its Undo is in the ledger below) and one queued set of profile photos. Drafts
// stay in this page's memory.
import { clear, el, emit, obj, post, str } from "./core.mjs";
import { BUST_UNSET, BUST_WORDS, GROUPS, SETS, SHAPES, STYLE, looksArgs, looksForm, looksReply,
         looksUndoArgs, optionText } from "./looksmodel.mjs";

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
  const groups = {};
  GROUPS.forEach(name => {
    const g = el("fieldset", "looks-group");
    g.appendChild(el("legend", "", name));
    groups[name] = g;
  });
  const body = groups.Body;
  const weight = el("input");
  weight.type = "number"; weight.min = form.min; weight.max = form.max; weight.step = 1;
  weight.inputMode = "numeric";
  weight.value = draft.weight !== undefined ? draft.weight : String(form.weight);
  weight.addEventListener("input", () => { draft.weight = weight.value; });
  const wl = el("label", "godmode-field", `Weight (kg, ${form.min}-${form.max})`);
  wl.appendChild(weight);
  body.appendChild(wl);
  let bust = null;
  if (form.bustable) {
    bust = el("select");
    const unset = el("option", "", BUST_UNSET); unset.value = ""; bust.appendChild(unset);
    form.busts.forEach(b => { const o = el("option", "", BUST_WORDS[b] || b.replace(/_/g, " ")); o.value = b; bust.appendChild(o); });
    bust.value = draft.bust !== undefined ? draft.bust : form.bust;
    bust.addEventListener("change", () => { draft.bust = bust.value; });
    const bl = el("label", "godmode-field", "Bust");
    bl.appendChild(bust);
    body.appendChild(bl);
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
      body.appendChild(lab);
      shapes[sh.name] = sel;
    });
  }
  const style = {};  // hair, makeup and accessories: one select each, where the mind lists values
  STYLE.forEach(st => {
    const f = form.style[st.name];
    if (!f.options.length) return;
    const sel = el("select");
    const unset = el("option", "", st.unset); unset.value = ""; sel.appendChild(unset);
    f.options.forEach(v => { const o = el("option", "", optionText(v)); o.value = v; sel.appendChild(o); });
    const key = "style:" + st.name;
    sel.value = draft[key] !== undefined ? draft[key] : f.value;
    sel.addEventListener("change", () => { draft[key] = sel.value; });
    const lab = el("label", "godmode-field", st.label);
    lab.appendChild(sel);
    groups[st.group].appendChild(lab);
    style[st.name] = sel;
  });
  const sets = {};  // wardrobe and palette: tick boxes, up to their limit
  const boxes = [];
  SETS.forEach(st => {
    const f = form.sets[st.name];
    if (!f.options.length) return;
    const key = "set:" + st.name;
    const picked = new Set(draft[key] !== undefined ? draft[key] : f.values);
    const set = el("fieldset", "looks-set");
    set.appendChild(el("legend", "", `${st.label} (up to ${st.max})`));
    const all = [];
    const limit = () => { all.forEach(c => { c.disabled = !live || (!c.checked && picked.size >= st.max); }); };
    f.options.forEach(v => {
      const c = el("input"); c.type = "checkbox"; c.value = v; c.checked = picked.has(v);
      c.addEventListener("change", () => {
        if (c.checked) picked.add(v); else picked.delete(v);
        draft[key] = [...picked];
        limit();
      });
      const lab = el("label", "looks-tick");
      lab.appendChild(c);
      lab.appendChild(document.createTextNode(" " + optionText(v)));
      set.appendChild(lab);
      all.push(c); boxes.push(c);
    });
    limit();
    groups.Style.insertBefore(set, groups.Style.children[1 + Object.keys(sets).length] || null);
    sets[st.name] = () => f.options.filter(v => picked.has(v));
  });
  GROUPS.forEach(name => { if (groups[name].children.length > 1) box.appendChild(groups[name]); });
  const status = el("p", "why"); status.setAttribute("role", "status");
  const last = said.get(guid);
  status.textContent = last ? last.words
    : "One save makes one new persona version (undo it in the ledger) and queues one new set of 4 photos, "
      + "for tonight or the photo booth; pick one of them as their photo to keep the new looks.";
  if (last && !last.ok) status.classList.add("bad");
  const acts = el("div", "acts");
  const save = el("button", "", "Save looks");
  save.type = "button";
  [weight, bust, ...Object.values(shapes), ...Object.values(style), save].forEach(x => { if (x) { x.disabled = !live; if (!live) x.title = why; } });
  if (!live) boxes.forEach(c => { c.title = why; });
  save.addEventListener("click", () => {
    let args;
    const picked = Object.fromEntries(Object.entries(shapes).map(([k, sel]) => [k, sel.value]));
    const styled = Object.fromEntries(Object.entries(style).map(([k, sel]) => [k, sel.value]));
    const chosen = Object.fromEntries(Object.entries(sets).map(([k, read]) => [k, read()]));
    try { args = looksArgs(guid, form, weight.value, bust ? bust.value : "", picked, styled, chosen); }
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
