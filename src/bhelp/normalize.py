from __future__ import annotations

import re
import unicodedata


_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_DCS_APC_PM = re.compile(r"\x1b[PX^_].*?\x1b\\", re.DOTALL)
_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ESC_SINGLE = re.compile(r"\x1b[@-_]")
_C1_OSC = re.compile(r"\u009d[^\x07\u009c]*(?:\x07|\u009c)")
_C1_DCS_SOS_PM_APC = re.compile(r"[\u0090\u0098\u009e\u009f].*?\u009c", re.DOTALL)
_C1_CSI = re.compile(r"\u009b[0-?]*[ -/]*[@-~]")
_BIDI = re.compile(r"[\u202a-\u202e\u2066-\u2069]")


def _apply_backspaces(text: str) -> str:
    output: list[str] = []
    for character in text:
        if character == "\b":
            if output and output[-1] != "\n":
                output.pop()
        else:
            output.append(character)
    return "".join(output)


def sanitize_text(text: str) -> str:
    """Remove terminal control sequences while retaining readable plain text."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _OSC.sub("", text)
    text = _DCS_APC_PM.sub("", text)
    text = _CSI.sub("", text)
    text = _ESC_SINGLE.sub("", text)
    text = _C1_OSC.sub("", text)
    text = _C1_DCS_SOS_PM_APC.sub("", text)
    text = _C1_CSI.sub("", text)
    text = _apply_backspaces(text)
    text = _BIDI.sub("", text)
    text = "".join(
        character
        for character in text
        if character in "\n\t" or unicodedata.category(character) != "Cc"
    )
    return "\n".join(line.rstrip() for line in text.split("\n")).strip("\n")


def decode_text(raw: bytes) -> str:
    if b"\x00" in raw:
        raise ValueError("input contains NUL bytes and appears to be binary")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw.decode()
        except UnicodeDecodeError as exc:
            raise ValueError("input is not valid UTF-8 text") from exc
