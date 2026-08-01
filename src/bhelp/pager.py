from __future__ import annotations

import copy
import os
import queue
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .cache import CacheError, TranslationCache, make_cache_key
from .config import DEFAULT_CONFIG
from .normalize import decode_text, sanitize_text
from .provider import PROMPT_VERSION, ChatCompletionsProvider, ProviderError, resolve_provider_settings
from .translate import language_name, segment_text


_SECRET_ENV_VARS = {
    "BHELP_API_KEY",
    "BHELP_CONFIG",
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
}
_MAX_LAZY_BATCH_CHARS = 4_000
_MAX_LAZY_BATCH_BLOCKS = 6
_POLL_MILLISECONDS = 75


class PagerError(RuntimeError):
    pass


class PagerUnavailable(RuntimeError):
    pass


class _TerminalSignal(RuntimeError):
    def __init__(self, signum: int):
        super().__init__(f"terminal signal {signum}")
        self.signum = signum


@dataclass
class PagerBlock:
    identifier: str
    original: str
    translation: str | None = None
    state: str = "waiting"
    cache_key: str | None = None
    cache_checked: bool = False


@dataclass(frozen=True)
class VirtualLine:
    text: str
    block_identifier: str
    part: str
    part_line: int
    style: str = "original"

    @property
    def anchor(self) -> tuple[str, str, int]:
        return (self.block_identifier, self.part, self.part_line)


@dataclass(order=True, frozen=True)
class _TranslationTask:
    priority: int
    sequence: int
    blocks: tuple[tuple[str, str], ...] = field(compare=False)


@dataclass(frozen=True)
class _TranslationResult:
    identifiers: tuple[str, ...]
    detected_language: str | None
    translations: dict[str, str]
    error: str | None
    fatal: bool
    api_batches: int


def _effective_value(args: Any, config: dict[str, Any], name: str, env_name: str | None = None) -> Any:
    value = getattr(args, name, None)
    if value is not None:
        return value
    if env_name and env_name in os.environ:
        return os.environ[env_name]
    return config[name]


def _read_fd(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        try:
            chunk = os.read(fd, 64 * 1024)
        except InterruptedError:
            continue
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _read_input(files: Sequence[str]) -> bytes:
    if files:
        chunks: list[bytes] = []
        for value in files:
            if value == "-":
                chunks.append(_read_fd(sys.stdin.fileno()))
                continue
            path = Path(value)
            try:
                chunks.append(path.read_bytes())
            except OSError as exc:
                raise PagerError(f"cannot read pager input {path}: {exc}") from exc
        return b"\n".join(chunks)
    try:
        return _read_fd(sys.stdin.fileno())
    except (AttributeError, OSError):
        stream = getattr(sys.stdin, "buffer", sys.stdin)
        data = stream.read()
        return data.encode() if isinstance(data, str) else data


def _write_original(raw: bytes) -> int:
    try:
        stream = getattr(sys.stdout, "buffer", None)
        if stream is not None:
            stream.write(raw)
            stream.flush()
        else:
            sys.stdout.write(raw.decode("utf-8", errors="replace"))
            sys.stdout.flush()
    except BrokenPipeError:
        return 0
    return 0


def _fallback_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in _SECRET_ENV_VARS:
        environment.pop(name, None)
    environment["MANPAGER"] = "cat"
    environment.setdefault("LESSCHARSET", "utf-8")
    return environment


def _fallback_command() -> list[str] | None:
    configured = os.environ.get("BHELP_FALLBACK_MANPAGER") or os.environ.get("BHELP_PAGER")
    if configured:
        try:
            command = shlex.split(configured)
        except ValueError:
            command = []
        executable_names = {Path(part).name for part in command[:1]}
        if command and not ("bhelp" in executable_names and "pager" in command[1:]):
            return command
    less = shutil.which("less")
    return [less, "-R"] if less else None


def _display_fallback(raw: bytes) -> int:
    raw = _safe_terminal_bytes(raw)
    if not sys.stdout.isatty() or os.environ.get("TERM", "").lower() in {"", "dumb"}:
        return _write_original(raw)
    command = _fallback_command()
    if not command:
        return _write_original(raw)
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, env=_fallback_environment())
    except (FileNotFoundError, OSError):
        return _write_original(raw)
    try:
        process.communicate(raw)
    except KeyboardInterrupt:
        if process.poll() is None:
            try:
                process.send_signal(signal.SIGINT)
                process.wait(timeout=0.5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.terminate()
                    process.wait(timeout=0.5)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        process.kill()
                    except OSError:
                        pass
        raise
    return 128 - process.returncode if process.returncode and process.returncode < 0 else int(process.returncode or 0)


def _safe_terminal_bytes(raw: bytes) -> bytes:
    """Return printable UTF-8 for an interactive fallback pager.

    Non-interactive output deliberately remains byte-for-byte transparent, but a
    terminal fallback must not replay OSC, DCS, bidi overrides, or broken binary
    control bytes that the curses path would otherwise sanitize.
    """

    try:
        decoded = decode_text(raw)
    except ValueError:
        decoded = raw.decode("utf-8", errors="replace")
    cleaned = sanitize_text(decoded)
    if cleaned and raw.endswith((b"\n", b"\r")):
        cleaned += "\n"
    return cleaned.encode("utf-8")


def _display_width(text: str) -> int:
    width = 0
    for character in text:
        if unicodedata.combining(character):
            continue
        width += 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
    return width


def _clip_visual(text: str, maximum: int) -> str:
    if maximum <= 0:
        return ""
    output: list[str] = []
    width = 0
    for character in text:
        character_width = 0 if unicodedata.combining(character) else (
            2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
        )
        if width + character_width > maximum:
            break
        output.append(character)
        width += character_width
    return "".join(output)


def _wrap_visual(line: str, maximum: int) -> list[str]:
    line = line.expandtabs(8)
    if not line or _display_width(line) <= maximum:
        return [line]
    output: list[str] = []
    current: list[str] = []
    current_width = 0
    last_break = -1
    for character in line:
        character_width = 0 if unicodedata.combining(character) else (
            2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
        )
        if current and current_width + character_width > maximum:
            split_at = last_break + 1 if last_break >= max(0, len(current) // 2) else len(current)
            output.append("".join(current[:split_at]).rstrip())
            current = current[split_at:]
            while current and current[0].isspace():
                current.pop(0)
            current_width = _display_width("".join(current))
            last_break = -1
            for index, existing in enumerate(current):
                if existing.isspace():
                    last_break = index
        current.append(character)
        current_width += character_width
        if character.isspace() or unicodedata.east_asian_width(character) in {"W", "F"}:
            last_break = len(current) - 1
    if current or not output:
        output.append("".join(current).rstrip())
    return output


def _is_heading(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not line[:1].isspace() and len(stripped) <= 80 and stripped.upper() == stripped


class LazyTranslator:
    def __init__(self, provider: ChatCompletionsProvider, source_language: str, target_language: str):
        self.provider = provider
        self.source_language = source_language
        self.target_language = target_language
        self.results: queue.Queue[_TranslationResult] = queue.Queue()
        self._tasks: queue.PriorityQueue[_TranslationTask] = queue.PriorityQueue()
        self._stop = threading.Event()
        self._sequence = 0
        self._thread: threading.Thread | None = None
        self._resolved_source = source_language

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="bhelp-translation", daemon=True)
        self._thread.start()

    def submit(self, blocks: Iterable[PagerBlock], priority: int) -> None:
        block_data = tuple((block.identifier, block.original) for block in blocks)
        if not block_data or self._stop.is_set():
            return
        self._sequence += 1
        self._tasks.put(_TranslationTask(priority, self._sequence, block_data))

    def close(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                task = self._tasks.get(timeout=0.1)
            except queue.Empty:
                continue
            if self._stop.is_set():
                return
            identifiers = tuple(identifier for identifier, _ in task.blocks)
            try:
                detected, translations = self.provider.translate_blocks(
                    task.blocks, self._resolved_source, self.target_language
                )
                if self.source_language == "auto" and detected:
                    self._resolved_source = detected
                self.results.put(_TranslationResult(identifiers, detected, translations, None, False, 1))
            except ProviderError as exc:
                message = str(exc)
                recoverable = message.startswith("completion") or message.startswith("translation JSON")
                if not recoverable:
                    self._stop.set()
                self.results.put(_TranslationResult(identifiers, None, {}, message, not recoverable, 1))
            except Exception:
                # An unexpected worker failure must not leave visible blocks in a
                # permanent loading state or expose exception details in the TUI.
                self._stop.set()
                self.results.put(
                    _TranslationResult(identifiers, None, {}, "translation worker failed", True, 1)
                )


class PagerSession:
    def __init__(
        self,
        text: str,
        label: str,
        args: Any,
        config: dict[str, Any],
        startup_warning: str = "",
    ):
        maximum_block_chars = min(int(config["batch_chars"]), _MAX_LAZY_BATCH_CHARS)
        self.blocks = [PagerBlock(pair.identifier, pair.original) for pair in segment_text(text, maximum_block_chars)]
        self.by_identifier = {block.identifier: block for block in self.blocks}
        self.label = label
        raw_source = str(_effective_value(args, config, "source_language", "BHELP_SOURCE_LANGUAGE"))
        raw_target = str(_effective_value(args, config, "target_language", "BHELP_TARGET_LANGUAGE"))
        self.source_language = sanitize_text(raw_source).replace("\n", " ")[:80] or "auto"
        self.target_language = sanitize_text(raw_target).replace("\n", " ")[:80] or "zh-CN"
        self.target_name = sanitize_text(language_name(self.target_language)).replace("\n", " ")[:120]
        self.detected_language: str | None = None
        self.cache_hits = 0
        self.api_batches = 0
        self.status_message = ""
        self.started = False
        self.network_enabled = False
        self.cache_enabled = bool(config["cache_enabled"]) and not bool(getattr(args, "no_cache", False))
        self.refresh_cache = bool(getattr(args, "refresh", False))
        self.cache = TranslationCache()
        self.translator: LazyTranslator | None = None
        self.lazy_batch_chars = maximum_block_chars
        if startup_warning:
            self.status_message = sanitize_text(startup_warning).replace("\n", " ")
        else:
            self._prepare(text, args, config)

    def _prepare(self, text: str, args: Any, config: dict[str, Any]) -> None:
        if bool(getattr(args, "no_network", False)):
            self.status_message = "离线模式：仅显示原文"
            return
        if len(text) > int(config["max_input_chars"]):
            self.status_message = "页面过大：仅显示原文，未发送到 API"
            return
        source_name = language_name(self.source_language)
        if source_name != "auto" and source_name.casefold() == self.target_name.casefold():
            self.status_message = "源语言与目标语言相同"
            return
        try:
            settings = resolve_provider_settings(
                config,
                provider_override=getattr(args, "provider", None),
                model_override=getattr(args, "model", None),
                base_url_override=getattr(args, "base_url", None),
            )
        except ProviderError as exc:
            self.status_message = f"翻译不可用：{exc}"
            return
        provider = ChatCompletionsProvider(settings)
        for block in self.blocks:
            block.cache_key = make_cache_key(
                provider=settings.name,
                base_url=settings.base_url,
                model=settings.model,
                source=source_name,
                target=self.target_name,
                text=block.original,
                prompt_version=PROMPT_VERSION,
            )
        self.translator = LazyTranslator(provider, source_name, self.target_name)
        self.network_enabled = True

    def _load_cached(self, block: PagerBlock) -> bool:
        if block.cache_checked:
            return False
        block.cache_checked = True
        if not self.cache_enabled or self.refresh_cache or not block.cache_key:
            return False
        cached = self.cache.get(block.cache_key)
        if not cached:
            return False
        translation = sanitize_text(cached["translation"])
        if not translation:
            return False
        block.translation = translation
        block.state = "translated"
        cached_source = cached.get("source_language")
        if isinstance(cached_source, str) and cached_source:
            self.detected_language = self.detected_language or sanitize_text(cached_source)
        self.cache_hits += 1
        return True

    def start(self) -> None:
        if self.started:
            return
        self.started = True
        if self.translator is not None and any(block.state == "waiting" for block in self.blocks):
            self.translator.start()

    def close(self) -> None:
        if self.translator is not None:
            self.translator.close()

    def _submit_group(self, candidates: list[PagerBlock], priority: int) -> bool:
        if self.translator is None:
            return False
        submitted = False
        current: list[PagerBlock] = []
        current_chars = 0
        for block in candidates:
            projected = current_chars + len(block.original)
            if current and (projected > self.lazy_batch_chars or len(current) >= _MAX_LAZY_BATCH_BLOCKS):
                for item in current:
                    item.state = "queued"
                self.translator.submit(current, priority)
                submitted = True
                current = []
                current_chars = 0
            current.append(block)
            current_chars += len(block.original)
        if current:
            for item in current:
                item.state = "queued"
            self.translator.submit(current, priority)
            submitted = True
        return submitted

    def schedule(self, visible: Sequence[str], prefetch: Sequence[str]) -> bool:
        if not self.started or not self.network_enabled or self.translator is None:
            return False
        changed = False
        ordered_identifiers = list(dict.fromkeys([*visible, *prefetch]))
        for identifier in ordered_identifiers:
            block = self.by_identifier.get(identifier)
            if block is not None and block.state == "waiting":
                changed = self._load_cached(block) or changed
        visible_candidates = [
            self.by_identifier[identifier]
            for identifier in visible
            if identifier in self.by_identifier and self.by_identifier[identifier].state == "waiting"
        ]
        visible_set = {block.identifier for block in visible_candidates}
        prefetch_candidates = [
            self.by_identifier[identifier]
            for identifier in prefetch
            if identifier in self.by_identifier
            and identifier not in visible_set
            and self.by_identifier[identifier].state == "waiting"
        ]
        changed = self._submit_group(visible_candidates, 0) or changed
        changed = self._submit_group(prefetch_candidates, 10) or changed
        return changed

    def drain_results(self) -> bool:
        if self.translator is None:
            return False
        changed = False
        while True:
            try:
                result = self.translator.results.get_nowait()
            except queue.Empty:
                break
            changed = True
            self.api_batches += result.api_batches
            if result.error:
                self.status_message = f"翻译警告：{result.error}"
                for identifier in result.identifiers:
                    block = self.by_identifier.get(identifier)
                    if block is not None:
                        block.state = "error"
                if result.fatal:
                    self.network_enabled = False
                    self.translator.close()
                    for block in self.blocks:
                        if block.state == "queued":
                            block.state = "waiting"
                continue
            if result.detected_language:
                self.detected_language = self.detected_language or result.detected_language
            for identifier, translation in result.translations.items():
                block = self.by_identifier.get(identifier)
                if block is None:
                    continue
                block.translation = translation
                block.state = "translated"
                if self.cache_enabled and block.cache_key:
                    try:
                        self.cache.put(
                            block.cache_key,
                            {
                                "translation": translation,
                                "source_language": result.detected_language or self.source_language,
                            },
                        )
                    except CacheError as exc:
                        self.status_message = f"缓存警告：{exc}"
        return changed

    def loading_count(self) -> int:
        return sum(block.state == "queued" for block in self.blocks)


def _document_lines(session: PagerSession, width: int) -> list[VirtualLine]:
    maximum = max(10, width)
    output: list[VirtualLine] = []
    for block in session.blocks:
        original_line = 0
        for raw_line in block.original.splitlines() or [""]:
            for wrapped in _wrap_visual(raw_line, maximum):
                style = "heading" if _is_heading(wrapped) else "original"
                output.append(VirtualLine(wrapped, block.identifier, "original", original_line, style))
                original_line += 1
        if block.translation:
            output.append(VirtualLine(f"【{session.target_name}】", block.identifier, "label", 0, "label"))
            translated_line = 0
            for raw_line in block.translation.splitlines() or [""]:
                for wrapped in _wrap_visual(raw_line, maximum):
                    output.append(
                        VirtualLine(wrapped, block.identifier, "translation", translated_line, "translation")
                    )
                    translated_line += 1
        elif block.state == "queued":
            output.append(VirtualLine("【翻译中…】", block.identifier, "loading", 0, "loading"))
        output.append(VirtualLine("", block.identifier, "separator", 0, "original"))
    if output and output[-1].part == "separator":
        output.pop()
    return output


def _unique_block_ids(lines: Sequence[VirtualLine]) -> list[str]:
    output: list[str] = []
    for line in lines:
        if line.block_identifier not in output:
            output.append(line.block_identifier)
    return output


def _restore_anchor(
    lines: Sequence[VirtualLine], anchor: tuple[str, str, int] | None, previous_top: int
) -> int:
    if not lines:
        return 0
    if anchor is not None:
        for index, line in enumerate(lines):
            if line.anchor == anchor:
                return index
        for index, line in enumerate(lines):
            if line.block_identifier == anchor[0]:
                return index
    return min(previous_top, len(lines) - 1)


def _style_attribute(curses_module: Any, style: str) -> int:
    if style == "heading":
        return curses_module.A_BOLD
    if style == "label":
        return curses_module.A_BOLD | curses_module.color_pair(2)
    if style == "translation":
        return curses_module.color_pair(1)
    if style == "loading":
        return curses_module.A_DIM
    return curses_module.A_NORMAL


def _safe_add(screen: Any, row: int, column: int, text: str, attribute: int, width: int) -> None:
    if width <= 0:
        return
    try:
        screen.addstr(row, column, _clip_visual(text, width), attribute)
    except Exception as exc:
        if exc.__class__.__name__ != "error":
            raise


def _status_text(session: PagerSession, top: int, body_height: int, line_count: int) -> str:
    source = session.detected_language or session.source_language
    position = 100 if line_count <= body_height else int(100 * min(top + body_height, line_count) / line_count)
    details = (
        f" {session.label} | {source} → {session.target_language} | "
        f"缓存 {session.cache_hits} API {session.api_batches} 等待 {session.loading_count()} | {position}% "
    )
    if session.status_message:
        safe_status = sanitize_text(session.status_message).replace("\n", " ")
        details += f"| {safe_status} "
    return sanitize_text(details + "| q退出 /搜索 Space翻页 ").replace("\n", " ")


def _prompt_search(screen: Any, curses_module: Any, width: int, height: int) -> str | None:
    try:
        curses_module.curs_set(1)
    except Exception:
        pass
    screen.timeout(-1)
    curses_module.echo()
    try:
        screen.move(height - 1, 0)
        screen.clrtoeol()
        screen.addstr(height - 1, 0, "/")
        raw = screen.getstr(height - 1, 1, max(1, width - 2))
        encoding = sys.stdin.encoding or "utf-8"
        return raw.decode(encoding, errors="replace") if isinstance(raw, bytes) else str(raw)
    except KeyboardInterrupt:
        raise
    except Exception:
        return None
    finally:
        curses_module.noecho()
        screen.timeout(_POLL_MILLISECONDS)
        try:
            curses_module.curs_set(0)
        except Exception:
            pass


def _find_line(lines: Sequence[VirtualLine], query: str, start: int, direction: int) -> int | None:
    if not query or not lines:
        return None
    needle = query.casefold()
    count = len(lines)
    for distance in range(count):
        index = (start + direction * distance) % count
        if needle in lines[index].text.casefold():
            return index
    return None


def _run_screen(screen: Any, session: PagerSession, curses_module: Any) -> int:
    screen.keypad(True)
    screen.timeout(_POLL_MILLISECONDS)
    try:
        curses_module.curs_set(0)
    except Exception:
        pass
    if curses_module.has_colors():
        try:
            curses_module.use_default_colors()
            curses_module.init_pair(1, curses_module.COLOR_GREEN, -1)
            curses_module.init_pair(2, curses_module.COLOR_CYAN, -1)
        except Exception:
            pass

    top = 0
    lines: list[VirtualLine] = []
    rendered_width = -1
    document_dirty = True
    last_query = ""
    search_direction = 1
    first_frame = True
    try:
        while True:
            height, width = screen.getmaxyx()
            if height < 2 or width < 10:
                raise PagerUnavailable("terminal is too small for the interactive pager")
            body_height = height - 1
            anchor = lines[top].anchor if lines and top < len(lines) else None
            results_changed = session.drain_results()
            if document_dirty or results_changed or width != rendered_width:
                new_lines = _document_lines(session, width)
                top = _restore_anchor(new_lines, anchor, top)
                lines = new_lines
                rendered_width = width
                document_dirty = False
            maximum_top = max(0, len(lines) - body_height)
            top = max(0, min(top, maximum_top))

            screen.erase()
            for row, line in enumerate(lines[top : top + body_height]):
                _safe_add(screen, row, 0, line.text, _style_attribute(curses_module, line.style), width - 1)
            status = _status_text(session, top, body_height, len(lines))
            _safe_add(screen, height - 1, 0, status, curses_module.A_REVERSE, width - 1)
            screen.refresh()

            # This ordering is intentional: no worker, and therefore no HTTP request,
            # exists until the original document has reached the terminal once.
            if first_frame:
                session.start()
                first_frame = False
            visible_lines = lines[top : top + body_height]
            prefetch_lines = lines[top + body_height : top + body_height * 2]
            if session.schedule(
                _unique_block_ids(visible_lines), _unique_block_ids(prefetch_lines)
            ):
                document_dirty = True

            key = screen.getch()
            if key == -1 or key == curses_module.KEY_RESIZE:
                continue
            if key in (ord("q"), ord("Q")):
                return 0
            if key == 3:
                raise KeyboardInterrupt
            if key in (ord("j"), curses_module.KEY_DOWN, 10, 13):
                top += 1
            elif key in (ord("k"), curses_module.KEY_UP):
                top -= 1
            elif key in (ord(" "), curses_module.KEY_NPAGE):
                top += max(1, body_height - 1)
            elif key in (ord("b"), curses_module.KEY_PPAGE):
                top -= max(1, body_height - 1)
            elif key in (ord("g"), curses_module.KEY_HOME):
                top = 0
            elif key in (ord("G"), curses_module.KEY_END):
                top = maximum_top
            elif key == ord("/"):
                query = _prompt_search(screen, curses_module, width, height)
                if query:
                    last_query = query
                    search_direction = 1
                    found = _find_line(lines, query, min(top + 1, len(lines) - 1), 1)
                    if found is not None:
                        top = found
            elif key in (ord("n"), ord("N")) and last_query:
                direction = search_direction if key == ord("n") else -search_direction
                start = (top + direction) % len(lines) if lines else 0
                found = _find_line(lines, last_query, start, direction)
                if found is not None:
                    top = found
            top = max(0, min(top, maximum_top))
    finally:
        session.close()


def _run_curses(session: PagerSession) -> int:
    try:
        import curses
    except ImportError as exc:
        raise PagerUnavailable("Python curses support is not installed") from exc
    try:
        tty_fd = os.open("/dev/tty", os.O_RDWR | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise PagerUnavailable("no controlling terminal is available") from exc
    saved_stdin = -1
    previous_handlers: dict[int, Any] = {}

    def terminate_from_signal(signum: int, _frame: Any) -> None:
        raise _TerminalSignal(signum)

    try:
        saved_stdin = os.dup(0)
        os.dup2(tty_fd, 0)
        for signal_name in ("SIGHUP", "SIGTERM", "SIGQUIT"):
            signum = getattr(signal, signal_name, None)
            if signum is None:
                continue
            try:
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, terminate_from_signal)
            except (OSError, ValueError):
                previous_handlers.pop(signum, None)
        try:
            return int(curses.wrapper(lambda screen: _run_screen(screen, session, curses)))
        except _TerminalSignal as exc:
            return 128 + exc.signum
    except curses.error as exc:
        raise PagerUnavailable(f"cannot initialize terminal pager: {exc}") from exc
    finally:
        for signum, handler in previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except (OSError, ValueError):
                pass
        if saved_stdin >= 0:
            try:
                os.dup2(saved_stdin, 0)
            finally:
                os.close(saved_stdin)
        os.close(tty_fd)


def run_pager(
    args: Any,
    config: dict[str, Any] | None,
    *,
    startup_warning: str = "",
) -> int:
    try:
        raw = _read_input(list(getattr(args, "files", [])))
    except PagerError as exc:
        print(f"bhelp: {exc}", file=sys.stderr)
        return 2
    if not raw:
        return 0
    if not sys.stdout.isatty():
        return _write_original(raw)
    if os.environ.get("TERM", "").lower() in {"", "dumb"}:
        return _display_fallback(raw)
    try:
        text = sanitize_text(decode_text(raw))
    except ValueError:
        return _display_fallback(_safe_terminal_bytes(raw))
    if not text:
        return _display_fallback(_safe_terminal_bytes(raw))
    sanitized_label = sanitize_text(os.environ.get("MAN_PN", "manual page"))
    label = (sanitized_label.splitlines() or ["manual page"])[0][:160] or "manual page"
    effective_config = config if config is not None else copy.deepcopy(DEFAULT_CONFIG)
    session = PagerSession(text, label, args, effective_config, startup_warning)
    try:
        return _run_curses(session)
    except KeyboardInterrupt:
        session.close()
        # A MANPAGER non-zero status is converted by man-db into its own error
        # message/status. Treat an interactive cancel like quitting a pager so
        # `man git` returns cleanly without a misleading failure diagnostic.
        return 0
    except PagerUnavailable:
        session.close()
        return _display_fallback(_safe_terminal_bytes(raw))
