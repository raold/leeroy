// A member's voice sheet in the Personas detail (the persona's speech field, written by
// `personas voices`): the rows the Voice panel lists and the ledger rows an Undo can take.
// Read-only here; pure, so node --test checks it without a page.

export const VOICE_NONE = "none yet: `personas voices` imports one";
export const SAMPLE_ORDER = ["answer", "tease", "tired", "flirt", "warm", "annoyed"];
// Her stance is written for a household: after a household change it stays out of her prompts
// until a sheet is imported again (the view's stance_current).
export const STANCE_STALE = "stance (not shown: her household changed since it was written)";

function obj(x) { return x && typeof x === "object" && !Array.isArray(x) ? x : {}; }
function strs(x) { return Array.isArray(x) ? x.filter(s => typeof s === "string" && s.trim()) : []; }
function str(x) { return typeof x === "string" ? x.trim() : ""; }

// [label, value] rows for the panel: her register, shape, words, never-words, what she brings
// up, how she is with him, her stance and each sample; one "voice" row without a sheet.
export function voiceRows(card) {
  const c = obj(card), s = c.speech && typeof c.speech === "object" ? obj(c.speech) : null;
  if (!s) return [["voice", card ? VOICE_NONE : "-"]];
  const samples = obj(s.samples);
  const rows = [["register", str(s.register)], ["shape", str(s.shape)],
                ["says", strs(s.says).join(", ")], ["never says", strs(s.never).join(", ")],
                ["brings up", strs(s.brings_up).join("; ")], ["with him", str(s.with_him)],
                [c.stance_current === false ? STANCE_STALE : "stance", str(s.stance)]];
  SAMPLE_ORDER.forEach(k => { if (str(samples[k])) rows.push(["sample: " + k, str(samples[k])]); });
  return rows.filter(r => r[1] !== "");
}

// A ledger row an Undo can take: a voice sheet import or removal not undone yet.
export function canUndoVoice(row) {
  const r = obj(row);
  return r.kind === "voice" && !r.undone && Number.isInteger(r.id) && r.id > 0;
}
