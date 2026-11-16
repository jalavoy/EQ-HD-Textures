"""Build `EQ HD Textures.exe` (launcher.c + launcher.rc) for Windows, from Linux or
Windows, with Zig's C compiler from PyPI (`ziglang`, in the dev group)."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def build_launcher(out_exe: Path) -> Path:
    out_exe = Path(out_exe)
    out_exe.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        # Zig writes its cache next to the output by default; keep it out of the way.
        cmd = [sys.executable, "-m", "ziglang", "cc", "-target", "x86_64-windows-gnu", "-O2",
               "-municode", "-Wl,--subsystem,windows", "-s", str(HERE / "launcher.c"), str(HERE / "launcher.rc"),
               "-luser32", "-o", str(out_exe)]
        r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                           env={**__import__("os").environ, "ZIG_LOCAL_CACHE_DIR": tmp})
    if r.returncode != 0:
        raise RuntimeError(f"building the launcher failed:\n{r.stderr.strip()}")
    for junk in (out_exe.with_suffix(".pdb"), out_exe.with_suffix(".lib")):
        junk.unlink(missing_ok=True)
    return out_exe


if __name__ == "__main__":
    print(build_launcher(Path(sys.argv[1] if len(sys.argv) > 1 else "EQ HD Textures.exe")))
