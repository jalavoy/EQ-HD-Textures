"""tools/build_windows.py: the pieces of the Windows zip build that run without downloading."""

import hashlib
import importlib.util
import os
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("build_windows", ROOT / "tools" / "build_windows.py")
bw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bw)


def fake_good_tree(tmp_path):
    top = tmp_path / "EQ HD Textures"
    for rel in ("EQ HD Textures.exe", "runtime/python/pythonw.exe", "runtime/python/vcruntime140.dll",
                "runtime/python/msvcp140.dll", "runtime/python/Lib/site-packages/PySide6/QtWidgets.pyd",
                "runtime/uv.exe",
                "runtime/app/src/eq_hd/gui/app.py", "README.txt", "LICENSE.txt",
                "runtime/uv-LICENSE-MIT.txt", "runtime/uv-LICENSE-APACHE.txt"):
        (top / rel).parent.mkdir(parents=True, exist_ok=True)
        (top / rel).write_bytes(b"x")
    return top


def test_pth_points_at_the_app_source_and_survives_moving(tmp_path):
    for root in (tmp_path / "a", tmp_path / "b c"):
        site = root / "runtime" / "python" / "Lib" / "site-packages"
        site.mkdir(parents=True)
        line = bw.write_pth(site).read_text().strip()
        assert not os.path.isabs(line)
        assert Path(os.path.normpath(site / line)) == root / "runtime" / "app" / "src"


def test_check_tree_flags_problems(tmp_path):
    top = fake_good_tree(tmp_path)
    assert bw.check_tree(top) == []
    (top / "runtime/python/Lib/site-packages/x.so").write_bytes(b"")
    deep = top / "runtime" / ("d" * 160)
    deep.mkdir()
    (deep / "f").write_bytes(b"")
    (top / "runtime/python/vcruntime140.dll").unlink()
    problems = " ".join(bw.check_tree(top))
    assert "x.so" in problems and "vcruntime140.dll" in problems and "longer than 150" in problems


def test_zip_has_one_top_folder_with_the_exe_at_its_root(tmp_path):
    top = fake_good_tree(tmp_path)
    (top / "runtime/app/src/eq_hd/__pycache__").mkdir()
    (top / "runtime/app/src/eq_hd/__pycache__/x.pyc").write_bytes(b"")
    sha = bw.make_zip(top, tmp_path / "out.zip")
    names = zipfile.ZipFile(tmp_path / "out.zip").namelist()
    assert all(n.startswith("EQ HD Textures/") for n in names)
    assert "EQ HD Textures/EQ HD Textures.exe" in names and len(sha) == 64
    assert not any("__pycache__" in n for n in names)


def test_stage_app_copies_the_source_without_caches(tmp_path):
    app = tmp_path / "app"
    bw.stage_app(ROOT, app)
    assert (app / "pyproject.toml").is_file() and (app / "uv.lock").is_file() and (app / "README.md").is_file()
    assert (app / "src" / "eq_hd" / "gui" / "icon.ico").is_file()
    assert not list(app.rglob("__pycache__")) and not (app / "tests").exists()


def test_fetch_rejects_a_wrong_hash(tmp_path):
    src = tmp_path / "f.bin"
    src.write_bytes(b"data")
    with pytest.raises(ValueError):
        bw.fetch(src.as_uri(), "0" * 64, tmp_path / "cache")
    good = hashlib.sha256(b"data").hexdigest()
    assert bw.fetch(src.as_uri(), good, tmp_path / "cache").read_bytes() == b"data"


def test_version_tag_must_match_pyproject():
    assert bw.main(["--version-tag", "v9.9.9", "--dry-check"]) == 1
    assert bw.main(["--version-tag", f"v{bw.version()}", "--dry-check"]) == 0


def test_prune_drops_qml_which_the_window_doesnt_use(tmp_path):
    site = tmp_path / "site-packages"
    (site / "PySide6" / "qml" / "QtQuick").mkdir(parents=True)
    (site / "PySide6" / "qml" / "QtQuick" / "x.png").write_bytes(b"x")
    (site / "PySide6" / "QtWidgets.pyd").write_bytes(b"x")
    bw.prune(site)
    assert not (site / "PySide6" / "qml").exists() and (site / "PySide6" / "QtWidgets.pyd").exists()


def test_cpp_runtime_must_be_next_to_python_exe(tmp_path):
    top = fake_good_tree(tmp_path)
    (top / "runtime/python/msvcp140.dll").unlink()
    qt = top / "runtime/python/Lib/site-packages/PySide6"
    qt.mkdir(parents=True, exist_ok=True)
    (qt / "msvcp140.dll").write_bytes(b"x")  # Qt's own copy only helps Qt, not the engine (torch)
    assert any("msvcp140.dll" in p for p in bw.check_tree(top))
    for name in ("msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll"):
        (qt / name).write_bytes(name.encode())
    bw.place_cpp_runtime(top / "runtime" / "python")
    assert bw.check_tree(top) == []
    assert (top / "runtime/python/msvcp140_2.dll").read_bytes() == b"msvcp140_2.dll"


def test_the_zip_must_carry_the_licence(tmp_path):
    top = fake_good_tree(tmp_path)
    (top / "LICENSE.txt").unlink()
    assert any("LICENSE.txt" in p for p in bw.check_tree(top))


def test_stage_app_includes_the_licence(tmp_path):
    bw.stage_app(ROOT, tmp_path / "app")
    assert "GNU GENERAL PUBLIC LICENSE" in (tmp_path / "app" / "LICENSE").read_text()


def test_uvs_licence_must_be_shipped_with_it(tmp_path):
    top = fake_good_tree(tmp_path)
    (top / "runtime/uv-LICENSE-MIT.txt").unlink()
    assert any("uv-LICENSE-MIT.txt" in p for p in bw.check_tree(top))
