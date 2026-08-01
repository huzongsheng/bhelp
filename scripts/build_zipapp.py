#!/usr/bin/env python3
"""Build a clean bhelp zipapp without local bytecode artifacts."""

from __future__ import annotations

import argparse
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import zipapp
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BYTECODE_SUFFIXES = {".pyc", ".pyo"}


def _ignore_bytecode(_directory: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name == "__pycache__" or Path(name).suffix.lower() in BYTECODE_SUFFIXES
    }


def _bytecode_members(archive: Path) -> list[str]:
    with zipfile.ZipFile(archive) as bundle:
        return [
            name
            for name in bundle.namelist()
            if "__pycache__" in PurePosixPath(name).parts
            or PurePosixPath(name).suffix.lower() in BYTECODE_SUFFIXES
        ]


def build(source: Path, output: Path) -> None:
    source = source.resolve()
    output = output.resolve()
    if not source.is_dir():
        raise SystemExit(f"source directory does not exist: {source}")

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bhelp-build-") as temporary_directory:
        staged_source = Path(temporary_directory) / "src"
        shutil.copytree(source, staged_source, ignore=_ignore_bytecode)

        descriptor, temporary_name = tempfile.mkstemp(prefix=".bhelp-", dir=output.parent)
        os.close(descriptor)
        temporary_output = Path(temporary_name)
        try:
            zipapp.create_archive(
                staged_source,
                target=temporary_output,
                interpreter="/usr/bin/env python3",
                main="bhelp.cli:entrypoint",
            )
            bad_members = _bytecode_members(temporary_output)
            if bad_members:
                raise RuntimeError(f"bytecode unexpectedly included in zipapp: {bad_members}")
            temporary_output.chmod(0o755)
            os.replace(temporary_output, output)
        finally:
            temporary_output.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=PROJECT_ROOT / "src")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "dist" / "bhelp")
    args = parser.parse_args()
    build(args.source, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
