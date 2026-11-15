"""Read and write EverQuest PFS archives (.s3d, .eqg, .pfs).

Layout:
  header:    u32 dir_offset, char[4] "PFS ", u32 version (0x20000)
  file data: per file, a run of blocks {u32 deflated_len, u32 inflated_len, zlib bytes}
  directory: u32 count, then count x {u32 crc, u32 data_offset, u32 inflated_size}
  footer:    "STEVE" + u32 timestamp (optional; most shipped archives have none or an
             arbitrary value, so the client ignores it)

One directory entry is the filename table: u32 count, then count x {u32 len,
char[len] NUL-terminated name}. Names map, in order, to the other entries sorted
by data offset. Older archives mark the table with crc 0x61580AC9, re-released
ones with 0xFFFFFFFF.

Each entry's crc is a CRC-32 (polynomial 0x04C11DB7, not reflected, init 0) of
the lower-cased name plus its NUL. A few shipped entries don't match their name;
those stored values are kept so a repack reproduces the archive exactly.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from typing import Callable

Progress = Callable[[int, int], None]  # (bytes done, bytes total)

MAGIC = b"PFS "
VERSION = 0x20000
NAMES_CRCS = (0x61580AC9, 0xFFFFFFFF)
BLOCK = 8192
# Archives written by eq-hd carry this in the footer's timestamp field, so the
# tool can recognise its own output from the file alone.
MARKER = b"EQHD"
FOOTER_LEN = 9


def has_marker(tail: bytes) -> bool:
    """True if the archive (or its last 9+ bytes) ends with eq-hd's footer marker."""
    return tail[-FOOTER_LEN:] == b"STEVE" + MARKER


def _crc_table() -> list[int]:
    table = []
    for i in range(256):
        c = i << 24
        for _ in range(8):
            c = ((c << 1) ^ 0x04C11DB7) if c & 0x80000000 else (c << 1)
        table.append(c & 0xFFFFFFFF)
    return table


_TABLE = _crc_table()


def name_crc(name: str) -> int:
    crc = 0
    for b in name.lower().encode("latin-1") + b"\0":
        crc = ((crc << 8) ^ _TABLE[((crc >> 24) ^ b) & 0xFF]) & 0xFFFFFFFF
    return crc


@dataclass
class Archive:
    """Archive contents in their original order, plus what's needed to repack faithfully."""

    files: dict[str, bytes]
    names_crc: int = NAMES_CRCS[0]
    crc_overrides: dict[str, int] = field(default_factory=dict)


class PfsError(ValueError):
    pass


def _inflate(buf: bytes, off: int, size: int) -> bytes:
    out = bytearray()
    while len(out) < size:
        dlen, ilen = struct.unpack_from("<II", buf, off)
        off += 8
        chunk = zlib.decompress(buf[off:off + dlen])
        if len(chunk) != ilen:
            raise PfsError(f"block at {off} inflated to {len(chunk)} bytes, expected {ilen}")
        out += chunk
        off += dlen
    return bytes(out)


def _deflate(data: bytes, level: int) -> bytes:
    out = bytearray()
    for i in range(0, max(len(data), 1), BLOCK):
        chunk = data[i:i + BLOCK]
        z = zlib.compress(chunk, level)
        out += struct.pack("<II", len(z), len(chunk)) + z
    return bytes(out)


def _names(nbuf: bytes) -> list[str]:
    (ncount,) = struct.unpack_from("<I", nbuf, 0)
    names, p = [], 4
    for _ in range(ncount):
        (ln,) = struct.unpack_from("<I", nbuf, p)
        names.append(nbuf[p + 4:p + 4 + ln].rstrip(b"\0").decode("latin-1"))
        p += 4 + ln
    return names


class Reader:
    """Single files of an archive on disk, without reading or inflating the rest:
    the directory is read once, then each get() reads only that file's blocks.
    (Re-using one texture from a 100 MB upscaled archive shouldn't inflate all of it.)"""

    def __init__(self, path):
        self.path = path
        try:
            with open(path, "rb") as f:
                dir_off, magic, _version = struct.unpack("<I4sI", f.read(12))
                if magic != MAGIC:
                    raise PfsError("not a PFS archive")
                f.seek(dir_off)
                (count,) = struct.unpack("<I", f.read(4))
                raw = f.read(count * 12)
                entries = [struct.unpack_from("<III", raw, i * 12) for i in range(count)]
                names_entry = next((e for e in entries if e[0] in NAMES_CRCS), None)
                if names_entry is None:
                    raise PfsError("no filename table")
                names = _names(self._inflate_at(f, names_entry[1], names_entry[2]))
        except struct.error as e:
            raise PfsError(f"truncated archive: {e}") from None
        data_entries = sorted((e for e in entries if e is not names_entry), key=lambda e: e[1])
        if len(data_entries) != len(names):
            raise PfsError(f"{len(data_entries)} entries but {len(names)} names")
        self.entries = {name: (off, size) for name, (_crc, off, size) in zip(names, data_entries)}

    @staticmethod
    def _inflate_at(f, off: int, size: int) -> bytes:
        f.seek(off)
        out = bytearray()
        while len(out) < size:
            dlen, ilen = struct.unpack("<II", f.read(8))
            chunk = zlib.decompress(f.read(dlen))
            if len(chunk) != ilen:
                raise PfsError(f"block inflated to {len(chunk)} bytes, expected {ilen}")
            out += chunk
        return bytes(out)

    def get(self, name: str) -> bytes | None:
        where = self.entries.get(name)
        if where is None:
            return None
        try:
            with open(self.path, "rb") as f:
                return self._inflate_at(f, *where)
        except struct.error as e:
            raise PfsError(f"truncated archive: {e}") from None


def read(buf: bytes, progress: Progress | None = None) -> Archive:
    if len(buf) < 12:
        raise PfsError("too small to be a PFS archive")
    dir_off, magic, _version = struct.unpack_from("<I4sI", buf, 0)
    if magic != MAGIC:
        raise PfsError("not a PFS archive")
    try:
        (count,) = struct.unpack_from("<I", buf, dir_off)
        entries = [struct.unpack_from("<III", buf, dir_off + 4 + i * 12) for i in range(count)]
    except struct.error as e:
        raise PfsError(f"truncated directory: {e}") from None

    names_entry = next((e for e in entries if e[0] in NAMES_CRCS), None)
    if names_entry is None:
        raise PfsError("no filename table")
    names = _names(_inflate(buf, names_entry[1], names_entry[2]))

    data_entries = sorted((e for e in entries if e is not names_entry), key=lambda e: e[1])
    if len(data_entries) != len(names):
        raise PfsError(f"{len(data_entries)} entries but {len(names)} names")
    files, overrides = {}, {}
    total, done = sum(e[2] for e in data_entries), 0
    for name, (crc, off, size) in zip(names, data_entries):
        if name_crc(name) != crc:
            overrides[name] = crc
        files[name] = _inflate(buf, off, size)
        done += size
        if progress:
            progress(done, total)
    return Archive(files, names_entry[0], overrides)


def write(archive: Archive, level: int = 6, marker: bool = False, progress: Progress | None = None) -> bytes:
    """Serialize an archive. Files are stored in dict order; names keep their case.

    With marker=True the footer carries MARKER, identifying eq-hd output.
    """
    body = bytearray()
    entries = []
    names = list(archive.files)
    total, done = sum(len(d) for d in archive.files.values()), 0
    for name in names:
        data = archive.files[name]
        crc = archive.crc_overrides.get(name, name_crc(name))
        entries.append((crc, 12 + len(body), len(data)))
        body += _deflate(data, level)
        done += len(data)
        if progress:
            progress(done, total)

    nbuf = bytearray(struct.pack("<I", len(names)))
    for name in names:
        raw = name.encode("latin-1") + b"\0"
        nbuf += struct.pack("<I", len(raw)) + raw
    entries.append((archive.names_crc, 12 + len(body), len(nbuf)))
    body += _deflate(bytes(nbuf), level)

    dir_off = 12 + len(body)
    entries.sort(key=lambda e: e[0])  # the shipped archives keep the directory sorted by crc
    directory = struct.pack("<I", len(entries)) + b"".join(struct.pack("<III", *e) for e in entries)
    footer = b"STEVE" + (MARKER if marker else struct.pack("<I", 0))
    return struct.pack("<I4sI", dir_off, MAGIC, VERSION) + bytes(body) + directory + footer
