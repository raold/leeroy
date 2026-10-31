# leeroy

[![ci](https://github.com/raold/leeroy/actions/workflows/ci.yml/badge.svg)](https://github.com/raold/leeroy/actions/workflows/ci.yml)
[![patches](https://github.com/raold/leeroy/actions/workflows/patches.yml/badge.svg)](https://github.com/raold/leeroy/actions/workflows/patches.yml)
[![build](https://github.com/raold/leeroy/actions/workflows/build.yml/badge.svg)](https://github.com/raold/leeroy/actions/workflows/build.yml)

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
| `patches/mod-ollama-chat/` | Local mod-ollama-chat commits, same format |
| `modules/mod-leeroy/` | The guild module: a local bridge (NDJSON over `127.0.0.1:7878`) that lets an outside "mind" see the guild and speak for its members, with unit tests and a smoke test in `tools/` |
| `mind/` | The mind: a Python service (`python -m leeroy_mind run`) that gives the guild's members personas, memories and their voice in guild chat, through the bridge and a local LLM (Ollama) |
| `CLAUDE.md` | Operating notes: paths, build traps, module quirks, what each patch does |

## Patches

- **Raid Browser listings**: idle randombots list themselves in the Raid
  Browser, so it is not empty.
- **`.playerbots bot add5|add10|add25|add40`**: fills your group to a 5-man
  party or a 10/25/40-man raid with a sensible tank/healer/damage mix at your
  level, with proper specs and gear.
- **Raid habits**: those bots follow in arrow formation, and damage dealers
  hold a few seconds into a pull so the tank gets threat.
- **Pinned random bots** (playerbots): an API other modules use to keep chosen
  random bots permanent and online, never re-rolled, teleported or pulled out
  of their group. mod-leeroy pins the guild with it.
- **Speaker filter** (mod-ollama-chat): lets another module keep chosen bots
  out of the LLM chat, so the guild speaks only through its own mind.

Apply them to a checkout of each module with
`git am /path/to/leeroy/patches/<module>/*.patch`.

## Scripts

```bash
./ac-update.sh [--dry-run]   # pull core + git modules, rebasing local patches
./ac-backup.sh [label]       # mysqldump all four databases
./ac-build.sh                # preflight checks, configure, build
./ac-server.sh start|stop|status
./ac-snapshot.sh             # copy live configs and module patches into this repo
```

They assume this repo is the install root, with the core checked out in
`azerothcore-wotlk/` and the build in `build/`. See `CLAUDE.md`.

## CI

| Workflow | When | What |
|---|---|---|
| `ci` | every push | shellcheck on `scripts/`, gitleaks over the full history, mod-leeroy unit tests, the mind's pytest suite |
| `patches` | patch changes, nightly | applies the patches to the newest upstream mod-playerbots (plus its codestyle check) and mod-ollama-chat; a nightly failure opens a `patch-drift` issue |
| `build` | patch or module changes, weekly | compiles the fork core with the patched playerbots and mod-leeroy (clang, `-Werror`, ccache) |
| `release` | `v*` tag | GitHub release with the patches, modules, configs and scripts |

There is no deploy job: the server runs on one PC and is updated with the
scripts above. A self-hosted runner on a public repo would let any pull request
run code on that PC.

## Research

Where published work exists, the design leans on it. [`references.bib`](references.bib) lists every source (232 BibTeX entries grouped by topic); each entry's `annote` says what it shaped. Metadata was checked against DOI registries, arXiv, the ACL Anthology, OpenReview, PMLR and the projects' own pages.

**Agents and memory.** The memory stream, the recency-importance-relevance recall score and reflection follow Park et al. (2023). Evidence-linked, time-aware facts follow Rasmussen et al. (2025), Latimer et al. (2025) and Wan et al. (2026), and writes are validated where hallucinations enter (Chen et al., 2026). Consolidation runs in the background (Lin et al., 2025) on a two-speed model (McClelland et al., 1995; Kumaran et al., 2016; Mattar and Daw, 2018), and recall uses activation-style decay (Anderson and Schooler, 1991).

**Theory of mind and who-knows-what.** LLM theory of mind is strong on some tests and brittle on others (Street et al., 2026; Ullman, 2023; Shapira et al., 2024; Kim et al., 2023; Gu et al., 2026), so beliefs live in an explicit ledger and each prompt is filtered to what that member could perceive (Wilf et al., 2024; Sclar et al., 2023; Jung et al., 2024). Secret-keeping is probed (Mireshghallah et al., 2024).

**Emotion and mood.** Appraisal labels follow Ortony, Clore and Collins (1988). Mood is a point in pleasure-arousal-dominance space (Mehrabian, 1996) with a personality-set baseline and decay (Gebhard, 2005); contagion follows Hatfield et al. (1993) and Bosse et al. (2015), and mood-congruent recall follows Bower (1981).

**Relationships and social dynamics.** Closeness grows with reciprocal self-disclosure (Altman and Taylor, 1973; Aron et al., 1997; Collins and Miller, 1994), and members differ in how they handle closeness and absence (Bartholomew and Horowitz, 1991; Mikulincer and Shaver, 2003). Attraction responds to proximity (Festinger et al., 1950), reciprocity (Aronson and Linder, 1965), similarity (Montoya et al., 2008), playing hard to get (Walster et al., 1973) and arousal after intense shared moments (Dutton and Aron, 1974). Gossip and rumour mutate as they travel (Allport and Postman, 1947; Kreminski, 2023), conventions spread as naming games (Baronchelli et al., 2006; Centola et al., 2018), feuds follow balance theory (Heider, 1946; Cartwright and Harary, 1956), and guild-wide opinions follow bounded-confidence dynamics (Deffuant et al., 2000; Hegselmann and Krause, 2002).

**Motivation and persona.** Members have weighted play motives (Yee, 2006) and daily states sampled around stable traits (Fleeson, 2001). Personas are re-anchored in every prompt because they drift (Li et al., 2024; Lu et al., 2026) and are built from specific detail (Park et al., 2024; Jones and Bergen, 2025).

**Stories and game AI.** Events are over-generated and then sifted (Ryan, 2018; Kreminski et al., 2019, 2021, 2022); algorithms select and the LLM narrates (Mendez and Gervas, 2023). Social moves follow Comme il Faut and Versu (McCoy et al., 2014; Evans and Short, 2014), and per-member bandits use Thompson sampling (Russo et al., 2018).

**Evaluation.** Weekly probes borrow from LongMemEval (Wu et al., 2025), LoCoMo (Maharana et al., 2024), SOTOPIA (Zhou et al., 2024), PersonaGym (Samuel et al., 2025) and persona-consistency metrics (Abdulhai et al., 2025).

**Ruled out.** Activation steering and persona vectors (Turner et al., 2023; Rimsky et al., 2024; Chen et al., 2025; AxBench, Wu et al., 2025; Tan et al., 2024), per-persona adapters (Neeko, Yu et al., 2024), preference optimisation on a player's reactions (Rafailov et al., 2023; Ethayarajh et al., 2024; Williams et al., 2025), self-editing live memory (Packer et al., 2023) and a cognitive-dissonance mechanic (Vaidis et al., 2024).

**Voice.** Mood maps to speaking rate, intensity and pitch (Juslin and Laukka, 2003); multi-speaker timing follows Stivers et al. (2009) and Ekstedt and Skantze (2022). The baseline voice model builds on StyleTTS 2 (Li et al., 2023) and speech recognition on Whisper (Radford et al., 2023).

**Software and models.** AzerothCore, mod-playerbots, mod-ollama-chat, Ollama, llama.cpp, SQLite with sqlite-vec, Gemma 4, Qwen3 and Qwen3-Embedding, plus retrieval staples: MMR (Carbonell and Goldstein, 1998), reciprocal rank fusion (Cormack et al., 2009), BM25 (Robertson and Zaragoza, 2009) and Matryoshka embeddings (Kusupati et al., 2022). Candidate chat models were compared on the UGI leaderboard.

## License

GPL-2.0, matching AzerothCore and mod-playerbots.
