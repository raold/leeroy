# Notes for agents

This repo is the customization layer of a single-player AzerothCore (WotLK 3.3.5a) server built
on the mod-playerbots fork: native modules (`modules/`), local patch series for upstream modules
(`patches/`), the mind service (`mind/`, Python 3.14), the voice service and the selfie worker
(`voice/`, `selfie/`, Python 3.12), the dojo (`dojo/`), configs (`config/`) and maintenance
scripts (`scripts/`). The [README](README.md) describes each part; the package READMEs hold the
detail.

CI builds and tests on Linux with clang. The server runs on Windows with MSVC, so a few failures
appear only at a native release. `scripts/verify/run.sh [BASE]` runs, on the server's machine,
only the checks a branch's diff needs (MSVC syntax check of changed mod-leeroy files with the live
build's settings, the patch series against the live module checkouts, migrations on a copy of
the live database, and more; see its header). The `verify` workflow runs its portable parts on
every pull request.

## Review guidelines

Flag these as P1 unless noted. Each comes from a bug that passed CI and failed in the live
install.

### Native code

- **Windows macros.** Never name an identifier `near`, `far`, `small`, `IN`, `OUT`, `OPTIONAL`,
  `ERROR`, `DELETE` or `interface` anywhere under `modules/*/src` (fields, locals, parameters,
  enumerators, functions). The live build is MSVC with `<windows.h>` reachable from the game
  headers, whose `minwindef.h` and `rpcndr.h` define them; clang on Linux and the unit tests never
  see those macros, so CI passes and the release build fails (a `bool near` field in a
  `src/Lib` header broke one). `src/Lib` code counts too: its headers are included by game
  translation units.
- **Module folder names.** AzerothCore includes `modules/<dir>/<dir>.cmake` (a differently named
  file is skipped silently, with its compile flags) and calls `Add<dir, dashes as
  underscores>Scripts()`. A module's folder, its `.cmake` and its loader function must agree
  exactly; a `-master` or `-main` suffix from a GitHub zip breaks both.
- **Upstream API.** Module code tracks the mod-playerbots fork of the core and the
  mod-playerbots module, not upstream AzerothCore. Playerbots config keys are `Playerbots.*`
  with no fallback for the old `AiPlayerbot.*` prefix: an unmigrated key silently runs at its
  default.
- **Pin guard.** In `RandomPlayerbotMgr::ProcessBot` the `IsPinned(botId)` guard must come
  before the `bot->isDead()` block and before "leave group if leader is rndbot", or a raid led by
  a pinned bot dissolves. Any playerbots patch that touches that function must keep the order.

### The bridge's wire contract

- mod-leeroy and the mind speak NDJSON over loopback, one mind at a time. A command id the mind
  keeps on the wire must pass `bridge.valid_preserved_id` (1-128 ASCII letters, digits, `_`,
  `-`); flag an id built from anything else, such as a `:`-joined key. Every DKP hand-out was
  once lost this way: the send raised, and the receipt was recorded as uncertain.
- A change to a message kind, field, error code or receipt changes both sides together
  (`mind/leeroy_mind/protocol.py`, `bridge.py` and `modules/mod-leeroy/src/Lib/LeeroyProtocol.*`)
  and keeps an older peer working or refusing cleanly through the hello handshake.
- A command whose outcome is unknown stays unknown and is reconciled. It is never resent as if it
  had not been sent, and never recorded as not taken: the exactly-once ledgers rely on it.
- **Fakes as strict as production.** Flag a fake, stub or fixture (`FakeBridge`, guild ports, fake
  clients) that accepts an id, field, size or state the production side refuses, and a test that
  passes only because its fake is looser than production.

### Privacy

- Logs, metrics, health output, console API errors and CI output carry ids, counts, codes and
  timings, never text: no chat or whisper text, DMs, prompts, persona text, memories or photo
  prompts. Flag an f-string or `%s` that logs such a value, a logged exception whose message can
  quote stored text, and a `repr()` of a row.
- A member's whisper and her DM with the player are one private conversation; officer chat and
  officer-only channels are one officer audience. Flag code that carries content from one
  audience into another (a whisper into guild chat, Discord or a memory others can recall).
- Built-in prompts in the repo stay non-explicit. The selfie worker renders adults only, clothed
  and non-explicit, never a real person's likeness, and no person's name reaches a prompt.

### Secrets

- Nothing secret is ever committed: database credentials, tokens, API keys, passwords, console
  keys. The history is public and permanent.
- `config/` and the `patches/` series are snapshots written by `scripts/ac-snapshot.sh`, which
  masks every value whose key ends in `DatabaseInfo`, `Secret`, `Token`, `ApiKey` or `Password`.
  Flag hand edits there, and a new secret-bearing key the mask would miss.

### Releases, migrations and patch series

- `scripts/ac-release.sh` puts merged `main` commits on the live branch. A mind release may touch
  only `mind/`, `selfie/`, `voice/` and a few top-level files; `--native` adds `modules/`,
  `config/` and `patches/`. `scripts/` is not part of either: the release refuses it without
  `--allow-scripts`. Flag a PR whose runtime change silently depends on a script change shipping
  with it, and say how the script change gets installed.
- Mind migrations (`mind/leeroy_mind/storage/migrations/NNNN_*.sql`) are numbered without holes
  against the merge target: two open PRs must not both take the next number, and a merged
  migration is never edited (live databases have applied it). A migration runs in one
  transaction with its data step, which must not commit. The mind refuses to open a database
  whose numbering skips a migration.
- A new `mind.toml` key or persona field that older code reads as invalid can turn a whole
  feature off on rollback; the PR should say so.
- Patch series apply in filename order, each entry on top of the ones before it: the native
  release `git am`s the entries a module checkout lacks, and the nightly `patches` workflow
  applies the whole series to upstream. New work is a new entry with the next number. An entry
  already released is never renumbered or rewritten (installed entries are recognized by commit
  ancestry or stable patch id; a changed one is a new patch). The dojo's series
  (`patches/dojo/mod-playerbots`) applies on top of the local playerbots series.

### Checks that judge a pull request

- Never let a PR weaken the check that judges it. A change to `.github/workflows/`,
  `scripts/tests/`, `scripts/verify/`, test fixtures, `conftest.py` or a test's expected values
  must not loosen a gate: removing or skipping a step or test, adding `continue-on-error`,
  `xfail` or `skip`, widening a tolerance, deleting an assertion, narrowing a path filter or
  lowering a threshold. A failing behaviour is fixed in the code, not in the expectation.
- A workflow change belongs in its own PR, not together with the code it tests (P2). The
  `verify` workflow warns when a PR mixes them.
- No self-hosted runners and no deploy jobs: the repo is public, and either would let a pull
  request run code on the server's machine.

### Housekeeping

- Local notes (`CLAUDE.md`, `docs/`, `private/`) are git-ignored on purpose; flag a PR that adds
  them or un-ignores them.
- Commits carry no `Co-Authored-By` or other attribution trailers.

<!-- review-outcomes:start -->
## Checks learned from released misses

- Before releasing native changes, compile the affected translation units with the production Windows/MSVC headers; portable policy tests alone do not cover Windows macros. Check: production-header MSVC syntax/build check, including new identifiers. Evidence: [msvc-near-390](https://github.com/raold/leeroy/pull/390).
- For raid AI changes, require an isolated running-world scenario that observes the intended effect with competing stock strategies enabled. An action returning success or a policy test is not proof of healing, aura acquisition, or safe movement. Check: dojo raid acceptance receipt for the candidate native build (pending runtime gate). Evidence: [valithria-439](https://github.com/raold/leeroy/pull/439).
<!-- review-outcomes:end -->
