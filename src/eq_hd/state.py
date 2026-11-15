"""Fingerprints of processed archives, so re-runs only redo what changed.

output/state.json records, per archive name: the sha256 and size of the source
archive that was processed, the settings key it was processed with, and the
sha256 of every output ever produced for it.

Outputs also carry a marker in their footer (see pfs.MARKER), so an archive
that's already been upscaled and copied into the EverQuest directory is
recognised as such even without this file, and is never upscaled again.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

STATE_VERSION = 1


def settings_key(pipeline_version: str, model_sha256: str, min_side: int, max_side: int) -> str:
    blob = json.dumps({"pipeline": pipeline_version, "model": model_sha256,
                       "min_side": min_side, "max_side": max_side}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


@dataclass
class Entry:
    source_sha256: str
    source_size: int
    settings_key: str
    result: str                     # "upscaled", "nothing" (no eligible textures) or "error"
    output_sha256: str | None = None
    output_size: int | None = None
    outputs_seen: list[str] = field(default_factory=list)
    textures: int = 0
    pixels: int = 0
    seconds: float = 0.0
    error: str | None = None
    updated: str = ""


class State:
    def __init__(self, path: Path):
        self.path = path
        self.archives: dict[str, Entry] = {}
        if path.exists():
            data = json.loads(path.read_text())
            if data.get("version") != STATE_VERSION:
                raise ValueError(f"{path}: unsupported state version {data.get('version')}")
            self.archives = {k: Entry(**v) for k, v in data["archives"].items()}

    def save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"version": STATE_VERSION,
                                   "archives": {k: asdict(v) for k, v in sorted(self.archives.items())}},
                                  indent=1))
        os.replace(tmp, self.path)

    def record(self, name: str, entry: Entry) -> None:
        old = self.archives.get(name)
        seen = list(old.outputs_seen) if old else []
        if entry.output_sha256 and entry.output_sha256 not in seen:
            seen.append(entry.output_sha256)
        entry.outputs_seen = seen
        entry.updated = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.archives[name] = entry


# Classification of an archive found in the EQ directory.
NEW = "new"                    # never processed
CHANGED = "changed"            # source differs from what was processed (e.g. patched)
SETTINGS = "settings changed"  # same source, different model or limits
MISSING = "output missing"     # processed, but the output file is gone or altered
RETRY = "retry"                # last attempt failed
CURRENT = "up to date"
INSTALLED = "installed"        # the EQ file is one of our own outputs (already copied in)
BLOCKED = "installed, stale"   # our output is installed, but it's out of date: needs the original back
TO_PROCESS = (NEW, CHANGED, SETTINGS, MISSING, RETRY)


def classify(name: str, sha: str, entry: Entry | None, key: str, output_file: Path, force: bool,
             marked: bool = False) -> str:
    """marked: the EQ file carries eq-hd's footer marker (it's one of our outputs)."""
    if marked or (entry is not None and sha in entry.outputs_seen):
        current = (entry is not None and entry.settings_key == key and entry.output_sha256 == sha and not force)
        return INSTALLED if current else BLOCKED
    if entry is None:
        return NEW
    if entry.source_sha256 != sha:
        return CHANGED
    if entry.result == "error":
        return RETRY
    if entry.settings_key != key or force:
        return SETTINGS
    if entry.result == "upscaled":
        if not output_file.exists() or output_file.stat().st_size != entry.output_size:
            return MISSING
    return CURRENT


def seconds_per_megapixel(state: "State") -> tuple[float, int] | None:
    """Measured speed from earlier runs: (seconds per source megapixel, archives it's based on)."""
    done = [e for e in state.archives.values() if e.result == "upscaled" and e.pixels > 0 and e.seconds > 0]
    pixels = sum(e.pixels for e in done)
    if pixels < 2_000_000:  # too little history to be meaningful
        return None
    return sum(e.seconds for e in done) / (pixels / 1e6), len(done)
