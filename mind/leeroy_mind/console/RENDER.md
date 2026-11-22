# Render selfies

The top-bar **Render selfies** button, beside Free GPU on every tab, renders the photo backlog
now instead of leaving it to the night or the photo booth. The mind side is
`selfies/manual.py`; the page is `static/js/render.mjs` with its words in `rendermodel.mjs`.

## What it renders

In this order:

1. **First sets**: a member with looks and a photo kind but no photo yet (queued as the night
   queues them).
2. **"4 more" sets** he asked for.
3. With `[selfies] snaps = true`, **planned situational and guild photos** that can render now.

The button reads `Render selfies (11)`. A click opens a dialog with:

- the counts by kind;
- the held sets that stay queued, and the sets the photo check refused earlier today, which wait
  for the night;
- a rough time;
- what stops a run.

The time comes from earlier renders: the booth's and this button's seconds per image that came
back, else the night's selfies stage. A pass whose renders only failed fast measures nothing.
With nothing measured the dialog says "no estimate yet".

**Photo requests are not part of it.** A request renders when he asks, inside its own bounded
GPU borrow (`photos/requests.py`), or sends a ready-made photo, so it never waits in a backlog.

It runs the night's own renderers (`render.render` for sets, the snaps stage's renderer for
photos). Prompts, the age guard, the minors screens, the NSFW gate, negatives, the clothed rule,
pairings, held-set rules, seeds and retries are theirs, unchanged. **Held sets stay held and
queued.**

## What it bypasses, and what it never does

It skips only the booth's scheduling limits: `booth_wait_minutes` (request age),
`booth_sets_per_day`, the failure cooldown, the ten minutes offline. It also skips the night's
per-night caps (`selfie_per_night`, `snaps_per_night`): the whole backlog renders. It needs
`[selfies] enabled = true` (and `snaps = true` for photos, as the night does), never
`booth = true`.

It never bypasses a content rule, a lease or the GPU pause.

## Refusals

Each is said in words on the button's title, and as a toast on click:

- he is in WoW, or his presence is unknown (before the first hello, bridge down);
- an offline raid, the voice service, a photo request or another live job holds the GPU;
- Free GPU is paused ("Resume the GPU first");
- a night is running or winding down: its own photo stages render the queue. Try again after it
  ends;
- the photo booth is rendering;
- members are writing answers to him on Discord, or owe him answers that fall due while the run
  would render. This covers DM and group DM answers put off to her own time; with no estimate,
  the next 30 minutes count. A run would hold those back, so it waits for them instead;
- with `[server]` on, the night's quiet window. Inside the window, no run starts until tonight's
  night has finished. Outside it, no run starts when its estimate is longer than the time left
  before the window opens;
- a run is already going;
- selfies are off, or nothing is queued (with how many refused sets wait for the night).

Until the console has a fresh read from the mind, the button says it is checking (or that
selfies are off, as `mind.toml` says). It never shows an old read.

## The GPU

A run takes the GPU as an image stage does. Admission is one step: the checks, letting go of
the server and Discord presence leases (as Free GPU does) and the run's start. The run unloads
every model but the embedding model and starts the worker. A second worker, the identity one,
starts for the photos.

**Any lease taken after the click stops the run**, before or after its first step. That covers:

- his Discord post;
- a rescue of an answer owed to him;
- a whisper answer carried to his DM;
- a group DM answer;
- his WoW login;
- an offline raid or the voice service.

Free GPU stops it too. The worker's whole process tree ends before any live model preloads. The
outcome names the cause: his post, an answer owed to him, a group DM's answer, his login and so
on.

**A run a lease stopped resumes by itself** (2026-11-21): the same local day, at least two
minutes after it stopped (an offline raid's start refused as `driver_pending` tries again after
one), once the click's own checks pass again; the mind looks every minute. Sets already ready
stay ready, so the resumed run renders what is still queued. His Stop (also pressed while it
waits, which cancels the resume), Free GPU and a shutdown never resume; a pause or an empty
backlog met while it waits drops it; at most six resumes a day. Logs
`selfies_manual_render_resumed tries=` and `selfies_manual_resume_dropped reason=`.

While a run renders:

- **no night falls due**: a night that would start waits for the run to end, then starts at the
  scheduler's next tick, with no attempt spent;
- the photo booth does not start, and a photo request's borrow is busy;
- the server lease stays released, so the guild's Discord stays quiet. The run's end takes it
  back at once, and that hand-over reloads the live model whole.

## Progress and Stop

While a run renders the button reads `Rendering 3 of 11 · Stop`. Its state comes from the
console's shared status stream, as the GPU control's does; the page keeps no timer and no state.
**Stop** ends the worker tree and answers once it has gone (or says it is still stopping after 60
seconds). A set mid-render stays queued; its `.part` folder is cleared by the next render, as
after the booth.

The outcome (done, stopped, stopped by what took the GPU, or failed, with counts) shows as a
toast on a page that watched the run, and in Ops under Selfies and snaps. A run stopped before
its first step is recorded too. `selfies status` prints it as `manual`. With `[selfies] critic`
on, a measured photo counts as rendered only once it is published.

## Receipts

Units go under night `manual:<local date>`, stages `selfies_manual` and `snaps_manual`, with a
`night_stage` row of their own and meta `selfies:manual:last`. A run never resets a night's stall
counter, never spends the booth's day allowance and never shows as nightly work.

A set the photo check refused (NSFW, three seeds) is skipped by later runs that day, since the
same seeds would be refused again; the night tries it with new seeds. A set a worker error ended
(a timeout, contention, an out-of-memory) is offered again at once.

## Control

The mind's `selfie` control op takes two more actions, `render` and `render_stop`, each with no
other argument. Its `status` carries `selfie_render` (running, progress, backlog, refusal and the
last run) while selfies are on. The console relays `POST /api/selfies/render`
`{"action": "render" | "render_stop"}` behind its usual key, Host, Origin and fetch-site gates.
`--no-control` disables the button, with the reason as its title. It does not need
`[control] ops_actions`.

Logs: `selfies_manual_render_started total= kinds=`, `selfies_manual_render_done done= failed=
held= seconds= outcome= by=`, and `selfies_manual_render_refused reason=`. They carry codes and
counts only.

## Operator notes

- No migration and no switch: the button appears with this release and does nothing until
  clicked. Rolling back leaves only harmless `manual:` receipts.
- Ops' **Render now** is unchanged: it still asks the booth (`booth = true` only) under the
  booth's own limits, and refuses while a Render selfies run renders.
- A run renders whatever is queued when it starts, plus "4 more" asked while its sets render.
  Photos planned after it started wait for the night or the next run.
