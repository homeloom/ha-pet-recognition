"""The files of an entry: pictures and crops, written safely and cleaned up.

Every file is named by a random ID of 32 hex characters, checked before any
access, so that no name from storage or from a request can reach another
path. Writes go to a temporary file that is synced to disk, then renamed,
so that a power loss leaves either the old state or the new one.
"""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import Callable, Iterable
from pathlib import Path

# The ID of a file, and the name of the file without its suffix.
FILE_ID = re.compile(r"[0-9a-f]{32}")


def new_id() -> str:
    """A random ID for a file or a record."""
    return secrets.token_hex(16)


def file_path(directory: Path, file_id: str, suffix: str) -> Path:
    """The file of `file_id` in `directory`, for an ID checked to be one."""
    if not FILE_ID.fullmatch(file_id):
        raise ValueError("not the ID of a file")
    return directory / f"{file_id}{suffix}"


def write_files(directory: Path, files: Iterable[tuple[Path, bytes]]) -> None:
    """Write each file atomically, then make the new names durable too."""
    directory.mkdir(parents=True, exist_ok=True)
    for path, content in files:
        temporary = path.with_suffix(".tmp")
        with temporary.open("wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def delete_files(paths: Iterable[Path]) -> None:
    """Delete files, whether or not they still exist."""
    for path in paths:
        path.unlink(missing_ok=True)


def delete_unknown_files(directory: Path, suffix: str, known: Callable[[str], bool]) -> None:
    """Delete the files of `directory` that are not a known ID with `suffix`.

    Such as the temporary file of a write cut short, or the file of a record
    that was removed before a crash.
    """
    if not directory.is_dir():
        return
    for path in directory.iterdir():
        if path.is_file() and (
            path.suffix != suffix or not FILE_ID.fullmatch(path.stem) or not known(path.stem)
        ):
            path.unlink()
