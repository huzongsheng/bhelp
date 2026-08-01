from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unicodedata
from typing import Iterable

from .translate import TextPair, language_name


_CYAN_BOLD = "\x1b[1;36m"
_GREEN = "\x1b[32m"
_RESET = "\x1b[0m"


def use_color(mode: str) -> bool:
    if mode == "always":
        return True
    if mode == "never" or "NO_COLOR" in os.environ:
        return False
    return sys.stdout.isatty()


def _character_width(character: str) -> int:
    if unicodedata.combining(character):
        return 0
    return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1


def _display_width(text: str) -> int:
    return sum(_character_width(character) for character in text)


def _wrap_line(line: str, width: int) -> list[str]:
    if not line or _display_width(line) <= width:
        return [line]
    indent = line[: len(line) - len(line.lstrip())]
    content = line[len(indent) :]
    available = max(20, width - _display_width(indent))
    output: list[str] = []
    current = ""
    current_width = 0
    last_break = -1
    for character in content:
        character_width = _character_width(character)
        if current and current_width + character_width > available:
            split_at = last_break + 1 if last_break >= max(0, len(current) // 2) else len(current)
            output.append(indent + current[:split_at].rstrip())
            current = current[split_at:].lstrip()
            current_width = _display_width(current)
            last_break = -1
        current += character
        current_width += character_width
        if character.isspace() or unicodedata.east_asian_width(character) in {"W", "F"}:
            last_break = len(current) - 1
    if current or not output:
        output.append(indent + current.rstrip())
    return output


def _wrap_translation(text: str, width: int) -> str:
    lines: list[str] = []
    for line in text.splitlines() or [""]:
        lines.extend(_wrap_line(line, width))
    return "\n".join(lines)


def render_bilingual(
    pairs: Iterable[TextPair], target_language: str, *, color: bool, width: int | None = None
) -> str:
    terminal_width = width or shutil.get_terminal_size((100, 24)).columns
    label = language_name(target_language)
    sections: list[str] = []
    for pair in pairs:
        section = pair.original.rstrip()
        if pair.translation:
            translated = _wrap_translation(pair.translation.rstrip(), terminal_width)
            if color:
                marker = f"{_CYAN_BOLD}【{label}】{_RESET}"
                translated = f"{_GREEN}{translated}{_RESET}"
            else:
                marker = f"【{label}】"
            section += f"\n{marker}\n{translated}"
        sections.append(section)
    return "\n\n".join(sections).rstrip() + ("\n" if sections else "")


def display_output(text: str, pager_mode: str) -> None:
    should_page = pager_mode == "always"
    if pager_mode == "auto" and sys.stdout.isatty():
        rows = shutil.get_terminal_size((100, 24)).lines
        should_page = text.count("\n") >= max(5, rows - 2)
    less = shutil.which("less")
    if should_page and less:
        environment = os.environ.copy()
        environment.setdefault("LESSCHARSET", "utf-8")
        command = [less, "-R"]
        if os.environ.get("BHELP_PAGER"):
            import shlex

            command = shlex.split(os.environ["BHELP_PAGER"])
        try:
            subprocess.run(command, input=text, text=True, env=environment, check=False)
            return
        except (FileNotFoundError, OSError):
            pass
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except OSError:
            pass

