"""Synthetic source/SQL fixtures; no checkout, database, compiler or server needed."""

import copy
import json

import pytest

from leeroy_mind.library.command_export import export_commands


def cpp(entries, *, tables="", script="sample_commands", tail="", body=None):
    body = body if body is not None else f"""
        {tables}
        static ChatCommandTable commandTable = {{ {entries} }};
        return commandTable;
    """
    return f"""
class {script} : public CommandScript
{{
public:
    {script}() : CommandScript("{script}") {{}}
    ChatCommandTable GetCommands() const override
    {{ {body} }}
}};
void AddSC_sample() {{ new {script}(); }}
{tail}
"""


def exported(source, **kwargs):
    return export_commands({"src/commands.cpp": source}, **kwargs)


def rows(result):
    return {row["name"]: row for row in result["records"]}


def reasons(result):
    return {gap["reason"] for gap in result["coverage"]["unresolved"]}


def test_nested_tables_default_handler_aliases_and_exact_locations():
    source = cpp('{"warp", destinations}', tables="""
        static ChatCommandTable destinations {
            {"add", HandleAdd, SEC_GAMEMASTER, Console::No},
            {"create", HandleAdd, SEC_GAMEMASTER, Console::No},
            {"", HandleWarp, rbac::RBAC_PERM_COMMAND_WARP, Console::Yes}
        };
    """)
    result = exported(source)
    commands = rows(result)
    assert list(commands) == ["warp", "warp add", "warp create"]
    assert commands["warp"]["handler"] == "HandleWarp"
    assert commands["warp"]["metadata"]["default_handler"] is True
    assert commands["warp"]["allow_console"] is True
    assert commands["warp"]["permission"] == {"kind": "rbac", "value": "rbac::RBAC_PERM_COMMAND_WARP"}
    add = commands["warp add"]
    assert add["permission"] == {"kind": "security", "value": 2}
    assert add["metadata"]["same_handler_names"] == ["warp add", "warp create"]
    assert add["source_line"] == next(i for i, line in enumerate(source.splitlines(), 1)
                                    if '{"add"' in line)
    assert add["source_path"] == "src/commands.cpp"
    assert result["expected_records"] == result["coverage"]["registered"] == 3
    assert result["coverage"]["unresolved"] == []
    assert result["coverage"]["verification"] == "source-observed"
    assert all(row["verification"] == "source-observed" for row in commands.values())


def test_namespace_is_not_executable_and_multiword_names_create_intermediate_nodes():
    result = exported(cpp('{"guild raid start", HandleStart, SEC_PLAYER, Console::No}'))
    commands = rows(result)
    assert list(commands) == ["guild", "guild raid", "guild raid start"]
    for name in ("guild", "guild raid"):
        node = commands[name]
        assert node["handler"] == node["syntax"] == node["help"] == ""
        assert node["permission"] == {"kind": "unknown", "value": None}
        assert node["allow_console"] is None
        assert node["metadata"]["namespace"] is True
        assert node["metadata"]["executable"] is False
    assert commands["guild raid start"]["metadata"]["executable"] is True
    assert "guild raid sta" not in commands  # Runtime abbreviation is not an alias registration.


def test_two_tables_can_share_subtree_and_reference_aliases():
    result = exported(cpp('{"warp", alias}, {"travel", alias}', tables="""
        static ChatCommandTable leaves = {{"", HandleWarp, 0, Console::Yes}};
        static ChatCommandTable const& alias = leaves;
    """))
    assert set(rows(result)) == {"warp", "travel"}
    assert all(row["handler"] == "HandleWarp" for row in result["records"])
    assert result["coverage"]["unresolved"] == []


@pytest.mark.parametrize("declaration", ["static ChatCommandTable root", "static std::vector<ChatCommand> root",
                                         "static std::vector<ChatCommandBuilder> root",
                                         "static Acore::ChatCommands::ChatCommandTable root"])
def test_legacy_vector_and_optional_equals(declaration):
    source = cpp("", body=f'{declaration} {{ {{"ping", HandlePing, SEC_PLAYER, Console::Yes}} }}; return root;')
    assert rows(exported(source))["ping"]["handler"] == "HandlePing"


def test_direct_return_and_nested_template_handler():
    source = cpp("", body='return {{"ping", HandlePing<First, Second>, 200, Console::Yes}};')
    command = rows(exported(source))["ping"]
    assert command["handler"] == "HandlePing<First,Second>"
    assert command["permission"] == {"kind": "rbac", "value": 200}


def test_comments_literals_braces_and_handler_code_do_not_create_commands():
    source = cpp('/* {"fake", Fake, 0, Console::Yes} */ {"pi" "ng", HandlePing, 0, Console::Yes}', tail=r'''
// class pretend : public CommandScript { GetCommands() { return bad; } };
char const* text = R"tag(class pretend : public CommandScript { GetCommands() {} };)tag";
bool HandlePing(int value) { return value < 3; }
''')
    result = exported(source)
    assert list(rows(result)) == ["ping"]
    assert result["coverage"]["parsed_definitions"] == 1
    assert reasons(result) == set()


def test_cpp_string_escape_and_raw_literal_names():
    source = cpp(r'{"p\x69ng", HandlePing, 0, Console::Yes}, {R"tag(raid status)tag", HandleStatus, 0, Console::Yes}')
    assert set(rows(exported(source))) == {"ping", "raid", "raid status"}


def test_case_insensitive_namespace_merging_keeps_source_spellings():
    result = exported(cpp('{"Guild go", HandleGo, 0, Console::Yes}, {"guild stop", HandleStop, 0, Console::Yes}'))
    commands = rows(result)
    assert len(commands) == 3
    assert set(d["spelling"] for d in commands["Guild"]["metadata"]["declarations"]) == {"Guild", "guild"}


def test_sql_permission_override_and_legacy_help_fill():
    source = cpp('{"ping", HandlePing, rbac::RBAC_PERM_COMMAND_PING, Console::Yes}')
    result = exported(source, command_overrides=[
        {"name": "PING", "security": 3, "help": "Syntax: .ping [value]\nCheck a value."}])
    row = rows(result)["ping"]
    assert row["permission"] == {"kind": "security", "value": 3}
    assert row["syntax"] == ".ping [value]"
    assert row["help"] == "Syntax: .ping [value]\nCheck a value."
    assert row["metadata"]["source_permission"] == {"kind": "rbac", "value": "rbac::RBAC_PERM_COMMAND_PING"}
    assert row["metadata"]["help_source"] == "command"
    assert row["metadata"]["command_overrides"][0]["permission_applied"] is True


def test_acore_string_help_wins_over_legacy_sql_help():
    source = cpp('{"ping", HandlePing, LANG_HELP_PING, rbac::RBAC_PERM_COMMAND_PING, Console::No}')
    result = exported(source, symbols={"LANG_HELP_PING": 721, "rbac::RBAC_PERM_COMMAND_PING": 202},
                      acore_strings={721: "Syntax: .ping <token>\nExact localized text."},
                      command_overrides=[{"name": "ping", "security": 255, "help": "wrong legacy text"}])
    row = rows(result)["ping"]
    assert row["help"] == "Syntax: .ping <token>\nExact localized text."
    assert row["syntax"] == ".ping <token>"
    assert row["permission"] == {"kind": "rbac", "value": 255}
    assert row["metadata"]["help_id"] == 721
    assert row["metadata"]["command_overrides"][0]["help_applied"] is False


@pytest.mark.parametrize("symbols", [{}, {"LANG_HELP_PING": 721}])
def test_missing_acore_help_never_falls_back_to_sql_or_invented_syntax(symbols):
    result = exported(cpp('{"ping", HandlePing, LANG_HELP_PING, SEC_PLAYER, Console::Yes}'),
                      symbols=symbols, command_overrides=[
                          {"name": "ping", "security": 0, "help": "Syntax: .ping fabricated"}])
    row = rows(result)["ping"]
    assert row["help"] == row["syntax"] == ""
    assert row["metadata"]["help_source"] == "acore_string"
    assert "unresolved_help" in reasons(result)


@pytest.mark.parametrize("zero", ["0", "AcoreStrings()", "AcoreStrings{}", "LANG_ZERO"])
def test_zero_help_key_allows_sql_help(zero):
    result = exported(cpp(f'{{"ping", HandlePing, {zero}, SEC_PLAYER, Console::Yes}}'),
                      symbols={"LANG_ZERO": 0}, command_overrides=[
                          {"name": "ping", "security": 1, "help": "Plain help without syntax."}])
    assert rows(result)["ping"]["help"] == "Plain help without syntax."
    assert rows(result)["ping"]["syntax"] == ""


def test_symbol_key_localization_and_unicode_text_are_preserved():
    result = exported(cpp('{"ping", HandlePing, LANG_HELP_PING, 0, Console::Yes}'),
                      acore_strings={"LANG_HELP_PING": "Syntax: .ping café\n精確な説明"})
    assert rows(result)["ping"]["syntax"] == ".ping café"
    assert rows(result)["ping"]["help"].endswith("精確な説明")


def test_duplicate_sql_keeps_first_help_and_last_permission_like_core():
    result = exported(cpp('{"ping", HandlePing, 0, Console::Yes}'), command_overrides=[
        {"name": "ping", "security": 1, "help": "first"},
        {"name": "ping", "security": 2, "help": "second"}])
    assert rows(result)["ping"]["help"] == "first"
    assert rows(result)["ping"]["permission"]["value"] == 2
    assert "duplicate_override" in reasons(result)


def test_namespace_override_does_not_make_an_executable_command():
    result = exported(cpp('{"raid go", HandleGo, 0, Console::Yes}'), command_overrides=[
        {"name": "raid", "security": 3, "help": "Namespace legacy help"},
        {"name": "uninstalled", "security": 0, "help": "Not a source registration"}])
    node = rows(result)["raid"]
    assert node["handler"] == ""
    assert node["permission"] == {"kind": "unknown", "value": None}
    assert node["metadata"]["command_overrides"][0]["permission_applied"] is False
    assert result["coverage"]["unmatched_overrides"] == [{"name": "uninstalled", "security": 0, "row": 1}]
    assert "uninstalled" not in rows(result)


def test_stale_sql_syntax_is_preserved_and_labeled_not_silently_corrected():
    result = exported(cpp('{"warp name npc id", HandleWarpNpc, 0, Console::Yes}'), command_overrides=[
        {"name": "warp name npc id", "security": 2, "help": "Syntax: .warp name id #player #creature\nHelp."}])
    node = rows(result)["warp name npc id"]
    assert node["syntax"] == ".warp name id #player #creature"
    assert node["help"] == "Syntax: .warp name id #player #creature\nHelp."
    assert node["metadata"]["syntax_source"] == "command"
    assert node["metadata"]["syntax_name_matches"] is False
    assert result["coverage"]["diagnostics"] == [{
        "source_path": node["source_path"], "source_line": node["source_line"],
        "reason": "syntax_name_mismatch", "detail": "warp name npc id: .warp name id #player #creature"}]
    assert result["coverage"]["unresolved"] == []


def test_matching_acore_syntax_has_observed_source_and_unknown_syntax_stays_empty():
    result = exported(cpp('{"ping", HandlePing, LANG_PING, 0, Console::Yes}, {"pong", HandlePong, 0, Console::Yes}'),
                      acore_strings={"LANG_PING": "Syntax: .PING <token>"})
    assert rows(result)["ping"]["metadata"]["syntax_source"] == "acore_string"
    assert rows(result)["ping"]["metadata"]["syntax_name_matches"] is True
    assert rows(result)["pong"]["metadata"]["syntax_source"] == "unknown"
    assert rows(result)["pong"]["syntax"] == ""
    assert result["coverage"]["diagnostics"] == []


def test_namespace_legacy_syntax_is_not_mistaken_for_an_invoker():
    result = exported(cpp('{"staff on", HandleOn, 0, Console::Yes}'), command_overrides=[
        {"name": "staff", "security": 1, "help": "Syntax: .staff [on/off]"}])
    row = rows(result)["staff"]
    assert row["handler"] == ""
    assert row["metadata"]["executable"] is False
    assert row["metadata"]["syntax_source"] == "command"
    assert "syntax_name_matches" not in row["metadata"]


def test_framework_calls_and_abstract_interface_are_accounted_for_separately():
    source = '''
class CommandScript : public ScriptObject {
    virtual std::vector<Acore::ChatCommands::ChatCommandBuilder> GetCommands() const = 0;
};
void Collect() { auto rows = script->GetCommands(); auto other = script.GetCommands(); }
'''
    result = exported(source)
    assert result["records"] == []
    assert result["coverage"]["unresolved"] == []
    assert [row["reason"] for row in result["coverage"]["non_definitions"]] == [
        "abstract_command_script_interface", "method_invocation", "method_invocation"]
    assert result["coverage"]["sources"][0]["definitions"] == 0


def test_unsupported_macro_entry_is_reported_without_fabricating_commands():
    source = cpp('SOME_COMMAND_MACRO(), {"ping", HandlePing, 0, Console::Yes}')
    result = exported(source)
    assert set(rows(result)) == {"ping"}
    assert "unknown_entry" in reasons(result)


def test_preprocessor_branch_is_not_guessed_but_unconditional_siblings_survive():
    source = cpp('''
#ifdef ENABLE_SECRET
        {"conditional", HandleConditional, 0, Console::Yes},
#else
        {"alternative", HandleAlternative, 0, Console::Yes},
#endif
        {"ping", HandlePing, 0, Console::Yes}
''')
    result = exported(source)
    assert set(rows(result)) == {"ping"}
    assert {"conditional_registration", "conditional_entry"} <= reasons(result)


def test_entire_conditional_command_definition_is_unresolved():
    result = exported("#ifdef ENABLE_COMMANDS\n" + cpp('{"ping", HandlePing, 0, Console::Yes}') + "\n#endif\n")
    assert rows(result) == {}
    assert "conditional_definition" in reasons(result)


@pytest.mark.parametrize("dynamic", ["root.push_back(MakeCommand());", "if (Enabled()) return other;",
                                     "ADD_COMMANDS(root);"])
def test_dynamic_getcommands_body_is_not_treated_as_final_registration(dynamic):
    body = f'static ChatCommandTable root = {{{{"ping", HandlePing, 0, Console::Yes}}}}; {dynamic} return root;'
    result = exported(cpp("", body=body))
    assert result["records"] == []
    assert "dynamic_registration" in reasons(result)


def test_unknown_table_cycle_and_top_level_default_are_explicit():
    result = exported(cpp('{"unknown", missing}, {"cycle", circular}, {"", HandleBlank, 0, Console::Yes}',
                          tables='static ChatCommandTable circular = {{"again", circular}};'))
    assert {"unknown_table", "table_cycle", "invalid_top_level_default"} <= reasons(result)
    assert "" not in rows(result)
    assert all(not row["metadata"]["executable"] for row in result["records"])


def test_duplicate_handler_is_ambiguous_not_arbitrarily_first_or_last():
    result = exported(cpp('{"ping", HandleFirst, 0, Console::Yes}, {"PING", HandleSecond, 2, Console::No}'))
    assert len(result["records"]) == 1
    node = result["records"][0]
    assert node["handler"] == ""
    assert node["metadata"]["ambiguous_handler"] is True
    assert node["metadata"]["executable"] is False
    assert "duplicate_handler" in reasons(result)


def test_unknown_permissions_and_console_remain_unknown():
    result = exported(cpp('{"ping", HandlePing, CALCULATE_PERMISSION(), CONSOLE_SETTING}'))
    row = rows(result)["ping"]
    assert row["permission"] == {"kind": "unknown", "value": None}
    assert row["metadata"]["permission_expression"] == "CALCULATE_PERMISSION()"
    assert row["allow_console"] is None
    assert {"unknown_permission", "unknown_console"} <= reasons(result)


def test_source_inventory_and_definition_only_are_not_runtime_verification():
    source = cpp('{"ping", HandlePing, 0, Console::Yes}').replace("new sample_commands();", "")
    result = export_commands({"src/empty.cpp": "// no commands\n", "src/commands.cpp": source})
    assert result["complete"] is True
    assert "registration_unverified" in reasons(result)
    assert result["coverage"]["unresolved_count"] == len(result["coverage"]["unresolved"])
    assert [(s["source_path"], s["definitions"]) for s in result["coverage"]["sources"]] == [
        ("src/commands.cpp", 1), ("src/empty.cpp", 0)]
    assert all(len(s["sha256"]) == 64 for s in result["coverage"]["sources"])
    assert rows(result)["ping"]["verification"] == "source-observed"


def test_bad_lexical_source_is_reported_and_other_source_survives():
    result = export_commands({"broken.cpp": "/* never closed", "good.cpp": cpp('{"ping", HandlePing, 0, Console::Yes}')})
    assert "lexical_error" in reasons(result)
    assert set(rows(result)) == {"ping"}
    assert len(result["coverage"]["sources"]) == 2


def test_handler_macro_from_another_supplied_file_stays_unresolved():
    source = cpp('{"ping", HANDLER, 0, Console::Yes}')
    result = export_commands({"commands.cpp": source, "macros.h": "# define HANDLER HandleDifferent\n"})
    row = rows(result)["ping"]
    assert row["handler"] == ""
    assert row["metadata"]["executable"] is False
    assert row["metadata"]["namespace"] is False
    assert "unknown_handler" in reasons(result)


def test_conditional_table_alias_is_not_guessed():
    source = cpp('{"ping", alias}', tables='''
        static ChatCommandTable leaf = {{"", HandlePing, 0, Console::Yes}};
#ifdef USE_LEAF
        static ChatCommandTable const& alias = leaf;
#endif
    ''')
    result = exported(source)
    assert rows(result)["ping"]["handler"] == ""
    assert {"conditional_table", "unknown_table"} <= reasons(result)


def test_malformed_return_is_a_coverage_gap_not_an_export_abort():
    source = cpp("", body='return {{"ping", HandlePing<Bad, 0, Console::Yes}};')
    result = exported(source)
    assert rows(result) == {}
    assert "malformed_registration" in reasons(result)


def test_multiple_scripts_with_same_local_table_names_stay_separate():
    source = cpp('{"ping", HandlePing, 0, Console::Yes}', script="first_commands")
    source += cpp('{"pong", HandlePong, 1, Console::No}', script="second_commands")
    result = exported(source)
    assert set(rows(result)) == {"ping", "pong"}
    assert result["coverage"]["parsed_definitions"] == 2
    assert result["coverage"]["unresolved"] == []


def test_external_definition_with_header_and_separate_instantiation():
    sources = {
        "commands.h": '''#ifndef SAMPLE_COMMAND_H
#define SAMPLE_COMMAND_H
class SampleCommand : public CommandScript {
public:
    Acore::ChatCommands::ChatCommandTable GetCommands() const override;
};
#endif
''',
        "commands.cpp": '''#include "commands.h"
ChatCommandTable SampleCommand::GetCommands() const {
    static ChatCommandTable root = {{"ping", HandlePing, SEC_PLAYER, Console::Yes}};
    return root;
}
''',
        "loader.cpp": 'void AddSampleScripts() { new SampleCommand(); }',
    }
    result = export_commands(sources)
    assert rows(result)["ping"]["source_path"] == "commands.cpp"
    assert result["coverage"]["parsed_definitions"] == 1
    assert result["coverage"]["unresolved"] == []


def test_header_guard_does_not_resolve_nested_feature_condition():
    source = '#ifndef COMMANDS_H\n#define COMMANDS_H\n#ifdef FEATURE\n'
    source += cpp('{"ping", HandlePing, 0, Console::Yes}') + '\n#endif\n#endif\n'
    result = exported(source)
    assert result["records"] == []
    assert "conditional_definition" in reasons(result)


def test_nonstandard_guard_with_outer_else_remains_unresolved():
    source = '#ifndef COMMANDS_H\n#define COMMANDS_H\n'
    source += cpp('{"ping", HandlePing, 0, Console::Yes}')
    source += '\n#else\n' + cpp('{"pong", HandlePong, 0, Console::Yes}', script="other_commands") + '\n#endif\n'
    result = exported(source)
    assert result["records"] == []
    assert "conditional_definition" in reasons(result)


def test_external_definition_without_supplied_declaration_is_explicitly_unknown():
    source = 'ChatCommandTable Missing::GetCommands() const { return {{"ping", HandlePing, 0, Console::Yes}}; }'
    result = exported(source)
    assert result["records"] == []
    assert "unrecognized_definition" in reasons(result)


def test_export_is_deterministic_and_does_not_mutate_inputs():
    sources = {"z.cpp": cpp('{"pong", HandlePong, 0, Console::Yes}', script="pong_commands"),
               "a.cpp": cpp('{"ping", HandlePing, 0, Console::Yes}', script="ping_commands")}
    overrides = [{"name": "ping", "security": 1, "help": "Syntax: .ping"}]
    before = copy.deepcopy((sources, overrides))
    first = export_commands(sources, command_overrides=overrides)
    second = export_commands(dict(reversed(list(sources.items()))), command_overrides=overrides)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert (sources, overrides) == before


@pytest.mark.parametrize("path", ["../source.cpp", "/source.cpp", "C:/source.cpp", "a/./x.cpp", "a//x.cpp", ""])
def test_provenance_path_rejects_absolute_or_ambiguous_paths(path):
    with pytest.raises(ValueError, match="relative"):
        export_commands({path: ""})


@pytest.mark.parametrize("bad", [True, -1, 256, "3", None])
def test_invalid_sql_permission_does_not_silently_coerce(bad):
    with pytest.raises(ValueError, match="uint8"):
        exported(cpp('{"ping", HandlePing, 0, Console::Yes}'),
                 command_overrides=[{"name": "ping", "security": bad, "help": ""}])


def test_invalid_symbol_boolean_is_rejected():
    with pytest.raises(ValueError, match="integer"):
        export_commands({}, symbols={"LANG_PING": True})
