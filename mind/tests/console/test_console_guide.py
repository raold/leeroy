"""Local help resources, repo links, and checkout identity across installation layouts."""
import asyncio
from importlib import resources
from pathlib import Path
import re
import subprocess

from console.rig import http, make_app
from leeroy_mind.console.guide import checkout_commit, guide_view


def test_guide_covers_each_tab_and_local_links():
    package = resources.files("leeroy_mind.console")
    root = Path(str(package)).resolve().parents[2]
    page = package.joinpath("page.html").read_text("utf-8")
    guide = package.joinpath("README.md").read_text("utf-8")
    tabs = re.findall(r'<button role="tab"[^>]+data-tab="([^"]+)"[^>]*>([^<]+)</button>', page)
    headings = re.findall(r"^## (.+)$", guide, re.M)
    assert tabs and all(name == label.lower() and headings.count(label) == 1 for name, label in tabs)
    for name in ("README.md", "RELEASE.md"):
        doc = package.joinpath(name).read_text("utf-8")
        links = re.findall(r"\[[^\]]+\]\(([^)]+)\)", doc)
        assert links
        for link in links:
            path, _, fragment = link.partition("#")
            target = (Path(str(package)) / path).resolve()
            assert target.is_relative_to(root) and target.is_file(), link
            current = root
            for part in target.relative_to(root).parts:
                assert part in {entry.name for entry in current.iterdir()}, link
                current /= part
            if fragment:
                titles = re.findall(r"^#{1,6} (.+)$", target.read_text("utf-8"), re.M)
                assert fragment in {re.sub(r"[^\w -]", "", h.lower()).replace(" ", "-") for h in titles}, link


def test_about_reads_exact_local_worktree_without_remotes(tmp_path):
    repo = tmp_path / "checkout"
    linked = tmp_path / "linked"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

    git("init", "-q")
    marker = repo / "mind/leeroy_mind/console/app.py"
    marker.parent.mkdir(parents=True)
    marker.touch()
    git("add", "mind")
    git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgSign=false",
        "commit", "-qm", "Fixture")
    sha = git("rev-parse", "HEAD")
    git("worktree", "add", "-q", "--detach", str(linked), sha)
    assert checkout_commit(repo) == checkout_commit(linked) == sha
    assert checkout_commit(marker.parent) is None
    view = guide_view(linked)
    assert view["checkout_commit"] == sha and view["source_commit"] != sha
    assert view["version"] and view["guide"].startswith("# Guild console guide")


def test_about_without_git_or_checkout(tmp_path, monkeypatch):
    assert checkout_commit(tmp_path) is None
    monkeypatch.setenv("PATH", "")
    view = guide_view(tmp_path)
    assert view["source_commit"] is view["checkout_commit"] is None
    assert view["version"] and "## Comms" in view["guide"]


def test_guide_uses_read_gate_without_a_running_mind(tmp_path):
    async def run():
        app = make_app(tmp_path)
        await app.start()
        try:
            assert (await http(app, "GET", "/api/guide", key=False)).status == 403
            assert (await http(app, "GET", "/api/guide", origin="http://other.invalid")).status == 403
            response = await http(app, "GET", "/api/guide")
            assert response.status == 200 and set(response.json()) == {
                "guide", "version", "source_commit", "checkout_commit"}
        finally:
            await app.close()
    asyncio.run(run())
