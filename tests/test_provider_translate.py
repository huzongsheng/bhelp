import json
import io
import threading
import tempfile
import unittest
import urllib.error
from unittest.mock import Mock, patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bhelp.cache import TranslationCache
from bhelp.provider import ChatCompletionsProvider, ProviderError, ProviderSettings
from bhelp.translate import translate_text


class _Handler(BaseHTTPRequestHandler):
    requests = 0
    last_authorization = None
    status = 200

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        type(self).last_authorization = self.headers.get("Authorization")
        body = {"data": [{"id": "test-model"}]}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_POST(self):
        type(self).requests += 1
        type(self).last_authorization = self.headers.get("Authorization")
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        if type(self).status != 200:
            self.send_response(type(self).status)
            self.end_headers()
            self.wfile.write(b'{"error":"sk-canary-secret"}')
            return
        user = json.loads(request["messages"][1]["content"])
        translated = {
            "source_language": "English",
            "translations": [
                {"id": block["id"], "text": "ZH:" + block["text"]} for block in user["blocks"]
            ],
        }
        body = {"choices": [{"message": {"content": json.dumps(translated)}}]}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())


class ProviderTests(unittest.TestCase):
    def setUp(self):
        _Handler.requests = 0
        _Handler.last_authorization = None
        _Handler.status = 200
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.settings = ProviderSettings(
            name="deepseek",
            api_key="sk-canary-secret",
            base_url=f"http://127.0.0.1:{self.server.server_port}",
            model="test-model",
            timeout_seconds=2,
            retries=0,
        )

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_models_and_translation(self):
        provider = ChatCompletionsProvider(self.settings)
        self.assertIn("test-model", provider.list_models())
        detected, translated = provider.translate_blocks([("b0000", "hello")], "auto", "zh-CN")
        self.assertEqual(detected, "English")
        self.assertEqual(translated["b0000"], "ZH:hello")
        self.assertEqual(_Handler.last_authorization, "Bearer sk-canary-secret")

    def test_authentication_error_does_not_leak_key(self):
        _Handler.status = 401
        provider = ChatCompletionsProvider(self.settings)
        with self.assertRaises(ProviderError) as context:
            provider.translate_blocks([("b0000", "hello")], "auto", "zh-CN")
        self.assertNotIn("sk-canary-secret", str(context.exception))

    def test_translation_cache_avoids_second_request(self):
        provider = ChatCompletionsProvider(self.settings)
        with tempfile.TemporaryDirectory() as directory:
            cache = TranslationCache(Path(directory))
            first = translate_text(
                "hello world",
                source_language="auto",
                target_language="zh-CN",
                provider=provider,
                batch_chars=1000,
                cache_enabled=True,
                refresh_cache=False,
                no_network=False,
                cache=cache,
            )
            second = translate_text(
                "hello world",
                source_language="auto",
                target_language="zh-CN",
                provider=provider,
                batch_chars=1000,
                cache_enabled=True,
                refresh_cache=False,
                no_network=False,
                cache=cache,
            )
        self.assertEqual(first.api_batches, 1)
        self.assertEqual(second.api_batches, 0)
        self.assertEqual(second.cache_hits, 1)
        self.assertEqual(_Handler.requests, 1)


class MalformedResponseTests(unittest.TestCase):
    def setUp(self):
        self.provider = ChatCompletionsProvider(ProviderSettings(
            name="deepseek", api_key="unused", base_url="http://127.0.0.1",
            model="test-model", timeout_seconds=1, retries=1,
        ))

    def completion(self, identifier="b0000", text="translated", source="English"):
        return {"choices": [{"message": {"content": json.dumps({
            "source_language": source,
            "translations": [{"id": identifier, "text": text}],
        })}}]}

    def test_non_string_ids_raise_provider_error(self):
        for identifier in ([], {}, None, 1):
            with self.subTest(identifier=identifier), patch.object(
                self.provider, "_request_json", return_value=self.completion(identifier=identifier)
            ):
                with self.assertRaisesRegex(ProviderError, "translation JSON"):
                    self.provider.translate_blocks([("b0000", "hello")], "auto", "zh-CN")

    def test_empty_or_control_only_output_is_recoverable(self):
        for text in ("", " \t ", "\x1b[31m\x1b[0m"):
            with self.subTest(text=text), patch.object(
                self.provider, "_request_json", return_value=self.completion(text=text)
            ):
                with self.assertRaisesRegex(ProviderError, "^translation JSON"):
                    self.provider.translate_blocks([("b0000", "hello")], "auto", "zh-CN")

    def test_control_only_source_language_is_rejected(self):
        with patch.object(self.provider, "_request_json", return_value=self.completion(source="\x1b[0m")):
            with self.assertRaisesRegex(ProviderError, "translation JSON"):
                self.provider.translate_blocks([("b0000", "hello")], "auto", "zh-CN")

    def test_empty_translation_retries_and_keeps_original_on_failure(self):
        with patch.object(self.provider, "_request_json", return_value=self.completion(text="")):
            result = translate_text(
                "hello", source_language="auto", target_language="zh-CN",
                provider=self.provider, batch_chars=1000, cache_enabled=False,
                refresh_cache=False, no_network=False,
            )
        self.assertEqual(result.api_batches, 2)
        self.assertEqual(result.pairs[0].original, "hello")
        self.assertIsNone(result.pairs[0].translation)
        self.assertEqual(len(result.errors), 1)

    def test_invalid_retry_after_uses_bounded_backoff(self):
        for retry_after in ("-1", "NaN", "nonsense", "0"):
            with self.subTest(retry_after=retry_after):
                error = urllib.error.HTTPError(
                    "http://127.0.0.1", 429, "rate limited",
                    {"Retry-After": retry_after}, io.BytesIO(b"{}"),
                )
                response = Mock()
                response.__enter__ = Mock(return_value=response)
                response.__exit__ = Mock(return_value=False)
                response.read.return_value = b'{"data": []}'
                with patch.object(self.provider._opener, "open", side_effect=[error, response]), patch(
                    "bhelp.provider.time.sleep"
                ) as sleep:
                    self.assertEqual(self.provider.list_models(), [])
                    delay = sleep.call_args.args[0]
                    self.assertGreaterEqual(delay, 0.5)
                    self.assertLessEqual(delay, 0.7)


class _SplitRecoveryProvider:
    def __init__(self):
        self.settings = ProviderSettings(
            name="deepseek",
            api_key="unused",
            base_url="https://example.invalid",
            model="test-model",
            timeout_seconds=1,
            retries=0,
        )

    def translate_blocks(self, blocks, source_language, target_language):
        material = list(blocks)
        if len(material) > 1:
            raise ProviderError("completion contained invalid translation JSON")
        identifier, text = material[0]
        return "English", {identifier: "ZH:" + text}


class RecoveryTests(unittest.TestCase):
    def test_invalid_large_batch_is_split_and_recovered(self):
        result = translate_text(
            "first\n\nsecond",
            source_language="auto",
            target_language="zh-CN",
            provider=_SplitRecoveryProvider(),
            batch_chars=1000,
            cache_enabled=False,
            refresh_cache=False,
            no_network=False,
        )
        self.assertEqual(result.errors, [])
        self.assertEqual(result.api_batches, 3)
        self.assertEqual([pair.translation for pair in result.pairs], ["ZH:first", "ZH:second"])

    def test_systemic_provider_error_aborts_remaining_batches(self):
        class FailingProvider(_SplitRecoveryProvider):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def translate_blocks(self, blocks, source_language, target_language):
                self.calls += 1
                raise ProviderError("API key authentication failed")

        provider = FailingProvider()
        result = translate_text(
            "a\n\nb\n\nc",
            source_language="auto",
            target_language="zh-CN",
            provider=provider,
            batch_chars=1,
            cache_enabled=False,
            refresh_cache=False,
            no_network=False,
        )
        self.assertEqual(provider.calls, 1)
        self.assertEqual(result.api_batches, 1)
        self.assertEqual(result.errors, ["API key authentication failed"])
        self.assertTrue(all(pair.translation is None for pair in result.pairs))


if __name__ == "__main__":
    unittest.main()
