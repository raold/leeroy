# The dojo

An isolated environment for training leeroy's combat policies with deep
reinforcement learning. It runs against its own AzerothCore worldserver,
databases and fixture characters, never the live realm, and steps the training
world on a virtual clock so it can run faster than real time.

This is the foundation increment: an isolated layout and its preflight check,
and the environment protocol with its lifecycle state machine, written once in
pure C++ (`modules/mod-dojo/src/Lib`, no AzerothCore includes) and once in
Python (`leeroy_dojo`). There is no learner, no Torch or Gymnasium dependency
and no native module build yet.

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
database or prints a credential.

```powershell
pwsh -NoProfile -File scripts/dojo/preflight.ps1 -Layout <layout.json>                    # check
pwsh -NoProfile -File scripts/dojo/preflight.ps1 -Layout <layout.json> -Json              # machine-readable
pwsh -NoProfile -File scripts/dojo/preflight.ps1 -Layout <layout.json> -WriteProvenance   # record baselines
```

It refuses a root inside the live install's core, build, configuration or
protected folders; a module set other than playerbots and dojo (plus listed
experiment modules); a junction that leaves the isolated checkouts; a missing
or mismatched marker; a database target that names a live schema or uses the
live database user; a production or busy port; a non-loopback dojo endpoint;
missing client data; and a core or playerbots checkout that does not descend
from the baseline commits in the provenance manifest. `-WriteProvenance`
records the branch, commit, remotes and SHA-256 fingerprints of every dirty
file for the live checkouts the dojo derives from and for the isolated
checkouts; later runtime commands require that manifest and the marker. Exit
status is 0 when nothing failed, 1 on a failed check and 2 for an unreadable
layout.

## Protocol

Version 1 is NDJSON on a loopback socket between one controller and one
worker. Each line is a JSON object of at most 256 KiB (UTF-8, without the
newline) with `"v": 1`, a `"type"` and a request `"id"` (1-64 characters of
`A-Za-z0-9._:-`). Unknown fields, more than 64 nested containers, NaN and
invalid UTF-8 are refused. Every request line gets exactly one response line:
`<type>_result` with `"ok": true`, or `"type": "error"` with a `code`.

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
  flight is answered when it completes; of a recent result, from the bounded
  cache, without touching the world. A different payload under a known id is
  `conflicting_duplicate`. A duplicate whose result has left the cache is
  validated afresh and fails as `wrong_step` or `stale_episode`.
- A request refused before it was applied leaves no trace; resending it
  after a fix is a new request.
- Each reset and abort starts a new generation. Completions and world
  callbacks carry the generation they began under and are ignored once it
  is old, so an abandoned operation cannot touch the next episode.
- Reconnecting never guesses which step was applied: the old session's
  episode is aborted.

| Error | Meaning |
|---|---|
| `malformed` | not JSON, not an object, a missing, unknown or ill-typed field |
| `line_too_long` | over the line bound; the connection is closed after it |
| `unsupported_version` | a protocol, schema or scenario the worker does not speak |
| `not_ready` | not allowed in the current state (no hello yet, booting, terminal, draining) |
| `bad_session` | not this connection's session |
| `busy` | another reset or step is in flight |
| `stale_episode` | not the current episode, or an episode id used in the last 64 resets |
| `wrong_step` | not the expected decision index |
| `conflicting_duplicate` | a known request id with a different payload |
| `invalid_action` | a missing, repeated or unknown actor, or an unknown or masked action |
| `aborted` | the episode was aborted; `reason` is `disconnect`, `closed`, `fault`, `reset_failed` or `invalid_result` |
| `result_overflow` | the result would exceed a bound; the episode is aborted, never truncated |

Bounds, published in `hello_result`: 256 KiB per line, 2 controlled actors,
64 events per step result, 8 cached results, 64 remembered episode ids and
one request in flight.

The worker adapter performs the state machine's effects: `begin_reset` and
`begin_step` (each with its generation), `abort`, `release` (the session
ended) and `close_connection`. Both implementations run the golden cases in
`modules/mod-dojo/tests/fixtures/dojo_protocol.jsonl`; its expectations are
written out in `make_dojo_protocol.py` without importing either one.

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

The pure C++ library builds without AzerothCore (Visual Studio 2022 or GCC):

```bash
cmake -S modules/mod-dojo/tests -B build-dojo-tests
cmake --build build-dojo-tests --config RelWithDebInfo --parallel 4
build-dojo-tests/RelWithDebInfo/dojo_tests.exe
```

The tests run without AzerothCore, MySQL, Torch or a running server. The
preflight tests build a synthetic install in a temporary directory and are
skipped when `pwsh` or `git` is missing. After changing a golden case, run
`python modules/mod-dojo/tests/fixtures/make_dojo_protocol.py`; a test
fails when the committed fixture differs from its generator.
