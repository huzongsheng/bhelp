from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .cache import CacheError, TranslationCache, make_cache_key
from .provider import PROMPT_VERSION, ChatCompletionsProvider, ProviderError


_LANGUAGE_NAMES = {
    "zh": "Simplified Chinese (zh-CN)",
    "zh-cn": "Simplified Chinese (zh-CN)",
    "zh-hans": "Simplified Chinese (zh-CN)",
    "zh-tw": "Traditional Chinese (zh-TW)",
    "zh-hant": "Traditional Chinese (zh-TW)",
    "en": "English (en)",
    "ja": "Japanese (ja)",
    "ja-jp": "Japanese (ja-JP)",
    "ko": "Korean (ko)",
    "fr": "French (fr)",
    "de": "German (de)",
    "es": "Spanish (es)",
    "ru": "Russian (ru)",
}


def language_name(value: str) -> str:
    if value.lower() == "auto":
        return "auto"
    return _LANGUAGE_NAMES.get(value.lower(), value)


def _canonical_language(value: str) -> str:
    lowered = value.lower().strip()
    aliases = {
        "simplified chinese": "zh-cn",
        "simplified chinese (zh-cn)": "zh-cn",
        "chinese": "zh-cn",
        "english": "en",
        "english (en)": "en",
        "traditional chinese": "zh-tw",
        "traditional chinese (zh-tw)": "zh-tw",
        "japanese": "ja",
        "japanese (ja)": "ja",
        "japanese (ja-jp)": "ja",
    }
    lowered = aliases.get(lowered, lowered)
    return {"zh": "zh-cn", "zh-hans": "zh-cn", "zh-hant": "zh-tw", "ja-jp": "ja"}.get(
        lowered, lowered
    )


@dataclass(frozen=True)
class TextPair:
    identifier: str
    original: str
    translation: str | None


@dataclass(frozen=True)
class TranslationResult:
    pairs: list[TextPair]
    detected_language: str | None
    cache_hits: int
    api_batches: int
    errors: list[str]


def _split_large_block(text: str, maximum: int) -> list[str]:
    if len(text) <= maximum:
        return [text]
    pieces: list[str] = []
    current: list[str] = []
    current_size = 0
    for line in text.splitlines(keepends=True):
        if len(line) > maximum:
            if current:
                pieces.append("".join(current).rstrip("\n"))
                current = []
                current_size = 0
            for offset in range(0, len(line), maximum):
                pieces.append(line[offset : offset + maximum].rstrip("\n"))
            continue
        if current and current_size + len(line) > maximum:
            pieces.append("".join(current).rstrip("\n"))
            current = []
            current_size = 0
        current.append(line)
        current_size += len(line)
    if current:
        pieces.append("".join(current).rstrip("\n"))
    return [piece for piece in pieces if piece]


def segment_text(text: str, maximum_block_chars: int) -> list[TextPair]:
    raw_blocks = [part.strip("\n") for part in re.split(r"\n[ \t]*\n+", text) if part.strip()]
    blocks: list[str] = []
    for raw_block in raw_blocks:
        blocks.extend(_split_large_block(raw_block, maximum_block_chars))
    return [TextPair(f"b{index:04d}", block, None) for index, block in enumerate(blocks)]


def _batches(items: Iterable[TextPair], maximum_chars: int) -> list[list[TextPair]]:
    batches: list[list[TextPair]] = []
    current: list[TextPair] = []
    current_chars = 0
    for item in items:
        projected = current_chars + len(item.original)
        if current and projected > maximum_chars:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(item)
        current_chars += len(item.original)
    if current:
        batches.append(current)
    return batches


def translate_text(
    text: str,
    *,
    source_language: str,
    target_language: str,
    provider: ChatCompletionsProvider | None,
    batch_chars: int,
    cache_enabled: bool,
    refresh_cache: bool,
    no_network: bool,
    cache: TranslationCache | None = None,
) -> TranslationResult:
    pairs = segment_text(text, batch_chars)
    if not pairs:
        return TranslationResult([], None, 0, 0, [])
    source_name = language_name(source_language)
    target_name = language_name(target_language)
    if source_name != "auto" and _canonical_language(source_name) == _canonical_language(target_name):
        return TranslationResult(pairs, source_name, 0, 0, [])
    if no_network:
        return TranslationResult(pairs, None, 0, 0, [])
    if provider is None:
        raise ValueError("provider is required unless no_network is enabled")

    active_cache = cache or TranslationCache()
    translations: dict[str, str] = {}
    cache_keys: dict[str, str] = {}
    detected_language: str | None = None
    cache_hits = 0
    errors: list[str] = []
    for pair in pairs:
        key = make_cache_key(
            provider=provider.settings.name,
            base_url=provider.settings.base_url,
            model=provider.settings.model,
            source=source_name,
            target=target_name,
            text=pair.original,
            prompt_version=PROMPT_VERSION,
        )
        cache_keys[pair.identifier] = key
        if cache_enabled and not refresh_cache:
            cached = active_cache.get(key)
            if cached:
                translations[pair.identifier] = cached["translation"]
                cached_source = cached.get("source_language")
                if isinstance(cached_source, str) and cached_source:
                    detected_language = detected_language or cached_source
                cache_hits += 1

    missing = [pair for pair in pairs if pair.identifier not in translations]
    api_batches = 0
    resolved_source = detected_language or source_name
    abort_remaining_batches = False

    def process_batch(batch: list[TextPair], *, allow_single_retry: bool = True) -> None:
        nonlocal api_batches, detected_language, resolved_source, abort_remaining_batches
        if abort_remaining_batches:
            return
        api_batches += 1
        try:
            batch_source, batch_translations = provider.translate_blocks(
                ((pair.identifier, pair.original) for pair in batch), resolved_source, target_name
            )
            detected_language = detected_language or batch_source
            if source_name == "auto":
                resolved_source = detected_language
            translations.update(batch_translations)
            if cache_enabled:
                for pair in batch:
                    try:
                        active_cache.put(
                            cache_keys[pair.identifier],
                            {
                                "translation": batch_translations[pair.identifier],
                                "source_language": batch_source,
                            },
                        )
                    except CacheError as exc:
                        errors.append(f"cache: {exc}")
        except ProviderError as exc:
            message = str(exc)
            recoverable_output_error = (
                message.startswith("completion") or message.startswith("translation JSON")
            )
            if recoverable_output_error and len(batch) > 1:
                midpoint = len(batch) // 2
                process_batch(batch[:midpoint])
                process_batch(batch[midpoint:])
            elif recoverable_output_error and allow_single_retry:
                process_batch(batch, allow_single_retry=False)
            else:
                errors.append(message)
                if not recoverable_output_error:
                    abort_remaining_batches = True

    for initial_batch in _batches(missing, batch_chars):
        if abort_remaining_batches:
            break
        process_batch(initial_batch)

    rendered_pairs = [
        TextPair(pair.identifier, pair.original, translations.get(pair.identifier)) for pair in pairs
    ]
    return TranslationResult(rendered_pairs, detected_language, cache_hits, api_batches, _unique(errors))


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result
