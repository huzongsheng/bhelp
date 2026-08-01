from __future__ import annotations

import copy
import io
import os
import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bhelp.config import DEFAULT_CONFIG
from bhelp.pager import (
    PagerBlock,
    PagerUnavailable,
    PagerSession,
    _TranslationResult,
    _document_lines,
    _fallback_environment,
    _restore_anchor,
    _run_screen,
    run_pager,
)
from bhelp.provider import ProviderSettings


def _args(**overrides):
    values = {
        "files": [],
        "source_language": None,
        "target_language": None,
        "provider": None,
        "model": None,
        "base_url": None,
        "no_cache": False,
        "refresh": False,
        "no_network": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _config(**overrides):
    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(overrides)
    return config


class _BinaryStdout:
    def __init__(self, *, isatty: bool):
        self.buffer = io.BytesIO()
        self._isatty = isatty

    def isatty(self):
        return self._isatty

    def flush(self):
        pass


class _RecordingTranslator:
    def __init__(self):
        self.submissions = []
        self.results = queue.Queue()
        self.closed = False

    def submit(self, blocks, priority):
        self.submissions.append(([block.identifier for block in blocks], priority))

    def close(self):
        self.closed = True


class PagerEntryPointTests(unittest.TestCase):
    def test_non_tty_is_a_byte_for_byte_passthrough_without_creating_provider(self):
        raw = b"\xff\x00\x1b[31mraw\r\nhelp\x80"
        output = _BinaryStdout(isatty=False)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "manual.bin"
            source.write_bytes(raw)
            args = _args(files=[str(source)])
            with patch("bhelp.pager.sys.stdout", output), patch(
                "bhelp.pager.ChatCompletionsProvider"
            ) as provider_type:
                result = run_pager(args, _config())

        self.assertEqual(result, 0)
        self.assertEqual(output.buffer.getvalue(), raw)
        provider_type.assert_not_called()

    def test_tty_fallback_receives_sanitized_text_not_terminal_control_bytes(self):
        raw = b"safe\x1b]52;c;secret\x07tail\x00\xff\n"
        output = _BinaryStdout(isatty=True)
        with patch("bhelp.pager.sys.stdout", output), patch(
            "bhelp.pager._read_input", return_value=raw
        ), patch("bhelp.pager._run_curses", side_effect=PagerUnavailable("test")), patch(
            "bhelp.pager._display_fallback", return_value=0
        ) as fallback, patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=False):
            result = run_pager(_args(), _config())

        self.assertEqual(result, 0)
        safe = fallback.call_args.args[0]
        self.assertNotIn(b"\x1b", safe)
        self.assertNotIn(b"\x00", safe)
        self.assertNotIn(b"secret", safe)
        self.assertIn(b"safetail", safe)

    def test_tty_unsupported_term_sanitizes_instead_of_byte_passthrough(self):
        raw = b"safe\x1b]52;c;secret\x07tail\n"
        for term in ("dumb", ""):
            with self.subTest(term=term):
                output = _BinaryStdout(isatty=True)
                with patch("bhelp.pager.sys.stdout", output), patch(
                    "bhelp.pager._read_input", return_value=raw
                ), patch("bhelp.pager.ChatCompletionsProvider") as provider_type, patch.dict(
                    os.environ, {"TERM": term}, clear=False
                ):
                    result = run_pager(_args(), _config())

                self.assertEqual(result, 0)
                self.assertEqual(output.buffer.getvalue(), b"safetail\n")
                provider_type.assert_not_called()

    def test_interactive_ctrl_c_is_a_clean_pager_exit(self):
        output = _BinaryStdout(isatty=True)
        with patch("bhelp.pager.sys.stdout", output), patch(
            "bhelp.pager._read_input", return_value=b"ORIGINAL HELP\n"
        ), patch("bhelp.pager._run_curses", side_effect=KeyboardInterrupt), patch.dict(
            os.environ, {"TERM": "xterm-256color"}, clear=False
        ):
            result = run_pager(_args(no_network=True), _config())

        self.assertEqual(result, 0)

    def test_fallback_environment_removes_all_supported_secret_variables(self):
        secrets = {
            "BHELP_API_KEY": "bhelp-secret",
            "BHELP_CONFIG": "/secret/config.json",
            "DEEPSEEK_API_KEY": "deepseek-secret",
            "OPENAI_API_KEY": "openai-secret",
        }
        with patch.dict(os.environ, {**secrets, "SAFE_VALUE": "kept"}, clear=True):
            environment = _fallback_environment()

        for name in secrets:
            self.assertNotIn(name, environment)
        self.assertEqual(environment["SAFE_VALUE"], "kept")
        self.assertEqual(environment["MANPAGER"], "cat")
        self.assertEqual(environment["LESSCHARSET"], "utf-8")


class LazySchedulingTests(unittest.TestCase):
    def test_large_source_block_is_split_at_the_lazy_batch_limit(self):
        session = PagerSession(
            "x" * 9_000,
            "test",
            _args(no_network=True),
            _config(batch_chars=10_000),
        )

        self.assertEqual([len(block.original) for block in session.blocks], [4_000, 4_000, 1_000])

    def test_only_visible_and_next_screen_blocks_are_scheduled_once(self):
        text = "first\n\nsecond\n\nthird\n\nfourth"
        session = PagerSession(text, "test", _args(no_network=True), _config(batch_chars=100))
        translator = _RecordingTranslator()
        session.translator = translator
        session.started = True
        session.network_enabled = True

        session.schedule(["b0000", "b0001"], ["b0001", "b0002"])

        self.assertEqual(
            translator.submissions,
            [(["b0000", "b0001"], 0), (["b0002"], 10)],
        )
        self.assertEqual(
            [block.state for block in session.blocks],
            ["queued", "queued", "queued", "waiting"],
        )

        session.schedule(["b0000", "b0001"], ["b0001", "b0002"])
        self.assertEqual(len(translator.submissions), 2)

    def test_all_cache_hits_make_no_api_call(self):
        class AlwaysHitCache:
            def get(self, _key):
                return {"translation": "cached", "source_language": "English (en)"}

            def put(self, _key, _value):
                raise AssertionError("a cache hit must not be rewritten")

        settings = ProviderSettings(
            name="deepseek",
            api_key="test-key",
            base_url="https://api.deepseek.com",
            model="test-model",
            timeout_seconds=1,
            retries=0,
        )
        provider = MagicMock()
        with patch("bhelp.pager.TranslationCache", return_value=AlwaysHitCache()), patch(
            "bhelp.pager.resolve_provider_settings", return_value=settings
        ), patch("bhelp.pager.ChatCompletionsProvider", return_value=provider):
            session = PagerSession(
                "first\n\nsecond", "test", _args(), _config(batch_chars=100)
            )
            session.start()
            session.schedule(["b0000"], ["b0001"])

        self.assertEqual(session.cache_hits, 2)
        self.assertTrue(all(block.state == "translated" for block in session.blocks))
        self.assertIsNotNone(session.translator)
        provider.translate_blocks.assert_not_called()
        session.close()


class RenderingAndFailureTests(unittest.TestCase):
    def test_translation_is_inserted_under_its_block_and_anchor_is_restored(self):
        session = PagerSession(
            "FIRST original\n\nSECOND original",
            "test",
            _args(no_network=True),
            _config(batch_chars=100),
        )
        before = _document_lines(session, 80)
        old_top = next(
            index
            for index, line in enumerate(before)
            if line.block_identifier == "b0001" and line.part == "original"
        )
        anchor = before[old_top].anchor

        session.blocks[0].translation = "translated first"
        session.blocks[0].state = "translated"
        after = _document_lines(session, 80)
        restored = _restore_anchor(after, anchor, old_top)

        self.assertEqual(after[restored].anchor, anchor)
        first_original = next(
            index
            for index, line in enumerate(after)
            if line.block_identifier == "b0000" and line.part == "original"
        )
        first_translation = next(
            index
            for index, line in enumerate(after)
            if line.block_identifier == "b0000" and line.part == "translation"
        )
        second_original = next(
            index
            for index, line in enumerate(after)
            if line.block_identifier == "b0001" and line.part == "original"
        )
        self.assertLess(first_original, first_translation)
        self.assertLess(first_translation, second_original)
        self.assertEqual(after[first_translation].text, "translated first")

    def test_fatal_provider_result_disables_network_but_keeps_original_blocks(self):
        session = PagerSession(
            "original one\n\noriginal two",
            "test",
            _args(no_network=True),
            _config(batch_chars=100),
        )
        translator = _RecordingTranslator()
        session.translator = translator
        session.network_enabled = True
        for block in session.blocks:
            block.state = "queued"
        originals = [block.original for block in session.blocks]
        translator.results.put(
            _TranslationResult(
                identifiers=("b0000",),
                detected_language=None,
                translations={},
                error="API key authentication failed",
                fatal=True,
                api_batches=1,
            )
        )

        self.assertTrue(session.drain_results())

        self.assertFalse(session.network_enabled)
        self.assertTrue(translator.closed)
        self.assertEqual([block.original for block in session.blocks], originals)
        self.assertEqual(session.blocks[0].state, "error")
        self.assertEqual(session.blocks[1].state, "waiting")
        self.assertEqual(session.api_batches, 1)
        lines = _document_lines(session, 80)
        self.assertIn("original one", [line.text for line in lines])
        self.assertIn("original two", [line.text for line in lines])
        self.assertFalse(any(line.part == "translation" for line in lines))


class ScreenOrderingTests(unittest.TestCase):
    class Curses:
        A_NORMAL = 0
        A_BOLD = 1
        A_DIM = 2
        A_REVERSE = 4
        COLOR_GREEN = 2
        COLOR_CYAN = 6
        KEY_RESIZE = 1000
        KEY_DOWN = 1001
        KEY_UP = 1002
        KEY_NPAGE = 1003
        KEY_PPAGE = 1004
        KEY_HOME = 1005
        KEY_END = 1006

        @staticmethod
        def has_colors():
            return False

        @staticmethod
        def curs_set(_value):
            pass

        @staticmethod
        def color_pair(value):
            return value << 4

    def test_original_frame_refreshes_before_session_start_and_quit_closes(self):
        events = []

        class Screen:
            def keypad(self, _enabled):
                pass

            def timeout(self, _milliseconds):
                pass

            def getmaxyx(self):
                return (5, 60)

            def erase(self):
                pass

            def addstr(self, *_args):
                pass

            def refresh(self):
                events.append("refresh")

            def getch(self):
                return ord("q")

        class Session:
            blocks = [PagerBlock("b0000", "ORIGINAL HELP")]
            target_name = "Simplified Chinese (zh-CN)"
            label = "test"
            detected_language = None
            source_language = "auto"
            target_language = "zh-CN"
            cache_hits = 0
            api_batches = 0
            status_message = ""

            def drain_results(self):
                return False

            def loading_count(self):
                return 0

            def start(self):
                events.append("start")

            def schedule(self, visible, prefetch):
                events.append(("schedule", tuple(visible), tuple(prefetch)))

            def close(self):
                events.append("close")

        result = _run_screen(Screen(), Session(), self.Curses)

        self.assertEqual(result, 0)
        self.assertLess(events.index("refresh"), events.index("start"))
        self.assertEqual(events.count("start"), 1)
        self.assertEqual(events[-1], "close")
        scheduled = next(event for event in events if isinstance(event, tuple))
        self.assertEqual(scheduled[1], ("b0000",))


if __name__ == "__main__":
    unittest.main()
