from pathlib import Path

import pytest

from eq_hd import paths


@pytest.fixture
def wsl(monkeypatch):
    monkeypatch.setattr(paths, "is_wsl", lambda: True)
    monkeypatch.setattr(paths.shutil, "which", lambda _: None)  # exercise the built-in translation
    monkeypatch.setattr(paths.os, "name", "posix")


@pytest.mark.parametrize("raw, expected", [
    (r"C:\EverQuest", "/mnt/c/EverQuest"),
    ("C:/Program Files/EverQuest", "/mnt/c/Program Files/EverQuest"),
    (r'"D:\Games\EQ"', "/mnt/d/Games/EQ"),
    ("C:\\", "/mnt/c"),
    (r"\\wsl$\Ubuntu\home\me\eq", "/home/me/eq"),
    (r"\\wsl.localhost\Ubuntu\home\me\eq", "/home/me/eq"),
    ("/mnt/c/EverQuest", "/mnt/c/EverQuest"),
])
def test_windows_paths_translate_under_wsl(wsl, raw, expected):
    assert paths.to_local_path(raw) == Path(expected)


def test_windows_path_outside_wsl_is_an_error(monkeypatch):
    monkeypatch.setattr(paths, "is_wsl", lambda: False)
    monkeypatch.setattr(paths.os, "name", "posix")
    with pytest.raises(paths.PathError):
        paths.to_local_path(r"C:\EverQuest")


def fake_eq(tmp_path, exe=True, archives=12):
    eq = tmp_path / "EverQuest"
    eq.mkdir()
    if exe:
        (eq / "eqgame.exe").write_bytes(b"MZ")
    for i in range(archives):
        (eq / f"zone{i}.s3d").write_bytes(b"x")
    return eq


def test_find_eq_install_accepts_a_real_looking_install(tmp_path):
    eq = paths.find_eq_install(str(fake_eq(tmp_path)))
    assert len(eq.archives) == 12


@pytest.mark.parametrize("kwargs, message", [
    ({"exe": False}, "eqgame.exe"),
    ({"archives": 2}, ".s3d"),
])
def test_find_eq_install_rejects_other_directories(tmp_path, kwargs, message):
    with pytest.raises(paths.PathError, match=message):
        paths.find_eq_install(str(fake_eq(tmp_path, **kwargs)))


def test_find_eq_install_rejects_missing_path(tmp_path):
    with pytest.raises(paths.PathError, match="does not exist"):
        paths.find_eq_install(str(tmp_path / "nope"))


def test_output_may_not_overlap_the_eq_directory(tmp_path):
    eq = paths.find_eq_install(str(fake_eq(tmp_path)))
    with pytest.raises(paths.PathError):
        paths.check_output_dir(eq.path, eq)
    with pytest.raises(paths.PathError):
        paths.check_output_dir(eq.path / "hd", eq)
    with pytest.raises(paths.PathError):
        paths.check_output_dir(tmp_path, eq)
    assert paths.check_output_dir(tmp_path / "out", eq) == (tmp_path / "out").resolve()


def test_candidate_eq_dirs_filters_invalid(tmp_path, monkeypatch):
    (tmp_path / "good").mkdir()
    good = fake_eq(tmp_path / "good")
    bad = tmp_path / "bad"
    bad.mkdir()
    monkeypatch.setattr(paths, "KNOWN_EQ_DIRS", [str(bad), str(tmp_path / "missing"), str(good)])
    assert paths.candidate_eq_dirs() == [good]


def test_default_cache_dir(tmp_path):
    assert paths.default_cache_dir("nt", {"LOCALAPPDATA": str(tmp_path)}) == tmp_path / "eq-hd"
    assert paths.default_cache_dir("posix", {"XDG_DATA_HOME": str(tmp_path / "xdg")}) == tmp_path / "xdg" / "eq-hd"
    assert paths.default_cache_dir("posix", {}) == Path.home() / ".local" / "share" / "eq-hd"
