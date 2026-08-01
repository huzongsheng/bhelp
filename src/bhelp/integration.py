from __future__ import annotations

import copy
import hashlib
import os
import subprocess
import shlex
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ConfigError, get_config_path, load_config, write_config


class IntegrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitIntegrationStatus:
    installed: bool
    viewers: list[str]
    command: str | None
    help_formats: list[str]


@dataclass(frozen=True)
class ManIntegrationStatus:
    installed: bool
    rc_path: str
    command: str | None
    modified: bool
    backup_path: str | None
    migrated_legacy_git: bool = False


_MAN_MARKER_START = "# >>> bhelp MANPAGER integration >>>"
_MAN_MARKER_END = "# <<< bhelp MANPAGER integration <<<"
_MAN_STATE_VERSION = 1


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


def _is_bhelp_man_command(command: str | None) -> bool:
    if not command:
        return False
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    return (
        len(parts) in {2, 4}
        and parts[1] == "man"
        and parts[0].rsplit("/", 1)[-1] == "bhelp"
        and (len(parts) == 2 or parts[2] == "--config")
    )


def git_status() -> GitIntegrationStatus:
    viewers = _get_all("man.viewer")
    command = _get_one("man.bhelp.cmd")
    help_formats = _get_all("help.format")
    return GitIntegrationStatus(
        "bhelp" in viewers and _is_bhelp_man_command(command) and help_formats == ["man"],
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
    integration_state = config.get("integration_state", {})
    if not isinstance(integration_state, dict):
        raise ConfigError("config field 'integration_state' must be an object")
    state = integration_state.get("git")
    current = git_status()
    # Without installation state we cannot distinguish our old settings from a
    # user's unrelated custom viewer named "bhelp".  Preserve Git config.
    if state is None:
        return current
    if not isinstance(state, dict):
        raise ConfigError("saved Git integration state is invalid")
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
    if "git" in integration_state:
        del integration_state["git"]
        if not integration_state:
            del config["integration_state"]
        write_config(config, config_path)
    return git_status()


def _resolved_rc_path(rc_path: str | os.PathLike[str] | None) -> Path:
    path = Path(rc_path).expanduser() if rc_path is not None else Path.home() / ".bashrc"
    # Keep the final path component unresolved so _read_rc/_atomic_write_text can
    # reject a symbolic-link rc file instead of silently following it.
    return Path(os.path.abspath(path))


def _read_rc(path: Path) -> str:
    if path.is_symlink():
        raise IntegrationError(f"refusing symbolic-link shell rc file: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError) as exc:
        raise IntegrationError(f"cannot read shell rc file {path}: {exc}") from exc


def _atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    if path.is_symlink():
        raise IntegrationError(f"refusing symbolic-link file: {path}")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        existing_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary_path = Path(temporary_name)
        try:
            if os.name == "posix":
                os.fchmod(fd, existing_mode if mode is None else mode)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            if os.name == "posix" and mode is not None:
                os.chmod(path, mode)
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
    except IntegrationError:
        raise
    except OSError as exc:
        raise IntegrationError(f"cannot write file {path}: {exc}") from exc


def _create_rc_backup(path: Path, original: str) -> Path:
    base = path.with_name(f"{path.name}.bhelp-backup")
    candidate = base
    suffix = 0
    while candidate.exists() or candidate.is_symlink():
        if candidate.is_symlink():
            suffix += 1
            candidate = base.with_name(f"{base.name}.{suffix}")
            continue
        try:
            info = candidate.stat()
            owned_regular_file = stat.S_ISREG(info.st_mode) and (
                os.name != "posix" or info.st_uid == os.getuid()
            )
            if owned_regular_file and candidate.read_text(encoding="utf-8") == original:
                if os.name == "posix":
                    os.chmod(candidate, 0o600)
                return candidate
        except (OSError, UnicodeError):
            pass
        suffix += 1
        candidate = base.with_name(f"{base.name}.{suffix}")
    _atomic_write_text(candidate, original, mode=0o600)
    return candidate


def _render_man_block(command: str) -> str:
    return (
        f"{_MAN_MARKER_START}\n"
        "# Managed by bhelp. Use bhelp's remove command instead of editing this block.\n"
        f"export MANPAGER={shlex.quote(command)}\n"
        f"{_MAN_MARKER_END}\n"
    )


def _man_state(config: dict[str, Any]) -> dict[str, Any]:
    integration_state = config.get("integration_state", {})
    if not isinstance(integration_state, dict):
        return {}
    state = integration_state.get("man", {})
    return state if isinstance(state, dict) else {}


def _marker_counts(text: str) -> tuple[int, int]:
    return text.count(_MAN_MARKER_START), text.count(_MAN_MARKER_END)


def _status_from(config: dict[str, Any], rc_path: Path, text: str) -> ManIntegrationStatus:
    state = _man_state(config)
    saved_rc_path = state.get("rc_path")
    block = state.get("installed_block")
    command = state.get("installed_manpager_command")
    backup_path = state.get("backup_path")
    starts, ends = _marker_counts(text)
    same_path = isinstance(saved_rc_path, str) and Path(saved_rc_path) == rc_path
    exact_block = isinstance(block, str) and block != "" and text.count(block) == 1
    markers_valid = starts == 1 and ends == 1
    installed = same_path and exact_block and markers_valid and isinstance(command, str)
    marker_present = starts != 0 or ends != 0
    modified = marker_present and not installed
    return ManIntegrationStatus(
        installed=installed,
        rc_path=str(rc_path),
        command=command if installed else None,
        modified=modified,
        backup_path=backup_path if isinstance(backup_path, str) else None,
    )


def man_status(
    config_path: str | os.PathLike[str] | None = None,
    *,
    rc_path: str | os.PathLike[str] | None = None,
) -> ManIntegrationStatus:
    resolved_rc = _resolved_rc_path(rc_path)
    config = load_config(config_path)
    return _status_from(config, resolved_rc, _read_rc(resolved_rc))


def _validate_saved_git_values(state: dict[str, Any]) -> tuple[list[str], str | None, list[str]] | None:
    previous_viewers = state.get("previous_man_viewer", [])
    previous_command = state.get("previous_man_bhelp_cmd")
    previous_help_formats = state.get("previous_help_format", [])
    if not isinstance(previous_viewers, list) or any(not isinstance(item, str) for item in previous_viewers):
        return None
    if previous_command is not None and not isinstance(previous_command, str):
        return None
    if not isinstance(previous_help_formats, list) or any(
        not isinstance(item, str) for item in previous_help_formats
    ):
        return None
    return previous_viewers, previous_command, previous_help_formats


def migrate_legacy_git_integration(config_path: str | os.PathLike[str] | None = None) -> bool:
    """Restore Git settings only when they still exactly match bhelp's saved installation."""

    config = load_config(config_path)
    integration_state = config.get("integration_state", {})
    if not isinstance(integration_state, dict):
        return False
    state = integration_state.get("git")
    if not isinstance(state, dict):
        return False
    installed_command = state.get("installed_man_bhelp_cmd")
    saved = _validate_saved_git_values(state)
    if saved is None or not isinstance(installed_command, str) or not _is_bhelp_man_command(installed_command):
        return False
    try:
        current = git_status()
    except IntegrationError:
        return False
    if not (
        current.viewers == ["bhelp"]
        and current.command == installed_command
        and current.help_formats == ["man"]
    ):
        return False

    previous_viewers, previous_command, previous_help_formats = saved
    try:
        _replace_all("man.viewer", previous_viewers)
        if previous_command:
            _git(["config", "--global", "man.bhelp.cmd", previous_command])
        else:
            _git(["config", "--global", "--unset-all", "man.bhelp.cmd"], allow_missing=True)
        _replace_all("help.format", previous_help_formats)
    except IntegrationError:
        try:
            _replace_all("man.viewer", current.viewers)
            if current.command:
                _git(["config", "--global", "man.bhelp.cmd", current.command])
            else:
                _git(["config", "--global", "--unset-all", "man.bhelp.cmd"], allow_missing=True)
            _replace_all("help.format", current.help_formats)
        except IntegrationError:
            pass
        raise

    del integration_state["git"]
    if not integration_state:
        del config["integration_state"]
    write_config(config, config_path)
    return True


def install_man_integration(
    bhelp_path: str | os.PathLike[str],
    config_path: str | os.PathLike[str] | None = None,
    *,
    rc_path: str | os.PathLike[str] | None = None,
) -> ManIntegrationStatus:
    resolved_rc = _resolved_rc_path(rc_path)
    executable = Path(bhelp_path).expanduser().resolve(strict=False)
    executable_text = str(executable)
    if "\n" in executable_text or "\r" in executable_text or "\x00" in executable_text:
        raise IntegrationError("bhelp executable path contains an unsafe character")
    pager_command = f"{shlex.quote(executable_text)} pager"
    if config_path is not None or os.environ.get("BHELP_CONFIG"):
        resolved_config = get_config_path(config_path).expanduser().resolve(strict=False)
        pager_command += f" --config {shlex.quote(str(resolved_config))}"
    block = _render_man_block(pager_command)

    config = load_config(config_path)
    text = _read_rc(resolved_rc)
    current = _status_from(config, resolved_rc, text)
    if current.installed:
        if current.command != pager_command:
            raise IntegrationError("man integration is already installed with a different bhelp executable")
        migrated = migrate_legacy_git_integration(config_path)
        refreshed = man_status(config_path, rc_path=resolved_rc)
        return ManIntegrationStatus(**{**refreshed.__dict__, "migrated_legacy_git": migrated})
    state = _man_state(config)
    saved_rc_path = state.get("rc_path")
    if isinstance(saved_rc_path, str) and Path(saved_rc_path) != resolved_rc:
        raise IntegrationError(f"man integration state belongs to a different shell rc file: {saved_rc_path}")
    starts, ends = _marker_counts(text)
    if starts or ends:
        raise IntegrationError("bhelp MANPAGER markers already exist but are modified or unmanaged; refusing to overwrite")

    backup_path = _create_rc_backup(resolved_rc, text)
    separator = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    insertion = f"{separator}{block}"
    rc_existed = resolved_rc.exists()
    original_config = copy.deepcopy(config)
    integration_state = config.setdefault("integration_state", {})
    if not isinstance(integration_state, dict):
        raise ConfigError("config field 'integration_state' must be an object")
    integration_state["man"] = {
        "version": _MAN_STATE_VERSION,
        "rc_path": str(resolved_rc),
        "rc_existed": rc_existed,
        "previous_rc_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "backup_path": str(backup_path),
        "installed_block": block,
        "installed_insertion": insertion,
        "installed_manpager_command": pager_command,
    }
    write_config(config, config_path)
    try:
        _atomic_write_text(resolved_rc, f"{text}{insertion}")
    except IntegrationError:
        try:
            write_config(original_config, config_path)
        except (ConfigError, OSError):
            pass
        raise

    migrated = migrate_legacy_git_integration(config_path)
    installed = man_status(config_path, rc_path=resolved_rc)
    return ManIntegrationStatus(**{**installed.__dict__, "migrated_legacy_git": migrated})


def remove_man_integration(
    config_path: str | os.PathLike[str] | None = None,
    *,
    rc_path: str | os.PathLike[str] | None = None,
) -> ManIntegrationStatus:
    resolved_rc = _resolved_rc_path(rc_path)
    config = load_config(config_path)
    state = _man_state(config)
    saved_rc_path = state.get("rc_path")
    if isinstance(saved_rc_path, str) and Path(saved_rc_path) != resolved_rc:
        raise IntegrationError(f"man integration state belongs to a different shell rc file: {saved_rc_path}")
    text = _read_rc(resolved_rc)
    block = state.get("installed_block")
    insertion = state.get("installed_insertion")
    starts, ends = _marker_counts(text)

    if not state:
        if starts or ends:
            raise IntegrationError("unmanaged bhelp MANPAGER markers found; refusing to remove them")
        return _status_from(config, resolved_rc, text)
    exact_block = isinstance(block, str) and block != "" and text.count(block) == 1
    if starts or ends:
        if starts != 1 or ends != 1 or not exact_block:
            raise IntegrationError("managed bhelp MANPAGER block was modified; refusing to remove it")
        if isinstance(insertion, str) and insertion and text.count(insertion) == 1:
            updated = text.replace(insertion, "", 1)
        else:
            updated = text.replace(block, "", 1)
        originally_existed = state.get("rc_existed") is True
        try:
            if not originally_existed and updated == "" and resolved_rc.exists():
                resolved_rc.unlink()
            else:
                _atomic_write_text(resolved_rc, updated)
        except OSError as exc:
            raise IntegrationError(f"cannot restore shell rc file {resolved_rc}: {exc}") from exc

    integration_state = config.get("integration_state", {})
    if isinstance(integration_state, dict) and "man" in integration_state:
        del integration_state["man"]
        if not integration_state:
            del config["integration_state"]
        write_config(config, config_path)
    return man_status(config_path, rc_path=resolved_rc)
