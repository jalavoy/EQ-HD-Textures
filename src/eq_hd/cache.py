"""Remembered results of the read-only steps, so a second check (the window checks
at start-up, then again when you click Render) doesn't redo them.

- Fingerprints: an archive's sha256 is reused while its size and modification
  time are unchanged. Patching or installing rewrites the file, which changes its
  time, so a changed archive is always hashed again.
- Texture counts: the eligible textures of an archive, by its sha256 and the
  size limits. Same bytes, same answer.

Both live next to state.json and are only a speed-up: if they're missing or
unreadable, everything is simply computed again.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import PIPELINE_VERSION


class _JsonCache:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {}
        self.dirty = False
        try:
            loaded = json.loads(path.read_text())
            if isinstance(loaded, dict) and loaded.get("pipeline") == PIPELINE_VERSION:
                self.data = loaded.get("entries", {})
        except (OSError, ValueError):
            pass

    def save(self, keep: set[str] | None = None) -> None:
        """Write if anything changed. keep: drop entries whose keys aren't in it."""
        if keep is not None:
            stale = [k for k in self.data if k not in keep]
            for k in stale:
                del self.data[k]
            self.dirty |= bool(stale)
        if not self.dirty:
            return
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps({"pipeline": PIPELINE_VERSION, "entries": self.data}))
        os.replace(tmp, self.path)
        self.dirty = False


class Fingerprints(_JsonCache):
    """{archive name: [size, mtime_ns, sha256, marked]}"""

    def get(self, name: str, st: os.stat_result) -> tuple[str, int, bool] | None:
        e = self.data.get(name)
        if e and e[0] == st.st_size and e[1] == st.st_mtime_ns:
            return e[2], e[0], e[3]
        return None

    def put(self, name: str, st: os.stat_result, fp: tuple[str, int, bool]) -> None:
        self.data[name] = [st.st_size, st.st_mtime_ns, fp[0], fp[2]]
        self.dirty = True


class Counts(_JsonCache):
    """{"sha256:min:max": [[texture, texture sha256, pixels], ...] or null (damaged)}"""

    @staticmethod
    def key(sha: str, min_side: int, max_side: int) -> str:
        return f"{sha}:{min_side}:{max_side}"

    def has(self, key: str) -> bool:
        return key in self.data

    def get(self, key: str):
        return self.data[key]

    def put(self, key: str, items) -> None:
        self.data[key] = items
        self.dirty = True
