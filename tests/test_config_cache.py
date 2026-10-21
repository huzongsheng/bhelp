import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bhelp.cache import TranslationCache, make_cache_key
from bhelp.config import ConfigError, initialize_config, load_config, redacted_config, set_api_key


class ConfigTests(unittest.TestCase):
    def test_config_is_private_and_show_is_redacted(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_CONFIG_HOME": directory}, clear=False
        ):
            path = initialize_config()
            set_api_key("deepseek", "canary-secret")
            config = load_config()
            self.assertEqual(config["providers"]["deepseek"]["api_key"], "canary-secret")
            shown = json.dumps(redacted_config(config))
            self.assertNotIn("canary-secret", shown)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_set_key_removes_utf8_bom_from_piped_input(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_CONFIG_HOME": directory}, clear=False
        ):
            initialize_config()
            set_api_key("deepseek", "\ufeffsk-test-value\r\n")
            self.assertEqual(load_config()["providers"]["deepseek"]["api_key"], "sk-test-value")

    def test_explicit_config_does_not_chmod_existing_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            shared = Path(directory) / "shared"
            shared.mkdir(mode=0o755)
            os.chmod(shared, 0o755)
            initialize_config(shared / "config.json")
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(shared.stat().st_mode), 0o755)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_symbolic_link_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target.json"
            target.write_text("{}", encoding="utf-8")
            link = root / "config.json"
            link.symlink_to(target)
            with self.assertRaises(ConfigError):
                load_config(link)


class CacheTests(unittest.TestCase):
    def test_cache_round_trip_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = TranslationCache(Path(directory) / "cache")
            key = "a" * 64
            cache.put(key, {"translation": "翻译", "source_language": "English"})
            self.assertEqual(cache.get(key)["translation"], "翻译")
            count, size = cache.stats()
            self.assertEqual(count, 1)
            self.assertGreater(size, 0)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE((cache.root / f"{key}.json").stat().st_mode), 0o600)

    def test_cache_key_separates_target_model_and_provider(self):
        common = dict(source="auto", text="hello", prompt_version="v1")
        keys = {
            make_cache_key(provider="deepseek", base_url="https://a", model="a", target="zh", **common),
            make_cache_key(provider="deepseek", base_url="https://a", model="b", target="zh", **common),
            make_cache_key(provider="openai", base_url="https://a", model="a", target="zh", **common),
            make_cache_key(provider="deepseek", base_url="https://a", model="a", target="ja", **common),
            make_cache_key(provider="deepseek", base_url="https://b", model="a", target="zh", **common),
        }
        self.assertEqual(len(keys), 5)

    def test_corrupt_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = TranslationCache(Path(directory))
            key = "b" * 64
            (Path(directory) / f"{key}.json").write_text("not-json", encoding="utf-8")
            self.assertIsNone(cache.get(key))

    def test_cache_path_failure_is_wrapped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "cache"
            root.write_text("not a directory", encoding="utf-8")
            cache = TranslationCache(root)
            from bhelp.cache import CacheError

            with self.assertRaises(CacheError):
                cache.put("c" * 64, {"translation": "x"})

    def test_non_utf8_cache_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = TranslationCache(Path(directory))
            key = "d" * 64
            (cache.root / f"{key}.json").write_bytes(b"\xff\xfe")
            self.assertIsNone(cache.get(key))

    def test_cached_text_is_sanitized_before_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = TranslationCache(Path(directory))
            key = "e" * 64
            cache.put(key, {"translation": "\x1b[31mhello\x1b[0m", "source_language": "\x1b[0mEnglish"})
            self.assertEqual(cache.get(key), {"translation": "hello", "source_language": "English"})

    def test_empty_cached_translation_and_invalid_source_are_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = TranslationCache(Path(directory))
            key = "f" * 64
            for data in (
                {"translation": ""}, {"translation": " \t "},
                {"translation": "\x1b[0m"},
                {"translation": "hello", "source_language": []},
            ):
                with self.subTest(data=data):
                    cache.put(key, data)
                    self.assertIsNone(cache.get(key))


if __name__ == "__main__":
    unittest.main()
