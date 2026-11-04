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
        self.assertRegex(runtime, r"(?m)^void AddSC_dojo_world\(\)\s*\{\s*new DojoWorldScript\(\);\s*"
                                  r"AddSC_dojo_commands\(\);\s*\}")
        commands = (MODULE / "src" / "Runtime" / "DojoCommands.cpp").read_text(encoding="utf-8")
        disabled = commands.split("#else", 1)[1]
        self.assertRegex(disabled, r"void AddSC_dojo_commands\(\)\s*\{\s*\}")   # the disabled variant registers nothing

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

    def test_the_runtime_opens_no_endpoint_and_starts_no_thread(self):
        """Until the step driver arrives nothing in the runtime listens, connects or runs a thread of its own."""
        for path in sources("src", "Runtime"):
            text = path.read_text(encoding="utf-8")
            for forbidden in ("Transport", "Lifecycle", "asio", "std::thread", "std::async", "tcp::", "Socket"):
                self.assertNotIn(forbidden, text, path.name)

    def test_the_disabled_variant_touches_no_bot_player_or_arena(self):
        """Every reference to playerbots, players, battlegrounds and commands sits inside #if DOJO_TRAINING_BUILD,
        so the variant every ordinary core gets compiles none of it. (Headers only declare.)"""
        for path in [p for p in sources("src", "Runtime") if p.suffix == ".cpp"]:
            outside = disabled_text(path.read_text(encoding="utf-8"))
            for forbidden in ("Playerbot", "Player*", "Player const*", "Battleground", "ChatCommand", "AddPlayerBot",
                              "TeleportTo", "CharacterCreateInfo"):
                self.assertNotIn(forbidden, outside, path.name)

    def test_only_the_director_logs_bots_in_and_only_the_manifests_fixtures(self):
        fixtures = (MODULE / "src" / "Runtime" / "DojoFixtures.cpp").read_text(encoding="utf-8")
        for path in sources("src", "Runtime"):
            if path.name != "DojoFixtures.cpp":
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("AddPlayerBot", text, path.name)
                self.assertNotIn("LogoutPlayerBot", text, path.name)
        self.assertEqual(re.findall(r"AddPlayerBot\(([^;]+)\);", fixtures), ["ObjectGuid(_actors[i].guid), 0"])
        self.assertIn("is not on the fixture account; refusing to touch another account's character", fixtures)

    def test_the_arena_factory_is_unrated_and_never_queued(self):
        fixtures = (MODULE / "src" / "Runtime" / "DojoFixtures.cpp").read_text(encoding="utf-8")
        self.assertIn("CreateNewBattleground(type, bracket, uint8(m.arena.arenaType), false)", fixtures)
        self.assertIn("sBattlegroundMgr->AddBattleground(bg);", fixtures)
        for forbidden in ("StartBattleground", "AddToBGFreeSlotQueue", "SetRated(true)", "sArenaTeamMgr", "SetArenaTeamIdForTeam"):
            self.assertNotIn(forbidden, fixtures)
        commands = (MODULE / "src" / "Runtime" / "DojoCommands.cpp").read_text(encoding="utf-8")
        self.assertIn("return !Dojo::Director::Instance().SuppressesWinCheck(bg);", commands)
        self.assertIn("return bg && _arenaInstance && bg->GetInstanceID() == _arenaInstance;", fixtures)

    def test_commands_are_console_only(self):
        commands = (MODULE / "src" / "Runtime" / "DojoCommands.cpp").read_text(encoding="utf-8")
        entries = re.findall(r'\{"(\w+)", (Handle\w+), (\w+), Console::(\w+)\}', commands)
        self.assertEqual(len(entries), 19)
        for name, _, security, console in entries:
            self.assertEqual((security, console), ("SEC_CONSOLE", "Yes"), name)


def function_body(text: str, signature: str) -> str:
    """The braces-balanced body of the first definition that starts with signature."""
    start = text.index(signature)
    open_at = text.index("{", start)
    depth = 0
    for i in range(open_at, len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return text[open_at:i + 1]
    raise AssertionError(f"unbalanced {signature}")


class ControlTest(unittest.TestCase):
    """Plan Task 6: the external controller and its executor, checked statically."""

    def setUp(self):
        self.executor = (MODULE / "src" / "Runtime" / "DojoExecutor.cpp").read_text(encoding="utf-8")

    def test_only_the_controller_acquires_or_releases(self):
        for path in sources("src"):
            text = path.read_text(encoding="utf-8")
            if path.suffix == ".cpp" and path.name != "DojoExecutor.cpp":
                for call in ("AcquireExternalControl", "ReleaseExternalControl", "ClearStrategies", "SetNextCheckDelay"):
                    self.assertNotIn(call, text, path.name)
        self.assertIn('constexpr char const* kOwner = "mod-dojo";', self.executor)
        self.assertIn("ai->AcquireExternalControl(kOwner)", self.executor)
        # Acquisition needs a registered slot, and the director registers only resolved fixture characters.
        fixtures = (MODULE / "src" / "Runtime" / "DojoFixtures.cpp").read_text(encoding="utf-8")
        self.assertEqual(fixtures.count("Control::Instance().Register("), 1)
        resolve = function_body(fixtures, "bool Director::Resolve(")
        self.assertLess(resolve.index("is not on the fixture account"), resolve.index("Control::Instance().Register("))

    def test_the_executor_requests_what_a_client_would(self):
        """D22: costs, cooldowns, range, line of sight, facing and the global cooldown stay the core's, so the executor
        goes through the client opcode handlers and never casts, faces or toggles pets through playerbots helpers."""
        for handler in ("HandleCastSpellOpcode", "HandleCancelCastOpcode", "HandleSetSelectionOpcode",
                        "HandleAttackSwingOpcode", "HandleAttackStopOpcode"):
            self.assertIn(f"GetSession()->{handler}(packet)", self.executor)
        for forbidden in ("->CastSpell(", "CastCustomSpell", "->prepare(", "new Spell(", "botAI->", "ToggleAutocast",
                          "GetCharmInfo", "SetInFront(", "ServerFacade"):
            self.assertNotIn(forbidden, self.executor)

    def test_knowledge_only_reads(self):
        """Mask queries must not change target, resources, procs or cooldowns: the knowledge read calls nothing that
        writes, and the modifier lookups pass no spell (so no charge is registered or dropped)."""
        body = function_body(self.executor, "ActorKnowledge Control::Knowledge(")
        for forbidden in ("Set", "Remove", "Add", "Modify", "Interrupt", "Cast", "Handle", "Stop", "Attack(", "Clear",
                          "Launch", "Teleport", "Record(", "_queue", "_slots", "const_cast"):
            self.assertNotIn(forbidden, body.replace("CalcPowerCost", "").replace("castQueued", "")
                             .replace("castRemainingMs", "").replace("GetCastTimeRemaining", ""), forbidden)
        self.assertIn("self->m_spellModTakingSpell == nullptr", body)
        self.assertIn("CalcPowerCost(self, info->GetSchoolMask(), nullptr)", body)
        self.assertIn("GetMaxRange(false, self, nullptr)", body)

    def test_the_catalog_is_the_manifests_kit(self):
        prepare = function_body(self.executor, "std::string Control::Prepare(")
        self.assertIn("for (KitSpell const& k : manifest.loadout.kit)", prepare)
        self.assertIn("BuildCatalog(kit, error)", prepare)

    def test_movement_is_refused_while_rooted_or_without_control(self):
        move = function_body(self.executor, "Control::Part Control::Move(")
        for state in ('"dead"', '"stunned"', '"rooted"', '"without control"'):
            self.assertIn(state, move)
        self.assertIn("MovePositionToFirstCollision", move)
        self.assertIn("IsBackward(movement) ? MOVE_RUN_BACK : MOVE_RUN", move)


class PlayerbotsSeriesTest(unittest.TestCase):
    """The dojo's playerbots patch series (scripts/dojo/patches.ps1 verifies the isolated checkout against it)."""

    SERIES = REPO / "patches" / "dojo" / "mod-playerbots"

    def patches(self) -> list[Path]:
        return sorted(self.SERIES.glob("*.patch"))

    def test_the_series_is_ours_and_narrow(self):
        patches = self.patches()
        self.assertTrue(patches)
        self.assertFalse((REPO / "patches" / "mod-playerbots").joinpath("dojo").exists())
        for patch in patches:
            text = patch.read_text(encoding="utf-8")
            with self.subTest(patch=patch.name):
                self.assertIn("From: raold <868138+raold@users.noreply.github.com>", text)
                self.assertNotIn("Co-Authored-By", text)
                self.assertNotIn("Co-authored-by", text)
                touched = set(re.findall(r"(?m)^diff --git a/(\S+) b/", text))
                self.assertLessEqual(touched, {"src/Bot/PlayerbotAI.cpp", "src/Bot/PlayerbotAI.h"})

    def test_the_gate_skips_everything_after_the_fall_information(self):
        text = "".join(p.read_text(encoding="utf-8") for p in self.patches())
        gate = ("+    if (IsExternallyControlled())\n+    {\n+        ++_externalControlSkippedUpdates;\n"
                "+        return;\n+    }\n")
        self.assertIn(gate, text)
        # The hunk that adds it sits in UpdateAI between the fall information and the cheat refill; patches.ps1
        # checks the whole order (cheats, UpdateAIReaction, UpdateAIInternal) in the isolated checkout itself.
        hunk = text[text.rindex("\n@@ -", 0, text.index(gate)) + 1:]
        hunk = hunk[:hunk.index("\n@@ ")]
        self.assertIn("void PlayerbotAI::UpdateAI(uint32 elapsed, bool minimal)", hunk.splitlines()[0])
        self.assertLess(hunk.index("bot->SetFallInformation(0, bot->GetPositionZ());"), hunk.index(gate))
        self.assertLess(hunk.index(gate), hunk.index("// Handle cheat options"))
        for refused in ("void PlayerbotAI::HandleCommand(", "bool PlayerbotAI::DoSpecificAction(",
                        "bool PlayerbotAI::CastSpell(uint32 spellId, Unit* target", "void PlayerbotAI::RequestSpellInterrupt()",
                        "void PlayerbotAI::QueueChatResponse("):
            self.assertIn(refused, text)
        self.assertIn("Reset(true);", text)


def disabled_text(text: str) -> str:
    """The source as the disabled variant compiles it: every #if DOJO_TRAINING_BUILD region (up to its #else or
    #endif) removed, nested conditionals followed."""
    out, stack = [], []   # stack of (is_training_region, in_else)
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#if"):
            stack.append([s == "#if DOJO_TRAINING_BUILD", False])
            continue
        if s.startswith("#else") and stack:
            stack[-1][1] = True
            continue
        if s.startswith("#endif") and stack:
            stack.pop()
            continue
        if any(training and not in_else for training, in_else in stack):
            continue
        out.append(line)
    return "\n".join(out)


if __name__ == "__main__":
    unittest.main()
