"""modules/mod-dojo as the core build sees it: names, defaults, what the runtime may reach and the shared vendored
json.hpp. Static checks of the source tree; nothing is built or run."""

import hashlib
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MODULE = REPO / "modules" / "mod-dojo"
LEEROY = REPO / "modules" / "mod-leeroy"
PREFLIGHT = REPO / "scripts" / "dojo" / "preflight.ps1"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sources(*parts: str) -> list[Path]:
    root = MODULE.joinpath(*parts)
    return sorted(p for p in root.rglob("*") if p.suffix in (".cpp", ".h"))


def includes(path: Path) -> list[tuple[str, str]]:
    return re.findall(r'(?m)^\s*#\s*include\s*([<"])([^>"]+)[>"]', path.read_text(encoding="utf-8"))


class VendoredJsonTest(unittest.TestCase):
    def test_matches_mod_leeroy_byte_for_byte(self):
        """Both modules put deps/ on the core's one `modules` target and the first include directory wins, so the two
        copies of nlohmann/json.hpp must never diverge (mod-dojo.cmake also refuses to configure if they do)."""
        ours = MODULE / "deps" / "nlohmann" / "json.hpp"
        theirs = LEEROY / "deps" / "nlohmann" / "json.hpp"
        self.assertEqual(sha256(ours), sha256(theirs))
        cmake = (MODULE / "mod-dojo.cmake").read_text(encoding="utf-8")
        self.assertIn("modules/mod-leeroy/deps/nlohmann/json.hpp", cmake)
        self.assertIn("differs from mod-leeroy's copy", cmake)


class RegistrationTest(unittest.TestCase):
    def test_cmake_and_loader_follow_the_folder_name(self):
        """CLAUDE.md build trap 1: modules/<dir>/<dir>.cmake and Add<dir, dashes as underscores>Scripts()."""
        self.assertEqual(MODULE.name, "mod-dojo")
        self.assertTrue((MODULE / "mod-dojo.cmake").is_file())
        self.assertEqual(sorted(p.name for p in MODULE.glob("*.cmake")), ["mod-dojo.cmake"])
        self.assertTrue((MODULE / "CMakeLists.txt").is_file())
        defined = {name for path in sources("src")
                   for name in re.findall(r"(?m)^void\s+(Add\w+Scripts)\s*\(\s*\)\s*$\s*\{", path.read_text(encoding="utf-8"))}
        self.assertEqual(defined, {"Addmod_dojoScripts"})

    def test_the_loader_registers_the_world_script(self):
        loader = (MODULE / "src" / "mod_dojo_loader.cpp").read_text(encoding="utf-8")
        self.assertIn("AddSC_dojo_world();\n}", loader)
        runtime = (MODULE / "src" / "Runtime" / "DojoWorldScript.cpp").read_text(encoding="utf-8")
        self.assertRegex(runtime, r"(?m)^void AddSC_dojo_world\(\)\s*\{\s*new DojoWorldScript\(\);\s*\}")

    def test_tests_stay_out_of_the_core(self):
        """The core collects every source under <module>/src; the test runner and its main() live in tests/."""
        self.assertFalse(any(p.name == "TestMain.cpp" for p in sources("src")))
        for name in ("CMakeLists.txt", "mod-dojo.cmake"):
            text = (MODULE / name).read_text(encoding="utf-8")
            code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
            self.assertNotIn("add_subdirectory", code, name)
            self.assertNotIn("tests", code, name)


class DefaultsTest(unittest.TestCase):
    def conf(self) -> dict[str, str]:
        text = (MODULE / "conf" / "mod_dojo.conf.dist").read_text(encoding="utf-8")
        return dict(re.findall(r'(?m)^\s*([A-Za-z0-9_.]+)\s*=\s*(.*?)\s*$', text))

    def test_ships_off(self):
        self.assertEqual(self.conf()["Dojo.Enable"], "0")

    def test_the_conf_names_exactly_the_keys_the_runtime_reads(self):
        runtime = (MODULE / "src" / "Runtime" / "DojoWorldScript.cpp").read_text(encoding="utf-8")
        read = set(re.findall(r'GetOption<[^>]+>\("([^"]+)"', runtime))
        self.assertEqual(read, set(self.conf()))
        self.assertIn('GetOption<bool>("Dojo.Enable", false)', runtime)

    def test_the_training_switch_defaults_off(self):
        cmake = (MODULE / "mod-dojo.cmake").read_text(encoding="utf-8")
        self.assertRegex(cmake, r'option\(AC_ENABLE_DOJO "[^"]+" OFF\)')
        self.assertIn("DOJO_TRAINING_BUILD=${DOJO_TRAINING_BUILD}", cmake)
        runtime = (MODULE / "src" / "Runtime" / "DojoWorldScript.cpp").read_text(encoding="utf-8")
        self.assertIn("#ifndef DOJO_TRAINING_BUILD\n#error", runtime)

    def test_marker_and_instance_rules_match_the_preflight(self):
        settings = (MODULE / "src" / "Lib" / "DojoSettings.h").read_text(encoding="utf-8")
        marker = re.search(r'kTrainingMarker = "([^"]+)"', settings).group(1)
        preflight = PREFLIGHT.read_text(encoding="utf-8")
        self.assertEqual(marker, re.search(r"\$DojoMarker = '([^']+)'", preflight).group(1))
        self.assertIn("$DojoIdPattern = '^[A-Za-z0-9._:-]{1,64}$'", preflight)
        self.assertIn("kMaxInstanceBytes = 64", settings)


class BoundaryTest(unittest.TestCase):
    def test_the_library_includes_nothing_from_the_core(self):
        """src/Lib builds without AzerothCore (tests/CMakeLists.txt); it may include itself, the standard library,
        the vendored json and header-only Boost.Asio."""
        lib = {p.name for p in sources("src", "Lib")}
        for path in sources("src", "Lib"):
            for kind, name in includes(path):
                with self.subTest(file=path.name, include=name):
                    if kind == '"':
                        self.assertIn(name, lib)
                    else:
                        self.assertTrue("/" not in name or name.startswith(("nlohmann/", "boost/asio")), name)

    def test_the_runtime_exposes_configuration_and_status_only(self):
        """Task 4's acceptance: with the module off (and, in this build, in every state) nothing opens an endpoint or
        claims a bot. The runtime may include only the settings and the core's config, log and script headers. The
        step driver and the controller hook change this test when they arrive."""
        for path in sources("src", "Runtime"):
            self.assertEqual(sorted(name for _, name in includes(path)),
                             ["Config.h", "DojoSettings.h", "Log.h", "ScriptMgr.h", "string"], path.name)
            text = path.read_text(encoding="utf-8")
            for forbidden in ("Transport", "Lifecycle", "Playerbot", "asio", "std::thread", "Player*"):
                self.assertNotIn(forbidden, text, path.name)


if __name__ == "__main__":
    unittest.main()
