"""The Windows launcher exe (tools/launcher), cross-compiled with Zig."""

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("ziglang")
ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("launcher_build", ROOT / "tools" / "launcher" / "build.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)

import os


def windows_temp() -> Path | None:
    """Windows' Temp folder as seen from WSL, when Windows tests are asked for
    (EQHD_WINDOWS_TESTS=1): they run the launcher on the real Windows."""
    if os.environ.get("EQHD_WINDOWS_TESTS") != "1" or not shutil.which("cmd.exe"):
        return None
    win = subprocess.run(["cmd.exe", "/c", "echo %TEMP%"], capture_output=True, text=True,
                         cwd="/mnt/c").stdout.strip()
    path = subprocess.run(["wslpath", "-u", win], capture_output=True, text=True).stdout.strip()
    return Path(path) if path and Path(path).is_dir() else None


WIN_TMP = windows_temp()


def test_launcher_builds_as_a_windows_gui_exe(tmp_path):
    exe = build.build_launcher(tmp_path / "EQ HD Textures.exe")
    data = exe.read_bytes()
    pe = int.from_bytes(data[0x3C:0x40], "little")
    assert data[:2] == b"MZ" and data[pe:pe + 4] == b"PE\0\0"
    assert int.from_bytes(data[pe + 24 + 68:pe + 24 + 70], "little") == 2  # IMAGE_SUBSYSTEM_WINDOWS_GUI
    assert "EQ HD Textures".encode("utf-16-le") in data  # version info from the .rc


@pytest.mark.skipif(WIN_TMP is None, reason="runs on Windows via WSL; set EQHD_WINDOWS_TESTS=1")
def test_launcher_check_finds_the_runtime():
    folder = WIN_TMP / "eqhd launcher tëst"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir()
    try:
        exe = build.build_launcher(folder / "EQ HD Textures.exe")
        assert subprocess.run([str(exe), "--check"], timeout=30).returncode == 3  # e.g. run from inside the zip
        py = folder / "runtime" / "python" / "pythonw.exe"
        py.parent.mkdir(parents=True)
        py.write_bytes(b"MZ")
        assert subprocess.run([str(exe), "--check"], timeout=30).returncode == 0
    finally:
        shutil.rmtree(folder, ignore_errors=True)
