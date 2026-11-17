// One way to write a time on every tab: local, 24-hour, ISO-like. Nothing here follows the
// browser's locale, so the same moment reads the same on his PC, laptop and phone.
function pad(n) { return (n < 10 ? "0" : "") + n; }
function date(sec) { return typeof sec === "number" && Number.isFinite(sec) ? new Date(sec * 1000) : null; }
// "2026-11-17"
export function isoDate(sec) {
  var d = date(sec);
  return d ? d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) : "";
}
// "09:05"
export function hourMinute(sec) {
  var d = date(sec);
  return d ? pad(d.getHours()) + ":" + pad(d.getMinutes()) : "";
}
// "2026-11-17 09:05"
export function isoMinute(sec) {
  var d = date(sec);
  return d ? isoDate(sec) + " " + hourMinute(sec) : "";
}
// "2026-11-17 09:05:33", for a line's hover title
export function isoSecond(sec) {
  var d = date(sec);
  return d ? isoMinute(sec) + ":" + pad(d.getSeconds()) : "";
}
