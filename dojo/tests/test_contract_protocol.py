import json
import unittest

from leeroy_dojo.protocol import (
    MAX_LINE_BYTES, MAX_TEXT_BYTES, Bounds, ErrorCode, canonical, clip, decode_request, encode, error, is_blank_line,
    line_bytes,
)


class DecodeTest(unittest.TestCase):
    def test_invalid_utf8_is_malformed(self):
        d = decode_request(b'{"v":1,"type":"status","id":"q","session":"\xff"}')
        self.assertIsNone(d.request)
        self.assertEqual(d.code, ErrorCode.MALFORMED)

    def test_bytes_and_text_agree(self):
        line = '{"v":1,"type":"hello","id":"h1","protocol":{"min":1,"max":1},"client":"héllo"}'
        self.assertEqual(decode_request(line).request, decode_request(line.encode("utf-8")).request)

    def test_line_limit_counts_utf8_bytes(self):
        line = '{"v":1,"type":"close","id":"c","session":"s","reason":"' + "é" * 6 + '"}'
        size = len(line.encode("utf-8"))
        self.assertEqual(size, len(line) + 6)
        self.assertEqual(decode_request(line, Bounds(max_line_bytes=size - 1)).code, ErrorCode.LINE_TOO_LONG)
        self.assertIsNotNone(decode_request(line, Bounds(max_line_bytes=size)).request)

    def test_default_bound_is_256_kib(self):
        self.assertEqual(MAX_LINE_BYTES, 262144)
        pad = "x" * (MAX_LINE_BYTES - len('{"v":1,"type":"close","id":"c","session":"s","reason":""}'))
        line = '{"v":1,"type":"close","id":"c","session":"s","reason":"' + pad + '"}'
        self.assertEqual(len(line), MAX_LINE_BYTES)
        self.assertEqual(decode_request(line).code, ErrorCode.MALFORMED)   # within the line bound, over the text one
        self.assertEqual(decode_request(line + " ").code, ErrorCode.LINE_TOO_LONG)

    def test_fingerprint_ignores_key_order_and_spacing(self):
        a = decode_request('{"v":1,"type":"status","id":"q","session":"s1"}').request
        b = decode_request('{ "session" : "s1", "id": "q", "type": "status", "v": 1 }').request
        self.assertEqual(a.fingerprint, b.fingerprint)
        c = decode_request('{"v":1,"type":"status","id":"q","session":"s2"}').request
        self.assertNotEqual(a.fingerprint, c.fingerprint)

    def test_a_byte_order_mark_is_refused(self):
        """The reviewer's probe: C++ (nlohmann) skipped a leading BOM while Python refused it; both refuse now."""
        for line in ('\ufeff{"v":1,"type":"status","id":"q"}', b'\xef\xbb\xbf{"v":1,"type":"status","id":"q"}'):
            self.assertEqual((decode_request(line).code, decode_request(line).id), (ErrorCode.MALFORMED, None))

    def test_an_integer_no_double_holds_is_not_json(self):
        """The reviewer's probe: a 400-digit decision kept its id in Python but made the line not JSON in C++."""
        head = '{"v":1,"type":"reset","id":"r1","session":"s1","episode":"e1","scenario_seed":7,"decision":'
        for digits in ("9" * 400, "-" + "9" * 400, "2" + "0" * 308, "9" * 5000):
            d = decode_request(head + digits + "}")
            self.assertEqual((d.code, d.id), (ErrorCode.MALFORMED, None), digits[:10])
        d = decode_request(head + "1" + "0" * 308 + "}")
        self.assertEqual((d.code, d.id), (ErrorCode.MALFORMED, "r1"))

    def test_only_spaces_tabs_and_carriage_returns_are_blank(self):
        for line in (b"", b" ", b"\t", b"\r", b" \t\r "):
            self.assertTrue(is_blank_line(line), line)
        for line in (b"\x0b", b"\x0c", b" \x0b ", b"\x00", "\u00a0".encode(), b"{}"):
            self.assertFalse(is_blank_line(line), line)

    def test_clip_keeps_whole_characters(self):
        self.assertEqual(clip("café", 5), "café")
        self.assertEqual(clip("aéé" + "b", 5), "a...")
        self.assertEqual(clip("ééé", 5), "é...")
        self.assertEqual(clip("ok\ud800", 10), "ok?")
        self.assertLessEqual(len(clip("x" * 1000).encode()), MAX_TEXT_BYTES)

    def test_deep_nesting_is_malformed_not_a_crash(self):
        line = '{"v":1,"type":"status","id":"q","session":' + "[" * 5000 + "]" * 5000 + "}"
        self.assertEqual(decode_request(line).code, ErrorCode.MALFORMED)


class EncodeTest(unittest.TestCase):
    def test_compact_single_line(self):
        text = encode({"a": "x\ny", "b": [1, 2.5]})
        self.assertNotIn("\n", text)
        self.assertEqual(text, '{"a":"x\\ny","b":[1,2.5]}')
        self.assertEqual(line_bytes({"a": "é"}), len('{"a":""}') + 2)

    def test_refuses_nan(self):
        with self.assertRaises(ValueError):
            encode({"x": float("nan")})

    def test_canonical_sorts_keys(self):
        self.assertEqual(canonical({"b": 1, "a": 2}), '{"a":2,"b":1}')

    def test_error_shape(self):
        self.assertEqual(error(ErrorCode.BUSY, "r1", "m"),
                         {"v": 1, "type": "error", "id": "r1", "ok": False, "code": "busy", "message": "m"})
        self.assertEqual(error(ErrorCode.ABORTED, None, "m", "fault")["reason"], "fault")
        self.assertEqual(len(ErrorCode.ALL), 12)

    def test_bounds_round_trip(self):
        b = Bounds(max_events=3, max_copies=5)
        self.assertEqual(Bounds.from_wire(b.to_wire()), b)
        self.assertEqual(b.to_wire()["in_flight"], 1)
        self.assertEqual(json.loads(encode(b.to_wire()))["max_events"], 3)


if __name__ == "__main__":
    unittest.main()
