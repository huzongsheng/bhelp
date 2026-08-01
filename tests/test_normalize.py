import unittest

from bhelp.normalize import decode_text, sanitize_text


class NormalizeTests(unittest.TestCase):
    def test_removes_ansi_osc_bidi_and_overstrike(self):
        source = "\x1b]52;c;ZXZpbA==\x07\x1b[31mN\bNA\bAM\bME\x1b[0m\u202e"
        self.assertEqual(sanitize_text(source), "NAME")

    def test_carriage_return_cannot_overwrite_output(self):
        self.assertEqual(sanitize_text("first\rsecond"), "first\nsecond")

    def test_c1_terminal_controls_are_removed(self):
        normalized = sanitize_text("safe\u009b31mred\u009d52;c;secret\u0007tail")
        self.assertNotIn("\u009b", normalized)
        self.assertNotIn("\u009d", normalized)
        self.assertNotIn("\u0007", normalized)
        self.assertEqual(normalized, "saferedtail")

    def test_binary_input_is_rejected(self):
        with self.assertRaises(ValueError):
            decode_text(b"text\x00binary")

    def test_utf8_is_preserved(self):
        self.assertEqual(decode_text("中文".encode()), "中文")


if __name__ == "__main__":
    unittest.main()
