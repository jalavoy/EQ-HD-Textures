"""Find already upscaled textures in earlier outputs, so they aren't upscaled again.

Every rendered archive's state entry records which original textures went into
it ({texture name: sha256 of the original bytes}). A texture whose original
bytes match one of those, under the same settings, is byte-for-byte what the
model would produce again, so it's copied from that output instead. The
padding mode is part of the key: zone textures are upscaled with wrap padding,
skins and equipment with reflect, so the same bytes give different results. That makes
a patch that touches a few textures (or only the geometry) of an archive cost
seconds, and shares identical textures between archives.
"""

from __future__ import annotations

import zlib
from collections import OrderedDict
from pathlib import Path
from typing import Callable

from . import pfs
from . import state as st
from .pipeline import is_tiling_archive

OPEN_ARCHIVES = 256  # output archives whose directory is kept (small: names and offsets)


class Library:
    def __init__(self, state: st.State, key: str, s3d_dir: Path):
        self.key = key
        self.s3d_dir = s3d_dir
        self._where: dict[tuple[bool, str], tuple[str, str]] = {}  # (tiling, sha) -> (archive, texture)
        self._shas: dict[str, list[tuple[bool, str]]] = {}  # archive -> the keys it provides
        self._open: OrderedDict[str, pfs.Reader] = OrderedDict()
        for name, entry in state.archives.items():
            self.add(name, entry)

    def add(self, name: str, entry: st.Entry) -> None:
        """(Re)register an archive's output, e.g. right after it has been rendered."""
        self._open.pop(name, None)
        for k in self._shas.pop(name, []):
            if self._where.get(k, ("",))[0] == name:
                del self._where[k]
        # "error": a failed re-render keeps the index of the earlier output, still on disk.
        if entry.settings_key != self.key or entry.result not in ("upscaled", "error") \
                or not entry.textures_index:
            return
        out = self.s3d_dir / name
        if not out.is_file() or out.stat().st_size != entry.output_size:
            return
        tiling = is_tiling_archive(name)
        provided = self._shas[name] = []
        for texture, sha in entry.textures_index.items():
            if (tiling, sha) not in self._where:
                self._where[(tiling, sha)] = (name, texture)
                provided.append((tiling, sha))

    def known(self, sha: str, tiling: bool) -> bool:
        return (tiling, sha) in self._where

    def reuser(self, archive: str) -> Callable[[str], bytes | None]:
        """lookup() bound to the padding mode of the archive being rendered."""
        tiling = is_tiling_archive(archive)
        return lambda sha: self.lookup(sha, tiling)

    def lookup(self, sha: str, tiling: bool) -> bytes | None:
        where = self._where.get((tiling, sha))
        if where is None:
            return None
        name, texture = where
        try:
            reader = self._open.get(name)
            if reader is None:
                reader = self._open[name] = pfs.Reader(self.s3d_dir / name)
                while len(self._open) > OPEN_ARCHIVES:
                    self._open.popitem(last=False)
            self._open.move_to_end(name)
            return reader.get(texture)
        except (OSError, pfs.PfsError, zlib.error):
            return None
