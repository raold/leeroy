# leeroy

A single-player World of Warcraft 3.3.5a (Wrath of the Lich King) server: one
human, a world full of [playerbots](https://github.com/mod-playerbots/mod-playerbots),
and raids filled with bots on demand. Bots talk through a local LLM
([mod-ollama-chat](https://github.com/DustinHendrickson/mod-ollama-chat)).

This repo is the customization layer on top of the
[mod-playerbots fork of AzerothCore](https://github.com/mod-playerbots/azerothcore-wotlk):
maintenance scripts, the tuned configs, and local patches. It contains no
Blizzard data and no core source.

The goal it is named for: a bot raid leader. A tank that knows the route,
calls the pulls, and runs the encounter, so a lone mage can follow it through
Icecrown the way you once followed a good raid lead. Not built yet.

## Layout

| Path | What |
|---|---|
| `scripts/` | Update, backup, build, start/stop, and snapshot scripts (Git Bash on Windows) |
| `config/` | Snapshots of the live server and module configs, secrets masked |
| `patches/mod-playerbots/` | Local playerbots commits, as `git format-patch` output |
| `CLAUDE.md` | Operating notes: paths, build traps, module quirks, what each patch does |

## Patches

- **Raid Browser listings**: idle randombots list themselves in the Raid
  Browser, so it is not empty.
- **`.playerbots bot add10|add25|add40`**: fills your group to a 10/25/40-man
  raid with a sensible tank/healer/damage mix at your level, with raid specs
  and gear.
- **Raid habits**: those bots follow in arrow formation, and damage dealers
  hold a few seconds into a pull so the tank gets threat.

Apply them to a mod-playerbots checkout with
`git am /path/to/leeroy/patches/mod-playerbots/*.patch`.

## Scripts

```bash
./ac-update.sh [--dry-run]   # pull core + git modules, rebasing local patches
./ac-backup.sh [label]       # mysqldump all four databases
./ac-build.sh                # preflight checks, configure, build
./ac-server.sh start|stop|status
./ac-snapshot.sh             # copy live configs and playerbots patches into this repo
```

They assume this repo is the install root, with the core checked out in
`azerothcore-wotlk/` and the build in `build/`. See `CLAUDE.md`.

## License

GPL-2.0, matching AzerothCore and mod-playerbots.
