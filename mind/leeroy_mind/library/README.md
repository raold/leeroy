# Scholar reference library

The library combines game references with complete, pinned AzerothCore documentation
snapshots. Official wiki Markdown uses source `acwiki`, generated API HTML uses
`acdoxygen`, installed core/module Markdown uses `acmodules`, and installed
command/teleport exports use `serverref`. Wiki/module documents have kind
`documentation`; API, command and teleport records use `api`, `command` and `teleport`.

Acquisition is separate from importing. Keep downloaded documents, exports and
`library.db` outside Git. An import checks the entire declared file inventory and
hashes before replacing that source in one SQLite transaction. There is no page,
language or archive cutoff. Missing/corrupt files preserve the previous complete
source. Schema version 5 preserves existing IDs, references and embedding bytes,
including databases already imported at version 4;
deploy matching code and data together.

```sh
python -m leeroy_mind.library --data-dir /private/library-stage build \
  --sources acwiki,acdoxygen,acmodules,serverref --no-embed --snapshot-workers 8 \
  --acwiki-snapshot /private/snapshots/wiki \
  --acdoxygen-snapshot /private/snapshots/doxygen \
  --acmodules-snapshot /private/snapshots/modules \
  --serverref-snapshot /private/snapshots/serverref
python -m leeroy_mind.library --data-dir /private/library-stage search \
  'teleport coordinates for Dalaran' --kind teleport --text-only
python -m leeroy_mind.library --data-dir /private/library-stage search \
  '.go xyz' --kind command --text-only
```

The default is eight verification/parser workers; `--snapshot-workers` accepts 1–16.
At most twice that many per-file results are queued. Workers do not access SQLite.
Both verification passes retain fresh path containment and link checks. Text search
works immediately; `--no-embed` never starts a model. Coordinate strings and command
syntax remain intact in CLI output and scholar reference facts.

For large HTML corpora, `--snapshot-parse-processes 8` runs parsing in separate CPU
processes while threads verify/read files. The range is 0–16; default 0 retains
threaded parsing. Both work queues are bounded, results keep manifest order, and
spawned parsers receive only verified bytes and metadata. When calling this API
from a script with processes enabled, guard its entry point with
`if __name__ == '__main__':`.

## Snapshot contract

Each directory contains UTF-8 `manifest.json` with these fields:

```json
{
  "format": "ac-library-snapshot-v1",
  "source": "acwiki",
  "origin": "https://github.com/azerothcore/wiki",
  "revision": "a-pinned-commit-id",
  "retrieved_at": 1793500000,
  "license": "unknown",
  "license_evidence": [],
  "complete": true,
  "errors": [],
  "expected_files": 1,
  "inventory_sha256": "sha256-of-canonical-files-array",
  "files": [{
    "path": "docs/example.md",
    "sha256": "sha256-of-exact-file-bytes",
    "bytes": 123,
    "url": "https://www.azerothcore.org/wiki/example",
    "language": "en",
    "archived": false,
    "version": ""
  }]
}
```

The placeholder hashes above must be replaced with 64 lowercase hexadecimal digits.
Hash the `files` array using UTF-8 JSON with sorted object keys, `ensure_ascii=False`
and separators `(',', ':')`. Include every Markdown file for `acwiki`/`acmodules` or every HTML
file for `acdoxygen`, excluding `.git`. Each original URL, relative path, revision,
hash, language and archive marker is retained. A declared license requires hashed
`license_evidence` entries with `path`, `sha256` and `url`; use `unknown` when the
snapshot does not establish its license. Do not infer the generated API snapshot's
underlying core commit from its publication date.

For `acmodules`, acquire tracked Markdown from each installed repository into a
separate subdirectory, retain each repository's original URL and installed revision
in its file entries, and pin the aggregate manifest to its content inventory. Record
local modifications explicitly in `version`; an upstream commit URL alone does not
prove the installed text is unchanged. For sources without Git metadata, record that
the upstream revision is unknown and identify the installed files by their hashes.
Keep per-module license evidence; a mixed collection may use global `unknown` rather
than assigning one module's license to every file. These documents are installed
source evidence, not proof that every described feature is enabled at runtime.

`serverref` requires `commands.json` and `teleports.json`, and may inventory extra
source/export evidence. `command_export.export_commands(sources, command_overrides=(),
acore_strings={}, symbols={})` is a pure parser for supplied installed C++ text and
read-only database exports. It performs no file, network, database or command I/O.
Supply command definitions, relevant headers, registration files, numeric language/
permission symbols, and database overrides from the same installation. The resulting
`ac-command-export-v1` document distinguishes executable commands, namespaces,
unknown constructs, registration evidence and stale help/syntax diagnostics.
Source observation does not prove runtime command availability. SQL override rows
alone do not register commands.

`teleports.json` uses format `ac-teleport-export-v1`, `complete: true`, an exact
`expected_records` count and `records`. Each record supplies integer `id` and `map`,
`name`, and decimal **strings** for `position_x`, `position_y`, `position_z` and
`orientation`. Optional `map_name` must come from observed map data; do not infer
zone names or convert world coordinates to map percentages.

The import stores snapshot provenance, manifest/inventory hashes, coverage and
per-file import inventory in `source_snapshot` and `source_inventory`; exact
records also live in `server_command` and `game_tele`.
Documentation is reference data. Neither importing nor answering a reference
question executes a GM command or follows instructions found in source text.
