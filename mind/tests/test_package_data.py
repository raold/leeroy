"""Every file the package reads at run time ships with it: each non-Python file under
leeroy_mind/ (the Markdown notes aside) matches a [tool.setuptools.package-data] glob of a package
it sits in, so a non-editable install has it too (review of #369: judge_weights.json was missing)."""
import tomllib
from pathlib import Path, PurePosixPath

MIND = Path(__file__).resolve().parents[1]
PACKAGE = MIND / "leeroy_mind"


def data_files() -> list[PurePosixPath]:
    out = []
    for path in sorted(PACKAGE.rglob("*")):
        rel = path.relative_to(MIND)
        if (path.is_dir() or "__pycache__" in rel.parts
                or path.suffix in (".py", ".pyc", ".md")):
            continue
        out.append(PurePosixPath(rel.as_posix()))
    return out


def shipped(rel: PurePosixPath, globs: dict[str, list[str]]) -> bool:
    for package, patterns in globs.items():
        root = PurePosixPath(*package.split("."))
        if root not in rel.parents:
            continue
        inside = rel.relative_to(root)
        if any(inside.full_match(p) for p in patterns):
            return True
    return False


def test_every_runtime_data_file_is_package_data():
    doc = tomllib.loads((MIND / "pyproject.toml").read_bytes().decode("utf-8"))
    globs = doc["tool"]["setuptools"]["package-data"]
    found = data_files()
    assert PurePosixPath("leeroy_mind/lab/judge_weights.json") in found
    missing = [str(f) for f in found if not shipped(f, globs)]
    assert not missing, f"add to package-data in mind/pyproject.toml: {missing}"


def test_the_check_sees_a_file_outside_every_glob():
    globs = {"leeroy_mind.lab": ["battery/*.toml"]}
    assert shipped(PurePosixPath("leeroy_mind/lab/battery/flirt.toml"), globs)
    assert not shipped(PurePosixPath("leeroy_mind/lab/judge_weights.json"), globs)
    assert not shipped(PurePosixPath("leeroy_mind/lab/battery/deeper/x.toml"), globs)
    assert not shipped(PurePosixPath("leeroy_mind/talk/battery/flirt.toml"), globs)
