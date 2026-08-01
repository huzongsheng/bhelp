from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Sequence

from .normalize import decode_text, sanitize_text


_SECRET_ENV_VARS = {
    "BHELP_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "BHELP_CONFIG",
}


@dataclass(frozen=True)
class CaptureResult:
    text: str
    returncode: int
    label: str
    too_large: bool = False


class CaptureFailure(RuntimeError):
    def __init__(self, message: str, returncode: int = 2):
        super().__init__(message)
        self.returncode = returncode


def _child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in _SECRET_ENV_VARS:
        environment.pop(name, None)
    environment.update(
        {
            "PAGER": "cat",
            "GIT_PAGER": "cat",
            "MANPAGER": "cat",
            "TERM": "dumb",
            "NO_COLOR": "1",
        }
    )
    environment.pop("MAN_KEEP_FORMATTING", None)
    return environment


def capture_command(
    argv: Sequence[str], timeout_seconds: float, max_input_chars: int
) -> CaptureResult:
    if not argv:
        raise CaptureFailure("no command was supplied", 2)
    maximum_bytes = max_input_chars * 4 + 1
    try:
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                env=_child_environment(),
            )
            deadline = time.monotonic() + timeout_seconds
            too_large = False
            timed_out = False
            try:
                while True:
                    if os.fstat(output.fileno()).st_size >= maximum_bytes:
                        too_large = True
                        _stop_process(process)
                        break
                    if process.poll() is not None:
                        break
                    if time.monotonic() >= deadline:
                        timed_out = True
                        _stop_process(process)
                        break
                    time.sleep(0.01)
            except KeyboardInterrupt:
                _stop_process(process)
                raise
            output.seek(0)
            raw = output.read(maximum_bytes)
    except FileNotFoundError as exc:
        raise CaptureFailure(f"command not found: {argv[0]}", 127) from exc
    except PermissionError as exc:
        raise CaptureFailure(f"permission denied: {argv[0]}", 126) from exc
    if timed_out:
        partial = sanitize_text(raw.decode("utf-8", errors="replace")) if raw else ""
        detail = f"command timed out after {timeout_seconds:g} seconds"
        if partial:
            detail += f"\n\n{partial}"
        raise CaptureFailure(detail, 124)
    try:
        text = sanitize_text(raw.decode("utf-8", errors="replace") if too_large else decode_text(raw))
    except ValueError as exc:
        raise CaptureFailure(str(exc), process.returncode or 2) from exc
    too_large = too_large or len(text) > max_input_chars
    if len(text) > max_input_chars:
        text = text[:max_input_chars].rstrip() + "\n\n[… input truncated by bhelp …]"
    elif too_large:
        text = text.rstrip() + "\n\n[… input truncated by bhelp …]"
    return CaptureResult(
        text=text,
        returncode=125 if too_large else int(process.returncode or 0),
        label=" ".join(argv),
        too_large=too_large,
    )


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)


def capture_man(
    pages: Sequence[str], timeout_seconds: float, max_input_chars: int
) -> CaptureResult:
    if not pages:
        raise CaptureFailure("no man page was supplied", 2)
    if not shutil.which("man"):
        raise CaptureFailure("the 'man' command is not installed", 127)
    command = [
        "man",
        "--no-hyphenation",
        "--no-justification",
        "--pager=cat",
        *pages,
    ]
    result = capture_command(command, timeout_seconds, max_input_chars)
    return CaptureResult(result.text, result.returncode, f"man {' '.join(pages)}", result.too_large)


def capture_stdin(max_input_chars: int) -> CaptureResult:
    # Four bytes per character covers UTF-8 while bounding unexpectedly large pipes.
    maximum_bytes = max_input_chars * 4 + 1
    raw = sys.stdin.buffer.read(maximum_bytes)
    too_large = len(raw) == maximum_bytes
    try:
        text = sanitize_text(decode_text(raw[: maximum_bytes - 1] if too_large else raw))
    except ValueError as exc:
        raise CaptureFailure(str(exc), 2) from exc
    if len(text) > max_input_chars:
        too_large = True
        text = text[:max_input_chars].rstrip()
    if too_large:
        text += "\n\n[… input truncated by bhelp …]"
    return CaptureResult(text=text, returncode=0, label="stdin", too_large=too_large)
