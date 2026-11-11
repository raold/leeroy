import { $, api, arr, clear, el, num, obj, post, refused, str } from "./core.mjs";
import { when } from "./words.mjs";
import { badges, DETECTORS, rate, variant } from "./insightmodel.mjs";

var selected = 0, generation = 0, selection = 0, loaded = null, budget = "medium", checking = false;
var cardNode = null, droppedNode = null;
export function appendInsightBadges(parent, value) {
  badges(value).forEach(function (b) {
    var badge = el("span", "chip insight-badge", b.text);
    badge.title = b.title; badge.setAttribute("aria-label", b.text + " " + b.title);
    parent.appendChild(badge);
  });
}
function section(title, part) {
  var box = el("section", "insight-part");
  box.appendChild(el("h4", "", title));
  if (part.why) { box.appendChild(el("p", "why", str(part.why))); }
  return box;
}
function claimList(box, rows) {
  var list = el("ul", "insight-claims");
  arr(rows).forEach(function (r) {
    list.appendChild(el("li", "", "#" + num(r.id) + " · " + str(r.kind) + " · " + str(r.subject)
      + " · polarity " + num(r.polarity) + " · seen " + num(r.seen) + " · " + when(r.last_seen)
      + (r.expires ? " · expires " + when(r.expires) : "")
      + (r.superseded_by ? " · replaced by #" + num(r.superseded_by) : "")));
  });
  box.appendChild(list);
  if (!arr(rows).length) { box.appendChild(el("p", "why", "No rows recorded.")); }
}
function showCard() {
  var v = variant(obj(loaded).awareness, budget);
  cardNode.textContent = v.text;
  droppedNode.textContent = "Dropped at this budget: " + v.dropped;
}
function render(value) {
  loaded = value;
  var box = $("insight-parts"), folds = {};
  box.querySelectorAll('details[data-insight-fold]').forEach(d => { folds[d.dataset.insightFold] = d.open; });
  var focusBudget = document.activeElement?.id === 'insight-budget';
  clear(box);
  var claims = obj(value.claims), c = section("Claims", claims);
  if (claims.state === "ok") {
    claimList(c, claims.live);
    var history = el("details"), summary = el("summary", "", "Superseded history");
    history.dataset.insightFold = 'claims'; history.open = !!folds.claims;
    history.appendChild(summary); claimList(history, claims.history); c.appendChild(history);
  }
  box.appendChild(c);
  var a = obj(value.awareness), awareness = section("Awareness card", a);
  var label = el("label", "", "Token budget "), select = el("select");
  select.setAttribute("aria-label", "Awareness token budget");
  select.id = 'insight-budget';
  ["small", "medium", "large"].forEach(function (v) {
    var option = el("option", "", v + " (" + ({small:200, medium:600, large:1200})[v] + ")");
    option.value = v; select.appendChild(option);
  });
  select.value = budget; select.addEventListener("change", function () { budget = select.value; showCard(); });
  label.appendChild(select); awareness.appendChild(label);
  var pre = el("pre", "insight-card"); pre.id = "insight-card"; awareness.appendChild(pre);
  var dropped = el("p", "why"); dropped.id = "insight-dropped"; awareness.appendChild(dropped);
  cardNode = pre; droppedNode = dropped;
  if (arr(a.missing).length) { awareness.appendChild(el("p", "why", "Missing sources: " + a.missing.join(", "))); }
  box.appendChild(awareness);
  var q = obj(value.quality), quality = section("Talk quality", q);
  if (q.rates) {
    quality.appendChild(el("p", "why", (q.fake ? "Fake candidate audit" : q.judged ? "Judged audit" : "Candidate audit")
      + " · " + when(q.until) + " · rank " + num(q.rank) + " of " + num(q.guild_members)
      + " by flagged-line count (highest first). Flags are possible issues, not accuracy scores."));
    DETECTORS.forEach(n => quality.appendChild(el("p", "", n + ": " + rate(obj(q.rates)[n]))));
    var trend = el("details"); trend.appendChild(el("summary", "", "Recent compatible runs"));
    trend.dataset.insightFold = 'trend'; trend.open = !!folds.trend;
    arr(q.trend).forEach(function (r) {
      trend.appendChild(el("p", "", when(r.until) + " · " + DETECTORS.map(n => n + ": " + rate(obj(r.rates)[n])).join("; ")));
    });
    quality.appendChild(trend);
  } else if (q.command) {
    quality.appendChild(el("p", "why", "Run this yourself on a scratch copy; replace D with its data directory. This read-only panel does not launch evaluations."));
    quality.appendChild(el("code", "insight-command", str(q.command)));
  }
  box.appendChild(quality); box.appendChild(section("Guard verdicts", obj(value.guards))); showCard();
  if (focusBudget) { select.focus({preventScroll:true}); }
}
export function selectInsight(guid) {
  if (guid !== selected) {
    selected = guid; generation++; selection++; loaded = null;
    $("insight-line").value = ""; $("insight-result").textContent = "";
    clear($("insight-parts"));
  }
}
export function pollInsight(guid) {
  selectInsight(guid);
  if (!guid) { return; }
  var seq = ++generation;
  api("/api/persona/insight?guid=" + guid, { poll: true }).then(function (r) {
    if (refused(r)) { return null; }
    return r.json().then(j => ({ok:r.ok, j:j}));
  }).then(function (r) {
    if (!r || seq !== generation || selected !== guid) { return; }
    $("insight-status").textContent = r.ok ? "Read-only · updated " + when(r.j.at) : str(r.j.error) || "Insight is unavailable.";
    if (r.ok) { render(r.j); } else { clear($("insight-parts")); }
  }).catch(function () { if (seq === generation) { $("insight-status").textContent = "Insight could not be reached."; } });
}
$("insight-check").addEventListener("submit", async function (event) {
  event.preventDefault();
  if (checking || !selected) { return; }
  checking = true; var guid = selected, current = selection;
  $("insight-submit").disabled = true; $("insight-result").textContent = "Checking…";
  try {
    var reply = await post("/api/persona/check", {guid:guid, text:$("insight-line").value});
    if (current !== selection) { return; }
    $("insight-result").textContent = reply.ok ? (arr(reply.conflicts).length
      ? reply.conflicts.map(c => "Possible conflict with claim #" + num(c.id) + ": " + str(c.subject) + " (" + str(c.reason) + ")").join("; ")
      : "No candidate conflicts found; this is not proof that the line is consistent.") : str(reply.error) || "The mind refused the check.";
  } catch (_) { if (current === selection) { $("insight-result").textContent = "The check could not be reached."; } }
  finally { checking = false; $("insight-submit").disabled = false; }
});
