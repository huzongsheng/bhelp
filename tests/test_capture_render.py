import os
import sys
import unittest
from unittest.mock import patch

from bhelp.capture import CaptureFailure, capture_command
from bhelp.render import render_bilingual
from bhelp.translate import TextPair, segment_text


class CaptureTests(unittest.TestCase):
    def test_secret_environment_is_not_passed_to_child(self):
        with patch.dict(
            os.environ,
            {"DEEPSEEK_API_KEY": "canary", "OPENAI_API_KEY": "other", "BHELP_API_KEY": "generic"},
            clear=False,
        ):
            result = capture_command(
                [
                    sys.executable,
                    "-c",
                    "import os; print(os.getenv('DEEPSEEK_API_KEY'), os.getenv('OPENAI_API_KEY'), os.getenv('BHELP_API_KEY'))",
                ],
                5,
                1000,
            )
        self.assertEqual(result.text, "None None None")

    def test_command_arguments_are_not_evaluated_by_a_shell(self):
        payload = "$(printf injected);`printf bad`"
        result = capture_command([sys.executable, "-c", "import sys; print(sys.argv[1])", payload], 5, 1000)
        self.assertEqual(result.text, payload)

    def test_missing_command_has_127(self):
        with self.assertRaises(CaptureFailure) as context:
            capture_command(["bhelp-command-that-does-not-exist"], 1, 100)
        self.assertEqual(context.exception.returncode, 127)

    def test_excessive_output_is_bounded_and_command_is_stopped(self):
        result = capture_command(
            [sys.executable, "-c", "import sys; sys.stdout.write('x' * 1000000)"],
            5,
            100,
        )
        self.assertTrue(result.too_large)
        self.assertEqual(result.returncode, 125)
        self.assertLess(len(result.text), 200)
        self.assertIn("input truncated", result.text)


class RenderTests(unittest.TestCase):
    def test_original_is_above_translation(self):
        output = render_bilingual(
            [TextPair("b0000", "DESCRIPTION", "说明")], "zh-CN", color=False, width=80
        )
        self.assertLess(output.index("DESCRIPTION"), output.index("说明"))
        self.assertIn("【Simplified Chinese (zh-CN)】", output)

    def test_segmentation_preserves_order_and_splits_large_blocks(self):
        pairs = segment_text("FIRST\n\n" + "x" * 15 + "\n\nLAST", 10)
        self.assertEqual(pairs[0].original, "FIRST")
        self.assertEqual(pairs[-1].original, "LAST")
        self.assertGreaterEqual(len(pairs), 4)


if __name__ == "__main__":
    unittest.main()
