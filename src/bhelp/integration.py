from __future__ import annotations

import subprocess
import shlex
from dataclasses import dataclass
from typing import Any

from .config import ConfigError, load_config, write_config


class IntegrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitIntegrationStatus:
    installed: bool
    viewers: list[str]
    command: str | None
    help_formats: list[str]


def _git(arguments: list[str], *, allow_missing: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *arguments], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
        )
    except FileNotFoundError as exc:
        raise IntegrationError("git is not installed") from exc
    if result.returncode and not allow_missing:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise IntegrationError(f"git config failed: {detail}")
    return result


def _get_all(key: str) -> list[str]:
    result = _git(["config", "--global", "--get-all", key], allow_missing=True)
    if result.returncode not in {0, 1}:
        raise IntegrationError(result.stderr.strip() or f"git config exited {result.returncode}")
    return [line for line in result.stdout.splitlines() if line]


def _get_one(key: str) -> str | None:
    values = _get_all(key)
    return values[-1] if values else None


def git_status() -> GitIntegrationStatus:
    viewers = _get_all("man.viewer")
    command = _get_one("man.bhelp.cmd")
    help_formats = _get_all("help.format")
    valid_command = False
    if command:
        try:
            parts = shlex.split(command)
            valid_command = (
                len(parts) in {2, 4}
                and parts[1] == "man"
                and parts[0].rsplit("/", 1)[-1] == "bhelp"
                and (len(parts) == 2 or parts[2] == "--config")
            )
        except ValueError:
            valid_command = False
    return GitIntegrationStatus(
        "bhelp" in viewers and valid_command and help_formats == ["man"],
        viewers,
        command,
        help_formats,
    )


def _replace_all(key: str, values: list[str]) -> None:
    _git(["config", "--global", "--unset-all", key], allow_missing=True)
    for value in values:
        _git(["config", "--global", "--add", key, value])


def install_git_integration(
    config_path: str | None = None, *, viewer_command: str = "bhelp man"
) -> GitIntegrationStatus:
    current = git_status()
    if current.installed:
        if current.command == viewer_command:
            return current
        raise IntegrationError("Git integration is already installed with a different bhelp command; remove it first")
    config = load_config(config_path)
    state: dict[str, Any] = config.setdefault("integration_state", {})
    state["git"] = {
        "previous_man_viewer": current.viewers,
        "previous_man_bhelp_cmd": current.command,
        "previous_help_format": current.help_formats,
        "installed_man_bhelp_cmd": viewer_command,
    }
    write_config(config, config_path)
    try:
        _replace_all("man.viewer", ["bhelp"])
        _git(["config", "--global", "man.bhelp.cmd", viewer_command])
        _replace_all("help.format", ["man"])
    except IntegrationError:
        _replace_all("man.viewer", current.viewers)
        if current.command is None:
            _git(["config", "--global", "--unset-all", "man.bhelp.cmd"], allow_missing=True)
        else:
            _git(["config", "--global", "man.bhelp.cmd", current.command])
        _replace_all("help.format", current.help_formats)
        raise
    return git_status()


def remove_git_integration(config_path: str | None = None) -> GitIntegrationStatus:
    config = load_config(config_path)
    state = config.get("integration_state", {}).get("git", {})
    current = git_status()
    previous_viewers = state.get("previous_man_viewer", [])
    previous_command = state.get("previous_man_bhelp_cmd")
    previous_help_formats = state.get("previous_help_format", [])
    installed_command = state.get("installed_man_bhelp_cmd", "bhelp man")
    if not isinstance(previous_viewers, list) or any(not isinstance(item, str) for item in previous_viewers):
        raise ConfigError("saved Git integration state is invalid")
    if not isinstance(previous_help_formats, list) or any(
        not isinstance(item, str) for item in previous_help_formats
    ):
        raise ConfigError("saved Git help.format state is invalid")
    if "bhelp" in current.viewers:
        new_values = list(previous_viewers)
        for value in current.viewers:
            if value != "bhelp" and value not in new_values:
                new_values.append(value)
        _replace_all("man.viewer", new_values)
    if current.command == installed_command:
        if isinstance(previous_command, str) and previous_command:
            _git(["config", "--global", "man.bhelp.cmd", previous_command])
        else:
            _git(["config", "--global", "--unset-all", "man.bhelp.cmd"], allow_missing=True)
    if current.help_formats == ["man"]:
        _replace_all("help.format", previous_help_formats)
    if "integration_state" in config and "git" in config["integration_state"]:
        del config["integration_state"]["git"]
        if not config["integration_state"]:
            del config["integration_state"]
        write_config(config, config_path)
    return git_status()
