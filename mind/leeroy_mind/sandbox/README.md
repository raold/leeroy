# The sandbox mind (`leeroy_mind.sandbox`)

Talk to the cast as the owner without the game, on a private copy of the guild, and see exactly
what each answer was made from. A real `MindApp` (the same talk pipeline, guards, beat, learned
arms and lessons, memories and prompts as the live mind) runs on a copy of the live data, with
the test suite's fake bridge instead of mod-leeroy and the real Ollama's live model. A small
HTTP API on 127.0.0.1 says his lines, flips his presence and returns every answer with its
latency, model calls, guard verdict and codes, the beat's want and use, and the exact prompt
messages that produced it. Reset puts the copy back, so a scripted conversation can be replayed
after a prompt change.

## Run it

From `mind/`, with the live mind running or not:

```bash
D=F:/azerothcore/mind-data
OUT=C:/Users/<you>/leeroy-sandbox      # outside the data dir and outside any git checkout
.venv/Scripts/python -m leeroy_mind sandbox --data-dir "$D" --out "$OUT" [--port N] [--online]
    [--seed S] [--reuse] [--fresh-library]
```

It prints `sandbox: http://127.0.0.1:N` once the mind is up and has been greeted by its bridge.
Ctrl+C or `POST /stop` stops it. `--online` starts with him in game (else he is out of the game
and only DMs are possible until `POST /login`). `--reuse` runs on the snapshot already in `OUT`
instead of copying the live data again: the way to replay a conversation on the same state with
different code. library.db (about 1.6 GB) is copied once per `OUT`; `--fresh-library` copies it
again.

A scripted conversation, one of his lines per line of the script (blank lines and lines
starting with `#` are skipped):

```bash
.venv/Scripts/python -m leeroy_mind sandbox-talk --url http://127.0.0.1:N --guid G \
    --channel dm --script talk.txt [--json out.json] [--wait 120] [--reset] [--prompts]
```

It prints a transcript: each answer with its latency, model calls (and which was sent), prompt
size (messages and estimated tokens), guard verdict and codes, and the beat's want; members'
other lines, drops with their reasons and codes, and reactions. `--prompts` prints each answer's
prompt, `--json` saves every result whole, `--reset` resets the sandbox first. For `whisper`,
`guild`, `officer` and `party` it logs him in first (`party` needs `POST /group` before).

### The API

| Request | What it does |
|---|---|
| `GET /status` | him (guid, name, in game), the live model and whether it answers, the background tasks switched off, bridge commands refused, models refused, Free GPU state |
| `GET /members[?online=1]` | the cast: guid, name, online, stance, a summary (age, gender, region, stance, job) |
| `GET /log[?since=N&limit=M]` | the sandbox's `mind.log` lines as JSON; `next` is the cursor for the next read |
| `POST /say {"channel", "to", "text"[, "wait"]}` | his line; `channel` is `dm`, `whisper`, `guild`, `officer` or `party`; `to` the member for `dm` and `whisper`; waits up to `wait` (default 120, at most 600) seconds |
| `POST /login {["wait"]}` / `POST /logout` | his presence in game, as the bridge's login and logout; `wait` collects the greetings |
| `POST /group {"members": [...]}` | his group with those members (empty: no group; more than four: a raid) |
| `POST /reset` | stop the mind, restore the copy from the snapshot taken at the start, start a new mind on it, him as at the start |
| `POST /stop` | stop the sandbox |

A DM goes through the control endpoint's `discord` op, the path the console's
`POST /api/discord` takes (the desk stores it, the members' answers are planned as for his real
DM). A whisper or guild, officer or party line is the bridge's chat event for his line, as
mod-leeroy delivers it.

`/say` answers once at least one answer has come and nothing has been planned or typed for 4
seconds, or at the deadline (`settled: false` when turns were still in flight). Its JSON:
`answers` (the lines that answer him where he spoke: `guid`, `name`, `text`, `latency_s`,
`kind`, `why`, `turn_id`, `llm_s`, `attempts` (the generation's model calls) and `sent_attempt`
(which of them was sent; 0 is the first draft), `guard` (`verdict`, `rewritten`, `codes` and each
review), `beat` (`asked`, `want`, `use`, `ms`, or `skipped`), `prompt` (`messages`, `tokens`,
`options`, `model`, `think`, the model's raw `reply`) and `first_prompt` when a later call was
sent), `others` (members' lines elsewhere meanwhile; never a whisper between two members),
`dropped` (turns that said nothing: `reason`, guard `codes`, any stored draft), `reactions`
(emoji on his DM) and `events` (the mind's own log events for the turn: ids and codes only).

The prompts come from the sandbox's own `[bakeoff] capture` (`bakeoff/README.md`): each is the
request whose reply became the line, **without the beat's lines** (capture takes them out, and
keeps only that there was one and its want). A line written by code (an announcement, a
fallback with no model words) has no prompt.

The API answers only a program on this machine that asks directly: the Host must be its own
address, a request with an `Origin` (any browser page) is refused, and every POST must carry
`Content-Type: application/json`. There is no key: any local program can drive it while it runs.

## What is switched off

The sandbox's `mind.toml` is the live one with these changes (`prepare.QUIET`):

- members never start a conversation with him (`[talk] whispers_per_hour` and
  `openers_per_hour` 0) and the server engine starts none (`[server] conversations_per_hour` and
  `dms_per_day` 0; the server's presence, cards and guards stay);
- off: voice and voice notes, training, the Realm and world personas, selfies (the night's image
  stages and the booth), members' photos and photo requests, banter, outreach, play styles, the
  mechanic feed, culture's calendar, loot talk, server bots and memes, new personas at night,
  offline raids and the regear, and the Ops actions;
- `[ollama] max_inflight` 2 (of Ollama's 4 slots, beside the live mind's own);
- `[bakeoff] capture = true`, so every turn's prompt is kept in the sandbox's mind.db.

At run time, on top of that:

- only the control endpoint, the server's presence tick and GPU lease and the signals capture
  run in the background; every other background task (the night, guild life, the Realm, the
  server's engine and DMs, godmode intents, decisions, banter, the booth...) is left out, and
  the night is never due;
- the model client may chat only with the live model and embed only with the embedding model:
  any other model (the night's reflection model, a persona generator) is refused before it
  reaches Ollama. The client is not the mind's own Ollama client, so the mind treats it as
  injected: its GPU arbiter never loads, unloads or pins a model (`NullAdmin`), and guild life
  never reads the game database;
- the bridge is the test suite's `FakeBridge` on a free port: it greets the mind with the copy's
  cast (the whole cast online, as the pinned cast is in the game) and acks only speech (`say`,
  `emote`, `ping`, `get_cast`, `afk`); every other command is refused `sandbox` and counted in
  `/status`. Nothing reaches WoW.

What stays as it is: the talk pipeline, guards, claims, awareness cards, the beat, typing
(realism and texture), learned arms and lessons, signals, memories (closed threads are extracted
on the live model, as in the live mind), the owner's dials and godmode edits.

## The GPU

The sandbox uses the GPU through the shared Ollama: the live model (Orion) that the live mind
keeps loaded, with the same requests and context size, so nothing is reloaded or unloaded, and
the embedding model, which the live mind pins too. Its requests queue beside the live mind's in
Ollama's four slots, so a busy sandbox can slow the live guild's answers a little. If the copy
says the GPU is paused (the console's Free GPU when it was taken), the sandbox says so at start
and its members stay silent: resume in the console, then start the sandbox again without
`--reuse`.

## The copy is private

`OUT` holds a complete copy of the guild: `mind.db` (every member's memories, his whispers and
DMs, explicit ones included), personas, prompt overlays, mind.toml (his real name), library.db,
and the prompts captured in the sandbox. It lives only under `OUT`: nothing is written anywhere
else, and the live data dir is only read (mind.db and library.db through SQLite's online backup,
on read-only connections that never block the live mind and write nothing but a reader's marks in
SQLite's shared-memory `-shm` file, as every reader does; the folders by copying). Keep `OUT`
outside anything that is synced or shared, and delete it when done (no command prunes it).
`OUT` is refused inside the data dir, around it, inside any git checkout, through a link or
junction, and when it is a folder with other things in it.

Layout: `OUT/snapshot/` (the copy as taken: `mind.db` migrated to this code's schema,
`mind.toml`, `personas/`, `prompts/`, `realm/`), `OUT/mind-data/` (the sandbox mind's data dir,
rebuilt from the snapshot at each start and reset; its `logs/mind.log`, `control.json` and
`library.db` stay), `OUT/sandbox.json` (its URL and pid while it runs).

## Testing a change before release

The sandbox runs whatever code Python imports. To try a worktree's prompt change on the same
state as the live code:

```bash
cd F:/azerothcore/mind
.venv/Scripts/python -m leeroy_mind sandbox --data-dir "$D" --out "$OUT"     # live code
.venv/Scripts/python -m leeroy_mind sandbox-talk --url ... --guid G --script talk.txt --json before.json
# stop it, then the worktree's code on the same snapshot:
PYTHONPATH=F:/azerothcore/worktrees/<name>/mind .venv/Scripts/python -m leeroy_mind sandbox \
    --data-dir "$D" --out "$OUT" --reuse
.venv/Scripts/python -m leeroy_mind sandbox-talk --url ... --guid G --script talk.txt --json after.json
```

Check which code runs: the sandbox's `mind.log` has `mind_started` with `code=` (the
`leeroy_mind` folder it imported). A worktree whose migrations are newer than the snapshot's
migrates the restored copy at each start; the snapshot itself stays as taken. The model's
sampling is not seeded, so compare several runs rather than one.

## Not covered

- Voice rooms: with voice off, game lines are always typed, as when he is in no voice room.
- His raid: the bridge sends no boss, death or loot events, guild life does not run, and raid
  orders are refused at the bridge.
- Members' photos, voice notes, group DMs started by the night and anything else the night
  makes: the night never runs in the sandbox, so what it would have written is not there.
- Photo files are not copied: each start logs `selfie_sync_failed` once for every member with a
  profile photo (the start's photo repair finds no files; nothing else follows from it).
