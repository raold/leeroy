# The dojo

An isolated environment for training leeroy's combat policies with deep
reinforcement learning. It runs against its own AzerothCore worldserver,
databases and fixture characters, never the live realm, and steps the training
world on a virtual clock so it can run faster than real time.

This is the foundation increment: an isolated layout and its preflight check;
the environment protocol with its lifecycle state machine and loopback
transport, written once in pure C++ (`modules/mod-dojo/src/Lib`, no
AzerothCore includes) and once in Python (`leeroy_dojo`); a deterministic fake
worker; and a checksummed episode recorder. There is no learner, no Torch or
Gymnasium dependency and no native module build yet: `mod-dojo` has no
registration or loader, so it must not be linked into a core.

## Layout

The dojo keeps everything apart from the installed server. A layout file
names each root by absolute path (see
[`scripts/dojo/layout.example.json`](../scripts/dojo/layout.example.json)):

| Root | Holds |
|---|---|
| `source` | a worktree of this repository (`modules/mod-dojo`, `dojo/`) |
| `core` | a separate AzerothCore checkout, from the recorded baseline commit |
| `playerbots` | a separate mod-playerbots checkout, likewise |
| `build` | a fresh CMake build tree, never the installed `build/` |
| `runtime` | binaries, private configuration, ports, process receipts and logs |
| `data` | manifests, trajectories, checkpoints and evaluation output |

The core's `modules/` holds only `mod-playerbots` and `mod-dojo`, each a
junction into the isolated checkouts. The training realm has its own schemas
and database user; it may read the live world schema to provision itself but
never writes a live schema. `runtime/dojo.marker` marks a training instance:

```json
{"marker": "leeroy-dojo-training", "instance": "dojo-01"}
```

## Preflight

`scripts/dojo/preflight.ps1` (PowerShell 7.2+) checks a layout and prints one
`ok`/`FAIL`/`note` line per check. It never starts a server, connects to a
database or prints a credential; the only programs it runs are git (rev-parse,
remote, status, merge-base, with fsmonitor off) and the version queries of
cmake, Python and vswhere.

```powershell
pwsh -NoProfile -File scripts/dojo/preflight.ps1 -Layout <layout.json>                    # check
pwsh -NoProfile -File scripts/dojo/preflight.ps1 -Layout <layout.json> -Json              # machine-readable
pwsh -NoProfile -File scripts/dojo/preflight.ps1 -Layout <layout.json> -WriteProvenance   # record baselines
```

It refuses rather than passes whenever it cannot positively identify what it
protects: the layout's `production` paths must exist, the live core must be a
git checkout, the live `CMakeCache.txt` must name that core, and the live
`worldserver.conf` must yield readable database targets (a named pipe or an
unparseable value is refused). It then refuses a root that is, is inside or
contains the live install, its core, build, configuration, modules or
protected folders; a module set other than playerbots and dojo (plus listed
experiment modules); a junction or symlink that leaves the isolated checkouts,
including one nested anywhere inside the core or a module; a missing or
mismatched marker; a training database that is not on loopback, names a live
schema (this machine under any name: loopback, host name or interface address,
is the live server) or uses the live database user; a production or busy port,
whether the layout requests it or a training `worldserver.conf` or
`authserver.conf` would bind it (a missing key means AzerothCore's default);
a non-loopback dojo endpoint; missing client data; and a core or playerbots
checkout that does not descend from the baseline commits in the provenance
manifest. `-WriteProvenance` records the branch, commit, remotes and SHA-256
fingerprints of every dirty file for the live checkouts the dojo derives from
and for the isolated checkouts. It writes nothing until every live-install and
root check has passed, and then only inside the data root, never over a file
that is not a provenance manifest. Later runtime commands require that
manifest and the marker. Exit status is 0 when nothing failed, 1 on a failed
check and 2 for an unreadable layout.

## Protocol

Version 1 is NDJSON on a loopback socket between one controller and one
worker. Each line is a JSON object of at most 256 KiB (UTF-8, without the
newline) with `"v": 1`, a `"type"` and a request `"id"` (1-64 characters of
`A-Za-z0-9._:-`). Unknown fields, more than 64 nested containers, NaN, a byte
order mark, invalid UTF-8 and an integer that no double can hold are refused;
such a number makes the whole line not JSON, so its id is not echoed. A line
of only spaces, tabs and carriage returns is skipped; anything else, a lone
vertical tab or form feed included, is decoded. Every request line gets
exactly one response line: `<type>_result` with `"ok": true`, or
`"type": "error"` with a `code` and a free-text `message` of at most 256
bytes (an echoed field name or worker reason is clipped).

| Request | Fields | Result |
|---|---|---|
| `hello` | `protocol {min,max}`, optional `client`, `expect {observation_schema, action_schema, scenario}` | `session`, `protocol`, `state`, `bounds`, `worker`, `scenario`, `schemas`, `actors`, `actions`, `clock`, `rng` |
| `reset` | `session`, `episode` (new), `decision` (0), `scenario_seed`, optional `scenario` | `episode`, `decision`, `generation`, `scenario`, `scenario_seed`, `sim_ms`, `observations` |
| `step` | `session`, `episode`, `decision`, `actions`: one `{actor, action}` per controlled actor | `decision`, `next_decision`, `generation`, `requested` (with `action_id`), `elapsed_ms`, `substeps`, `sim_ms`, `observations`, `events`, `rewards`, `terminated`, `truncated`, `outcome` |
| `status` | optional `session` | `state`, `session`, `episode`, `decision`, `generation`, `in_flight`, `draining`, `abort_reason` |
| `close` | `session`, optional `reason` | `session`; the worker then closes the connection |

The connection's session comes from `hello`, and reset, step and close must
carry it. The worker reports its scenario, observation and action schemas,
the actor slots and teams, the action catalog (`actions[0]` is always
`noop`), the clock (`substep_ms`, `decision_ms`, paced or not) and what its
RNG can promise (`scenario_seed`, `combat_replay`) separately. `observations`
is one `{actor, obs, mask}` per actor in slot order: `obs` is that actor's
permitted numbers and `mask` one 0 or 1 per catalog action, with noop always
open. A step must name a command, an explicit noop included, for every actor;
commands for one decision share the observation they answer. Each command's
`action_id` is `decision * actors + actor`, and every execution event that
results from it, possibly in a later step, carries that id. `rewards` is one
`{actor, terminal, shaping}` per actor. `outcome` is null while the episode
runs, else `{reason, winner}`: `death` (winner is a team),
`simultaneous_death` or `time_limit` (no winner) end it as `terminated`;
`external` is a known-valid cutoff and sets `truncated` instead.

Lifecycle: `booting -> ready -> resetting -> awaiting_action -> stepping ->
awaiting_action | terminal`, with `aborted` and `closing` exits. A reset is
accepted in `ready`, `awaiting_action`, `terminal` and `aborted`, never while
an abandoned operation is still draining. A step is accepted only in
`awaiting_action`. Disconnecting, closing, a worker fault, a failed reset or
an invalid or oversized result aborts the episode: the worker is told to tear
it down, and it is never delivered as a lost game. The next session starts a
fresh episode with a reset.

Exactly-once rules:

- One reset or step is in flight at a time; another gets `busy`.
- An exact duplicate (same id, same canonical payload) of the request in
  flight is answered when it completes, up to `max_copies` answers in all
  (the original included; the transport's inbox capacity, 16); a copy beyond
  that gets `busy` at once. A duplicate of a recent result is answered from
  the bounded cache, without touching the world. A different payload under a
  known id is `conflicting_duplicate`. A duplicate whose result has left the
  cache is validated afresh and fails as `wrong_step` or `stale_episode`.
- A request refused before it was applied leaves no trace; resending it
  after a fix is a new request.
- Each reset and abort starts a new generation. Completions and world
  callbacks carry the generation they began under and are ignored once it
  is old, so an abandoned operation cannot touch the next episode.
- Each `begin_reset` and `begin_step` effect carries a `token`, unique for the
  worker's lifetime, and a completion names the generation and token of the
  operation it finishes. Anything else is stale, so a late or repeated
  completion of decision N can never answer decision N+1, and only the
  abandoned operation's own completion ends a drain.
- Reconnecting never guesses which step was applied: the old session's
  episode is aborted.

| Error | Meaning |
|---|---|
| `malformed` | not JSON, not an object, a missing, unknown or ill-typed field |
| `line_too_long` | over the line bound; the connection is closed after it |
| `unsupported_version` | a protocol, schema or scenario the worker does not speak |
| `not_ready` | not allowed in the current state (no hello yet, booting, terminal, draining) |
| `bad_session` | not this connection's session |
| `busy` | another reset or step is in flight, or `max_copies` copies of this one already are |
| `stale_episode` | not the current episode, or an episode id used in the last 64 resets |
| `wrong_step` | not the expected decision index |
| `conflicting_duplicate` | a known request id with a different payload |
| `invalid_action` | a missing, repeated or unknown actor, or an unknown or masked action |
| `aborted` | the episode was aborted; `reason` is `disconnect`, `closed`, `fault`, `reset_failed` or `invalid_result` |
| `result_overflow` | the result would exceed a bound; the episode is aborted, never truncated |

Bounds, published in `hello_result`: 256 KiB per line, 2 controlled actors,
64 events per step result, 8 cached results, 64 remembered episode ids, one
request in flight and 16 answers owed to it (`max_copies`).

The worker adapter performs the state machine's effects: `begin_reset` and
`begin_step` (each with its generation and token), `abort`, `release` (the
session ended) and `close_connection`. Worker payloads are typed as strictly
as requests: an actor slot, mask bit or millisecond count is an integer,
never a bool or a float, and a result that cannot be encoded aborts the
episode. Both implementations run the golden cases in
`modules/mod-dojo/tests/fixtures/dojo_protocol.jsonl` (worker descriptions,
line framing, decoding and whole sessions); its expectations are written out
in `make_dojo_protocol.py` without importing either one.

## Transport

`Dojo::Transport` (C++, header-only Boost.Asio) and `leeroy_dojo.transport`
(Python, for the fake worker) are the worker's side of the socket. They bind
loopback only, port 0 for an ephemeral port that the worker records in its
process receipt. One controller at a time: another connection gets a `busy`
error line and is closed. The io thread only frames lines and decodes them
into pure data; the worker thread polls connect, line and disconnect events
and sends responses. Nothing is dropped to relieve pressure: while the worker
has not taken 16 lines, or 64 responses wait unwritten, reading stops and TCP
pushes back on the controller. Each connection has a serial, and a response
for a connection that has gone is dropped, since its session has ended.

## Fake worker, recorder and smoke

`leeroy_dojo.fake_worker` runs the transport, the lifecycle machine and
`fake_game.FakeDuel`, a deterministic two-actor duel that is not WoW but has
the contract's shape: 10 ms substeps in 200 ms decisions, a 150 ms command
delay, one pending command per actor (a new one supersedes it, noop keeps
it), a bolt whose damage lands after its cast with the action id that started
it, an instant lance, a global cooldown, rejections with a reason,
simultaneous death and time limits, the +1/-1/0 terminal reward with
optional potential shaping, a worker-lifetime clock that a reset does not
rewind, and seeded combat that replays exactly. Its records of what the world
did keep at most 100 000 entries each. `leeroy_dojo.client` is a blocking
controller that resends an identical line after a timeout (never more copies
than the worker's `max_copies`), discards the extra answers it is owed, and
raises `UnexpectedAnswer` for any answer it never asked for.

`leeroy_dojo.recording` writes an episode as `header.json` (scenario, seeds,
worker and schema versions, clock and RNG capabilities, bounds, policies),
chunks of up to 64 decision records each closed by a trailer with its
SHA-256 and a hash chain that starts from the header's SHA-256, and
`footer.json` with the outcome, the header's hash and every chunk's hash,
written last. A chunk is renamed into place only when complete. The episode's
directory is named after its id, which must be one safe path component (no
leading or trailing dot, no colon, no Windows device name). `verify_episode`
names a missing, unfinished, altered or foreign chunk, a replaced header, a
missing footer, a terminal record that is not the last, and a last record
whose flags do not match the footer's `terminated` or `truncated` outcome
(an aborted episode may end anywhere); `load_transitions` refuses an episode
that is incomplete or aborted.

```bash
.venv/Scripts/python -m leeroy_dojo protocol-smoke --fake [--out DIR] [--json]
.venv/Scripts/python -m leeroy_dojo verify-episode DIR/episodes/smoke-a
```

The smoke plays three recorded episodes against an in-process fake worker:
one with a step delivered twice and a result replayed from the cache, one cut
by dropping the connection right after a step was sent, and one on a new
session that must replay the first exactly. It checks that every decision
advanced the world once, that the old session is refused, that the cut
episode is aborted and excluded, and that a deleted final chunk and a crash
before the footer are both caught. It writes only under its output directory
(a new temporary directory by default) and exits 0 when every check passes.

## Development

The package is standard library only and targets Python 3.12, which current
PyTorch wheels support and which is already installed here for the voice
service; its environment is kept apart from the mind's and the voice
service's.

```bash
uv venv --python 3.12 dojo/.venv
uv pip install --python dojo/.venv/Scripts/python.exe -e "dojo[test]"
cd dojo && .venv/Scripts/python -m pytest -q
```

The pure C++ library builds without AzerothCore (Visual Studio 2022 or GCC);
the transport needs Boost's headers:

```bash
cmake -S modules/mod-dojo/tests -B build-dojo-tests -DDOJO_BOOST_INCLUDE_DIR=F:/azerothcore/boost_1_92_0
cmake --build build-dojo-tests --config RelWithDebInfo --parallel 4
build-dojo-tests/RelWithDebInfo/dojo_tests.exe
```

CI builds the same tests with GCC and `-DDOJO_WARNINGS_AS_ERRORS=ON`
(`-Wall -Wextra -Werror`; `/W4 /WX` with MSVC).

The tests run without AzerothCore, MySQL, Torch or a running server. The
preflight tests build a synthetic install in a temporary directory and are
skipped when `pwsh` or `git` is missing; one of them runs the preflight with
only logging shims on `PATH` and listeners on every database and game port,
and another checks that nothing outside the data root changes. They link
directories with junctions on Windows and symlinks elsewhere, so CI (Linux)
covers the symlink paths only. After changing a golden case, run
`python modules/mod-dojo/tests/fixtures/make_dojo_protocol.py`; a test
fails when the committed fixture differs from its generator.
