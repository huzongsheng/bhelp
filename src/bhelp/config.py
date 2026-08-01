from __future__ import annotations

import copy
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any


class ConfigError(RuntimeError):
    pass


DEFAULT_CONFIG: dict[str, Any] = {
    "provider": "deepseek",
    "source_language": "auto",
    "target_language": "zh-CN",
    "pager": "auto",
    "color": "auto",
    "command_timeout_seconds": 30,
    "max_input_chars": 250_000,
    "batch_chars": 10_000,
    "timeout_seconds": 90,
    "retries": 2,
    "cache_enabled": True,
    "providers": {
        "deepseek": {
            "api_key": "",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-v4-flash",
        },
        "openai": {
            "api_key": "",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-5.6-luna",
        },
    },
}


def get_config_path(explicit: str | os.PathLike[str] | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    if value := os.environ.get("BHELP_CONFIG"):
        return Path(value).expanduser()
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "bhelp" / "config.json"


def _deep_merge(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _validate_secure_file(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode):
        raise ConfigError(f"refusing symbolic-link config file: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise ConfigError(f"config path is not a regular file: {path}")
    if os.name == "posix":
        if info.st_uid != os.getuid():
            raise ConfigError(f"config file is not owned by the current user: {path}")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ConfigError(f"config permissions are too open; run: chmod 600 {path}")


def _validate_config(data: dict[str, Any]) -> None:
    provider = data.get("provider")
    providers = data.get("providers")
    if not isinstance(provider, str) or not provider:
        raise ConfigError("config field 'provider' must be a non-empty string")
    if not isinstance(providers, dict) or provider not in providers:
        raise ConfigError(f"provider '{provider}' has no entry under 'providers'")
    for key in ("command_timeout_seconds", "max_input_chars", "batch_chars", "timeout_seconds"):
        if not isinstance(data.get(key), (int, float)) or data[key] <= 0:
            raise ConfigError(f"config field '{key}' must be greater than zero")
    if not isinstance(data.get("retries"), int) or not 0 <= data["retries"] <= 8:
        raise ConfigError("config field 'retries' must be an integer from 0 to 8")


def load_config(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    config_path = get_config_path(path)
    data = copy.deepcopy(DEFAULT_CONFIG)
    if config_path.exists() or config_path.is_symlink():
        _validate_secure_file(config_path)
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"cannot read config {config_path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ConfigError("config root must be a JSON object")
        _deep_merge(data, loaded)
    _validate_config(data)
    return data


def write_config(data: dict[str, Any], path: str | os.PathLike[str] | None = None) -> Path:
    config_path = get_config_path(path)
    _validate_config(_deep_merge(copy.deepcopy(DEFAULT_CONFIG), copy.deepcopy(data)))
    if config_path.is_symlink():
        raise ConfigError(f"refusing to replace symbolic-link config file: {config_path}")
    parent = config_path.parent
    parent_existed = parent.exists()
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix" and not parent_existed:
        os.chmod(parent, 0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=parent)
    temporary_path = Path(temporary_name)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, config_path)
        if os.name == "posix":
            os.chmod(config_path, 0o600)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
    return config_path


def initialize_config(path: str | os.PathLike[str] | None = None, force: bool = False) -> Path:
    config_path = get_config_path(path)
    if (config_path.exists() or config_path.is_symlink()) and not force:
        raise ConfigError(f"config already exists: {config_path}")
    return write_config(copy.deepcopy(DEFAULT_CONFIG), config_path)


def set_api_key(provider: str, api_key: str, path: str | os.PathLike[str] | None = None) -> Path:
    normalized_key = api_key.lstrip("\ufeff").strip()
    if not normalized_key:
        raise ConfigError("API key must not be empty")
    if not normalized_key.isascii() or any(ord(character) < 33 or ord(character) > 126 for character in normalized_key):
        raise ConfigError("API key must contain only printable ASCII characters and no spaces")
    config = load_config(path)
    if provider not in config["providers"]:
        raise ConfigError(f"unknown provider: {provider}")
    config["providers"][provider]["api_key"] = normalized_key
    return write_config(config, path)


def redacted_config(config: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(config)
    for provider in result.get("providers", {}).values():
        if isinstance(provider, dict) and "api_key" in provider:
            provider["api_key"] = "<configured>" if provider["api_key"] else "<not set>"
    return result
