import json
import threading
import tempfile
import unittest
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
