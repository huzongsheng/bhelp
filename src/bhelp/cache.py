from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any


class CacheError(RuntimeError):
    pass


def get_cache_dir() -> Path:
    root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return root / "bhelp" / "v2"


def make_cache_key(
    *,
    provider: str,
    base_url: str,
    model: str,
    source: str,
    target: str,
    text: str,
    prompt_version: str,
) -> str:
    material = json.dumps(
        {
            "provider": provider,
            "base_url": base_url,
            "model": model,
            "source": source,
            "target": target,
            "text": text,
            "prompt_version": prompt_version,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


class TranslationCache:
    def __init__(self, root: Path | None = None):
        self.root = root or get_cache_dir()

    def _ensure_root(self) -> None:
        if self.root.is_symlink():
            raise CacheError(f"refusing symbolic-link cache directory: {self.root}")
        try:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not self.root.is_dir():
                raise CacheError(f"cache path is not a directory: {self.root}")
            if os.name == "posix":
                os.chmod(self.root, 0o700)
        except OSError as exc:
            raise CacheError(f"cannot prepare cache directory {self.root}: {exc}") from exc

    def _path(self, key: str) -> Path:
        if len(key) != 64 or any(character not in "0123456789abcdef" for character in key):
            raise CacheError("invalid cache key")
        return self.root / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("translation"), str):
            return None
        return data

    def put(self, key: str, data: dict[str, Any]) -> None:
        try:
            self._ensure_root()
            path = self._path(key)
            if path.is_symlink():
                raise CacheError(f"refusing symbolic-link cache file: {path}")
            fd, temporary_name = tempfile.mkstemp(prefix=".cache.", suffix=".tmp", dir=self.root)
            temporary_path = Path(temporary_name)
            try:
                if os.name == "posix":
                    os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_path, path)
                if os.name == "posix":
                    os.chmod(path, 0o600)
            finally:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass
        except CacheError:
            raise
        except OSError as exc:
            raise CacheError(f"cannot write translation cache: {exc}") from exc

    def stats(self) -> tuple[int, int]:
        if not self.root.exists() or self.root.is_symlink():
            return 0, 0
        files = [path for path in self.root.iterdir() if path.is_file() and not path.is_symlink()]
        return len(files), sum(path.stat().st_size for path in files)

    def clear(self) -> tuple[int, int]:
        if not self.root.exists() or self.root.is_symlink():
            return 0, 0
        count = 0
        size = 0
        for path in self.root.iterdir():
            if path.is_file() and not path.is_symlink():
                info = path.stat()
                path.unlink()
                count += 1
                size += info.st_size
        return count, size
