"""The upscaling model: where it comes from and how it's verified."""

from __future__ import annotations

import contextlib
import hashlib
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModelSpec:
    filename: str
    url: str
    sha256: str
    license: str


DEFAULT_MODEL = ModelSpec(
    filename="4x-PBRify_UpscalerV4.pth",
    url="https://github.com/Kim2091/Kim2091-Models/releases/download/4x-PBRify_UpscalerV4/4x-PBRify_UpscalerV4.pth",
    sha256="82db39e31e01da6816a9814c0553a8dcdb7bd2d655353e67075c9295068cb789",
    license="CC0-1.0",
)


class ModelError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download(spec: ModelSpec, dest: Path, log) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    log(f"downloading {spec.filename} from {spec.url}")
    req = urllib.request.Request(spec.url, headers={"User-Agent": "eq-hd-textures"})
    h = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=60) as resp, part.open("wb") as f:
        size = resp.headers.get("Content-Length")
        bar = log.task("downloading", int(size) if size else None, in_bytes=True) \
            if hasattr(log, "task") else contextlib.nullcontext(lambda n=1: None)
        with bar as advance:
            for block in iter(lambda: resp.read(1 << 20), b""):
                h.update(block)
                f.write(block)
                advance(len(block))
    if h.hexdigest() != spec.sha256:
        part.unlink(missing_ok=True)
        raise ModelError(f"downloaded {spec.filename} has sha256 {h.hexdigest()}, expected {spec.sha256}")
    part.replace(dest)


def verify(path: Path, spec: ModelSpec | None) -> str:
    """Return the file's sha256; for the default model, insist it matches the pinned hash."""
    digest = sha256_file(path)
    if spec is not None and digest != spec.sha256:
        raise ModelError(f"{path} has sha256 {digest}, expected {spec.sha256} for {spec.filename}. "
                         "Delete it to re-download.")
    return digest
