"""The packaged operator guide and the two local identities an operator can verify."""
import os
import re
import subprocess
import tomllib
from importlib import metadata, resources
from pathlib import Path


def checkout_commit(root: Path) -> str | None:
    # Explicit .git also supports linked worktrees and prevents walking up to a parent repo.
    if not (root / ".git").exists() or not (root / "mind/leeroy_mind/console/app.py").is_file():
        return None
    try:
        result = subprocess.run(
            ["git", "--git-dir=.git", "rev-parse", "--verify", "HEAD^{commit}"], cwd=root,
            env={k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")},
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=2,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    if result.returncode == 0 and re.fullmatch(rb"[0-9a-f]{40}(?:[0-9a-f]{24})?", value):
        return value.decode("ascii")
    return None


def guide_view(managed_root: Path) -> dict:
    package = resources.files(__package__)
    project = Path(__file__).resolve().parents[2]
    source_root = project.parent if project.name == "mind" and (project / "pyproject.toml").is_file() else None
    try:
        if source_root:
            with (project / "pyproject.toml").open("rb") as f:
                version = tomllib.load(f)["project"]["version"]
        else:
            version = metadata.version("leeroy-mind")
    except (OSError, ValueError, KeyError, metadata.PackageNotFoundError):
        version = None
    source = checkout_commit(source_root) if source_root else None
    managed = source if source_root == managed_root.resolve() else checkout_commit(managed_root)
    return {"guide": package.joinpath("README.md").read_text("utf-8"), "version": version,
            "source_commit": source, "checkout_commit": managed}
