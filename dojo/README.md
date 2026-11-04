# The dojo

An isolated environment for training leeroy's combat policies with deep
reinforcement learning. It runs against its own AzerothCore worldserver,
databases and fixture characters, never the live realm, and steps the training
world on a virtual clock so it can run faster than real time.

This is the foundation increment: an isolated layout and its preflight check.
There is no learner, no Torch or Gymnasium dependency and no native module
build yet.

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

## Development

The package is standard library only and targets Python 3.12, the oldest
interpreter with current PyTorch wheels that is already installed here; its
environment is kept apart from the mind's and the voice service's.

```bash
uv venv --python 3.12 dojo/.venv
uv pip install --python dojo/.venv/Scripts/python.exe -e "dojo[test]"
cd dojo && .venv/Scripts/python -m pytest -q
```

The tests run without AzerothCore, MySQL, Torch or a running server. The
preflight tests build a synthetic install in a temporary directory and are
skipped when `pwsh` or `git` is missing.
