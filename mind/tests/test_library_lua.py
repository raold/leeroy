import pytest

from leeroy_mind.library.lua import LuaSyntaxError, LuaTable, Name, assignments, parse_value, tokenize


def test_strings_escapes_and_long_brackets():
    toks = tokenize(r'''"a\"b\n\65\x42" 'it''s' [[raw
text]] [==[with ]] inside]==]''')
    assert [v for k, v in toks if k == "string"] == ['a"b\nAB', "it", "s", "raw\ntext",
                                                     "with ]] inside"]


def test_comments_are_skipped():
    toks = tokenize('x -- line comment\n--[[ block\n comment ]] y --[==[ x ]==] z')
    assert [v for _, v in toks] == ["x", "y", "z"]


def test_numbers_and_ops():
    assert [v for _, v in tokenize("0x1F 12 3.5 1e3 .. ... ~=")] == [31, 12, 3.5, 1000.0, "..",
                                                                     "...", "~="]


def test_table_array_hash_and_nesting():
    t = parse_value('{ 1, "two", three = 3, ["four"] = { 4; 5 }, -6, true, nil }')
    assert isinstance(t, LuaTable)
    assert t.array == [1, "two", -6, True, None]
    assert t.hash["three"] == 3 and t.hash["four"].array == [4, 5]
    assert t.get(2) == "two" and t.get("three") == 3


def test_locale_lookups_concat_and_calls():
    assert parse_value('AL["Normal Mode"]') == "Normal Mode"
    assert parse_value('BabbleBoss["Ick"].." / "..BabbleBoss["Krick"]') == "Ick / Krick"
    assert parse_value('"Level "..80') == "Level 80"
    assert parse_value("GetSpellInfo(2018)") is None
    assert parse_value("1 + 2") is None
    assert parse_value("SomeGlobal") == Name("SomeGlobal")
    assert parse_value("function(a) if a then return 1 end end") is None


def test_assignments_skip_code_and_report_errors():
    src = '''
local x = 5
if x then print("hi") end
Data["A"] = { 1, 2 };
function f() Data["Fake"] = { 9 } end
Data.B = "bee"
Other["C"] = 1
Data["Bad"] = { 1, 2
'''
    found, errors = assignments(src, {"Data"})
    assert found["Data"]["A"].array == [1, 2] and found["Data"]["B"] == "bee"
    assert "C" not in found["Data"]
    assert errors and "Bad" in errors[0]


def test_unfinished_string_raises():
    with pytest.raises(LuaSyntaxError):
        tokenize('"no end\n"')
