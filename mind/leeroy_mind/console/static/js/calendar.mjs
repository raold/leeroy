import { $, arr, clear, el, num, obj, str } from "./core.mjs";
import { face } from "./faces.mjs";
import { notSetUp, pollTab, renderKpis } from "./tabkit.mjs";
import { birthdaysThisWeek, dayGroups, kindWord, nextRaid, signupCounts, youText } from "./calendarmodel.mjs";
import { when } from "./words.mjs";

// -- the calendar tab: mind.db only ---------------------------------------------------------------

var CALENDAR_MS = 60000;

function nameOf(v, g) { return str(obj(v.names)[String(g)]) || "#" + g; }
function two(n) { return (n < 10 ? "0" : "") + n; }
function hhmm(sec) { var d = new Date(sec * 1000); return two(d.getHours()) + ":" + two(d.getMinutes()); }
function dayHead(day) {
  var p = day.split("-");
  return new Date(num(p[0]), num(p[1]) - 1, num(p[2])).toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" });
}

function renderCalendar(v) {
  var now = Date.now() / 1000, up = arr(v.upcoming), missed = arr(v.missed);
  var nxt = nextRaid(up, now);
  renderKpis($("calendar-kpis"), [
    { k: "Next raid night", v: nxt ? when(nxt.at) : "-", d: nxt ? str(nxt.title) : "none posted" },
    { k: "Signed up", v: nxt ? String(arr(obj(nxt.signups).signed_up).length) : "-", d: nxt ? "for it" : "" },
    { k: "Birthdays this week", v: String(birthdaysThisWeek(up, now)), d: "" },
    { k: "Missed", v: String(missed.length), d: "raid nights, 28 days" }]);
  var box = $("calendar-upcoming"), miss = $("calendar-missed"), list = $("calendar-missed-list");
  clear(box);
  clear(list);
  if (!up.length && !missed.length && arr(v.missing).indexOf("realm_calendar") >= 0) {
    notSetUp(box, "the Realm's calendar desk has posted nothing.");
    miss.hidden = true;
    return;
  }
  miss.hidden = false;
  dayGroups(up).forEach(function (g) {
    var day = el("div", "calday");
    day.appendChild(el("h4", "", dayHead(g.day)));
    g.events.forEach(function (e) {
      var r = el("div", "srow");
      r.appendChild(el("span", "chip", kindWord(e.kind)));
      r.appendChild(el("span", "", str(e.title)));
      r.appendChild(el("span", "mono", hhmm(e.at)));
      if (e.state === "pending") { r.appendChild(el("span", "muted", "being posted")); }
      var counts = signupCounts(e.signups);
      if (counts.length) { r.appendChild(el("span", "muted", counts.join(" · "))); }
      var faces = el("span", "who");
      arr(obj(e.signups).signed_up).slice(0, 12).forEach(function (gd) { faces.appendChild(face(gd, nameOf(v, gd), "list")); });
      if (faces.firstChild) { r.appendChild(faces); }
      if (e.you) { r.appendChild(el("span", "chip", youText(e.you))); }
      day.appendChild(r);
    });
    box.appendChild(day);
  });
  if (!up.length) { box.appendChild(el("p", "note", "Nothing is posted for the next two weeks.")); }
  missed.forEach(function (m) {
    var r = el("div", "srow");
    r.appendChild(el("span", "mono", when(m.at)));
    r.appendChild(el("span", "", str(m.text)));
    list.appendChild(r);
  });
  if (!missed.length) { list.appendChild(el("p", "note", "You missed no raid night in the last 28 days.")); }
}

pollTab("calendar", { path: "/api/calendar", ms: CALENDAR_MS, render: renderCalendar });
