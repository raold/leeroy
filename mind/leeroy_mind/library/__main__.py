"""python -m leeroy_mind.library: build, embed, search and inspect the guild's library.db (in the
mind data dir, never in the repo).

  build [--sources world,atlasloot,wiki,curated] [--no-embed] [--offline]
  embed [--batch N] [--limit N]
  search "query" [--kind K] [-k N] [--text-only]
  stats
  fetch [--cap N]     (wiki pages into the cache only)"""
import argparse
import asyncio
import json
import logging
import sys
import textwrap
from pathlib import Path

from ..clock import SystemClock
from ..config import default_data_dir

SOURCES = ("world", "atlasloot", "wiki", "curated")


def _root(args) -> Path:
    return Path(args.data_dir) if args.data_dir else default_data_dir()


def _cache(root: Path) -> Path:
    return root / "library-cache" / "wiki"


def _library(root: Path, llm=None):
    from .core import Library
    cfg = _config(root)
    return Library(root / "library.db", clock=SystemClock(), llm=llm,
                   embed_model=cfg.models.embedding)


def _config(root: Path):
    from ..config import load_config
    return load_config(root / "mind.toml")


def _ollama(root: Path):
    from ..llm.client import OllamaClient
    return OllamaClient(_config(root).ollama.url, timeout=120.0)


def make_sources(args, root: Path) -> list:
    names = [s.strip() for s in args.sources.split(",") if s.strip()]
    bad = [s for s in names if s not in SOURCES]
    if bad:
        raise SystemExit(f"unknown sources {bad}; choose from {', '.join(SOURCES)}")
    out = []
    for name in names:
        if name == "world":
            from .world import WorldSource
            out.append(WorldSource(conf=Path(args.conf), mysql=Path(args.mysql)))
        elif name == "atlasloot":
            from .atlasloot import AtlasLootSource
            out.append(AtlasLootSource(Path(args.addons)))
        elif name == "curated":
            from .curated import CuratedSource
            out.append(CuratedSource())
        else:
            from .wiki import WikiClient, WikiSource
            client = WikiClient(_cache(root), offline=args.offline,
                                min_interval=max(1.0, args.interval))
            out.append(WikiSource(client, cap=args.cap, progress=print))
    return out


async def _embed(lib, llm, batch: int, limit: int | None) -> int:
    return await lib.embed_pending(llm, batch=batch, limit=limit, progress=print)


def cmd_build(args) -> int:
    from .lua import LuaSyntaxError
    from .mysql import QueryError
    from .wiki import WikiError
    from ..personas.roster import RosterError
    root = _root(args)
    lib = _library(root)
    try:
        try:
            reports = lib.build(make_sources(args, root), progress=print)
        except (QueryError, RosterError, WikiError, LuaSyntaxError, OSError) as e:
            print(f"build failed: {e.__class__.__name__}: {e}", file=sys.stderr)
            return 1
        log_path = root / "library-cache" / "build.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "w", encoding="utf-8") as fh:
            for rep in reports:
                fh.write(f"== {rep.source}: {rep.total} chunks\n")
                fh.writelines(line + "\n" for line in rep.log)
        for rep in reports:
            print(f"{rep.source}: {rep.total} chunks ({rep.inserted} new, {rep.updated} changed, "
                  f"{rep.unchanged} unchanged, {rep.deleted} removed; {len(rep.log)} log lines)")
        print(f"build log: {log_path}")
        if args.no_embed:
            print(f"not embedding: {lib.pending_embeddings()} chunks wait for "
                  "`python -m leeroy_mind.library embed`")
            return 0
        done = asyncio.run(_embed(lib, _ollama(root), args.batch, None))
        print(f"embedded {done}")
        return 0
    finally:
        lib.close()


def cmd_embed(args) -> int:
    from ..llm.client import LLMError
    root = _root(args)
    lib = _library(root)
    try:
        try:
            done = asyncio.run(_embed(lib, _ollama(root), args.batch, args.limit))
        except LLMError as e:
            print(f"embed stopped: {e} ({lib.pending_embeddings()} still pending)",
                  file=sys.stderr)
            return 1
        print(f"embedded {done}; {lib.pending_embeddings()} pending")
        return 0
    finally:
        lib.close()


def cmd_search(args) -> int:
    root = _root(args)
    lib = _library(root, llm=None if args.text_only else _ollama(root))
    try:
        kinds = args.kind or None
        hits = asyncio.run(lib.search(args.query, k=args.k, kinds=kinds))
        if not hits:
            print("no matches")
            return 1
        for c in hits:
            print(f"[{c.kind}] {c.heading()}  ({c.source}, {c.score:.2f})")
            print(textwrap.fill(c.text, width=100, initial_indent="    ",
                                subsequent_indent="    ", max_lines=8))
            if c.source == "wiki":
                print(f"    source: {c.cite()}")
        return 0
    finally:
        lib.close()


def cmd_stats(args) -> int:
    lib = _library(_root(args))
    try:
        print(json.dumps(lib.stats(), indent=2, ensure_ascii=False))
        return 0
    finally:
        lib.close()


def cmd_fetch(args) -> int:
    from .wiki import WikiClient, WikiError, build_plan
    cache = _cache(_root(args))
    client = WikiClient(cache, min_interval=max(1.0, args.interval))
    try:
        plan, skipped = build_plan(client, cap=args.cap)
        print(f"plan: {len(plan)} pages, {len(skipped)} left out ({client.requests} requests)")
        pages = client.pages([e.title for e in plan], progress=print)
    except WikiError as e:
        print(f"fetch failed: {e}", file=sys.stderr)
        return 1
    missing = [e for e in plan if pages.get(e.title) is None]
    lines = skipped + [f"missing on the wiki: {e.title} (tier {e.tier}, {e.why})" for e in missing]
    (cache / "plan.json").write_text(json.dumps([e.__dict__ for e in plan], ensure_ascii=False,
                                                indent=0), encoding="utf-8")
    (cache / "skipped.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"pages: {len(plan) - len(missing)} cached, {len(missing)} missing; "
          f"{client.requests} requests; skipped log at {cache / 'skipped.log'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    from ..personas.roster import DEFAULT_CONF, DEFAULT_MYSQL
    from .atlasloot import DEFAULT_ADDONS
    from .chunks import KINDS
    parser = argparse.ArgumentParser(prog="leeroy_mind.library",
                                     description="the guild's WotLK reference library")
    parser.add_argument("--data-dir", default=None)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("build", help="load sources into library.db, then embed new chunks")
    p.add_argument("--sources", default=",".join(SOURCES), help="comma list of " + ", ".join(SOURCES))
    p.add_argument("--no-embed", action="store_true", help="leave embedding for later")
    p.add_argument("--offline", action="store_true", help="wiki: use only cached pages")
    p.add_argument("--cap", type=int, default=1500, help="wiki: most pages to read")
    p.add_argument("--interval", type=float, default=1.0,
                   help="wiki: seconds between requests (never below 1)")
    p.add_argument("--batch", type=int, default=32, help="texts per embed call")
    p.add_argument("--conf", default=str(DEFAULT_CONF), help="worldserver.conf (credentials)")
    p.add_argument("--mysql", default=str(DEFAULT_MYSQL))
    p.add_argument("--addons", default=str(DEFAULT_ADDONS), help="the client's Interface/AddOns")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("embed", help="embed chunks that have no vector yet")
    p.add_argument("--batch", type=int, default=32)
    p.add_argument("--limit", type=int, default=None)
    p.set_defaults(func=cmd_embed)

    p = sub.add_parser("search", help="search the library")
    p.add_argument("query")
    p.add_argument("--kind", action="append", choices=KINDS,
                   help="repeatable; give it whenever the question has a type")
    p.add_argument("-k", type=int, default=5)
    p.add_argument("--text-only", action="store_true", help="BM25 only, never call Ollama")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("stats", help="chunk counts and build times")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("fetch", help="fetch the planned wiki pages into the cache only")
    p.add_argument("--cap", type=int, default=1500)
    p.add_argument("--interval", type=float, default=1.0,
                   help="seconds between requests (never below 1)")
    p.set_defaults(func=cmd_fetch)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
