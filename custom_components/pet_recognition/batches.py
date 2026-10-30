"""Several photos of a pet at once: a folder of the media, or a zip archive.

Home Assistant's file field takes one file, but its media browser uploads
several at once. So a folder of the media directories can be chosen, and a
zip archive uploaded in the photo field. Both become a batch: its photos
are read one by one, outside the event loop, when they are learned, and
never moved or deleted.

A folder is listed only under the media directories, a few levels deep,
and symbolic links are never followed. A zip archive is read entry by
entry, in memory, never extracted to a path it names. Its folders, hidden
files and macOS metadata are skipped, the size of each entry and of the
whole is bounded, and a photo is told by its content, not its extension.
"""

from __future__ import annotations

import zipfile
import zlib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path, PurePosixPath

from .pets import PictureError

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
# How deep folders are looked for, and how many are offered.
FOLDER_DEPTH = 3
FOLDER_LIMIT = 100
# The most entries of an archive, and the most bytes read from it.
ARCHIVE_ENTRIES = 1000
ARCHIVE_BYTES = 256 * 1024 * 1024
# Why a batch stops: what follows is not tried.
ARCHIVE_SPENT = "archive_too_large"
STOPS = frozenset({"cannot_connect", ARCHIVE_SPENT})
# The most names a summary shows per group.
SHOWN_NAMES = 30

# What a summary says of each reason a photo was not added.
GROUPS = {
    "duplicate_picture": "duplicate",
    "no_animal": "no_animal",
    "several_animals": "no_animal",
    "not_image": "unreadable",
    "picture_too_large": "unreadable",
    "unreadable_picture": "unreadable",
    "archive_too_large": "unreadable",
}
NOT_ADDED = "not_added"


@dataclass
class Item:
    """A photo of a batch, by the name shown, and how to read it."""

    name: str
    read: Callable[[], bytes]


@dataclass
class Batch:
    """The photos to learn, and what to close once they are."""

    items: list[Item]
    close: Callable[[], None] = field(default=lambda: None)


@dataclass
class Summary:
    """What became of the photos of a batch, by name."""

    added: list[str] = field(default_factory=list)
    skipped: dict[str, list[str]] = field(default_factory=dict)

    def skip(self, name: str, reason: str) -> None:
        """Count a photo not added, for `reason`."""
        self.skipped.setdefault(GROUPS.get(reason, NOT_ADDED), []).append(name)

    def placeholders(self) -> dict[str, str]:
        """The counts and names of each group, for the summary's description."""
        groups = {"added": self.added} | {
            group: self.skipped.get(group, [])
            for group in ("duplicate", "no_animal", "unreadable", NOT_ADDED)
        }
        found = {}
        for group, names in groups.items():
            found[group] = str(len(names))
            shown = [_escape(name) for name in names[:SHOWN_NAMES]]
            if len(names) > SHOWN_NAMES:
                shown.append("…")
            found[f"{group}_names"] = ", ".join(shown) or "-"
        return found


def is_image(data: bytes) -> bool:
    """Whether `data` starts as a JPEG, PNG or WebP picture."""
    return (
        data.startswith(b"\xff\xd8\xff")
        or data.startswith(b"\x89PNG\r\n\x1a\n")
        or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")
    )


def media_folders(media_dirs: Mapping[str, str]) -> dict[str, str]:
    """The folders of the media directories that hold photos, as select
    options: `<media directory ID>/<relative path>` to a label."""
    found: dict[str, str] = {}
    for dir_id, base in media_dirs.items():
        root = Path(base)
        for folder, count in _folders(root):
            relative = folder.relative_to(root).as_posix()
            label = "/" if relative == "." else relative
            if len(media_dirs) > 1:
                label = f"{dir_id}: {label}"
            found[f"{dir_id}/{relative}"] = f"{label} ({count})"
            if len(found) == FOLDER_LIMIT:
                return found
    return found


def folder_batch(media_dirs: Mapping[str, str], value: str, max_bytes: int) -> Batch:
    """The photos of a folder that `media_folders` offered, by file name."""
    dir_id, _, relative = value.partition("/")
    if dir_id not in media_dirs:
        raise PictureError("folder_not_found")
    root = Path(media_dirs[dir_id]).resolve()
    folder = (root / relative).resolve()
    if not folder.is_relative_to(root) or not folder.is_dir():
        raise PictureError("folder_not_found")
    return Batch(
        [Item(path.name, partial(_read_file, path, max_bytes)) for path in _photos(folder)]
    )


def archive_batch(archive: zipfile.ZipFile, max_bytes: int) -> Batch:
    """The entries of a zip archive, by file name, read from the open archive,
    which the batch's `close` must then close."""
    entries = [
        info for info in archive.infolist() if not info.is_dir() and not _hidden(info.filename)
    ]
    if len(entries) > ARCHIVE_ENTRIES:
        raise PictureError("archive_too_large")
    reader = _ArchiveReader(archive, max_bytes)
    return Batch(
        [Item(PurePosixPath(info.filename).name, partial(reader.read, info)) for info in entries]
    )


class _ArchiveReader:
    """Reads the entries of an archive, within the bytes it may read in all.

    Once they are spent, no other entry is opened, and the batch stops.
    """

    def __init__(self, archive: zipfile.ZipFile, max_bytes: int) -> None:
        self._archive = archive
        self._max_bytes = max_bytes
        self._read = 0

    def read(self, info: zipfile.ZipInfo) -> bytes:
        if info.file_size > self._max_bytes:
            raise PictureError("picture_too_large")
        if (left := ARCHIVE_BYTES - self._read) <= 0:
            raise PictureError(ARCHIVE_SPENT)
        try:
            with self._archive.open(info) as file:
                data = file.read(min(self._max_bytes, left) + 1)
        except (zipfile.BadZipFile, zlib.error, RuntimeError, NotImplementedError, OSError) as err:
            raise PictureError("unreadable_picture") from err
        self._read += len(data)
        if len(data) > left:
            raise PictureError(ARCHIVE_SPENT)
        return _checked(data, self._max_bytes)


def _folders(root: Path) -> Iterator[tuple[Path, int]]:
    """The folders under `root`, itself included, with their number of photos."""
    pending = [(root, 0)]
    while pending:
        folder, depth = pending.pop()
        try:
            entries = sorted(entry for entry in folder.iterdir() if _visible(entry))
            count = sum(1 for entry in entries if _is_photo(entry))
            subfolders = [entry for entry in entries if entry.is_dir()]
        except OSError:
            continue
        if count:
            yield folder, count
        if depth < FOLDER_DEPTH:
            pending.extend((subfolder, depth + 1) for subfolder in reversed(subfolders))


def _photos(folder: Path) -> list[Path]:
    """The photos of a folder, by name, without its subfolders."""
    try:
        return sorted(entry for entry in folder.iterdir() if _visible(entry) and _is_photo(entry))
    except OSError as err:
        raise PictureError("folder_not_found") from err


def _visible(entry: Path) -> bool:
    """Not hidden, and no symbolic link, which could lead out of the media."""
    return not entry.name.startswith(".") and not entry.is_symlink()


def _is_photo(entry: Path) -> bool:
    return entry.suffix.lower() in IMAGE_SUFFIXES and entry.is_file()


def _hidden(name: str) -> bool:
    """A hidden entry of an archive, or the metadata macOS adds to it."""
    return any(part.startswith(".") or part == "__MACOSX" for part in PurePosixPath(name).parts)


def _read_file(path: Path, max_bytes: int) -> bytes:
    try:
        with path.open("rb") as file:
            data = file.read(max_bytes + 1)
    except OSError as err:
        raise PictureError("unreadable_picture") from err
    return _checked(data, max_bytes)


def _checked(data: bytes, max_bytes: int) -> bytes:
    if len(data) > max_bytes:
        raise PictureError("picture_too_large")
    if not is_image(data):
        raise PictureError("not_image")
    return data


def _escape(name: str) -> str:
    """A file name as Markdown text."""
    return "".join(f"\\{char}" if char in "\\`*_[]()<>#|!~" else char for char in name)
