"""Moving the window's data to another data folder."""

import os
import threading

import pytest

from eq_hd.gui import data_folder
from eq_hd.gui.setup_env import Cancelled


def make_data(old):
    old.mkdir(parents=True, exist_ok=True)
    data_folder.mark(old)
    (old / "output" / "s3d").mkdir(parents=True)
    (old / "output" / "s3d" / "zone.s3d").write_bytes(b"z" * 1000)
    (old / "output" / "state.json").write_text("{}")
    (old / "downloads").mkdir()
    (old / "downloads" / "torch.whl").write_bytes(b"t" * 500)
    (old / "engine-env").mkdir()
    (old / "engine-env" / "python").write_bytes(b"p")


def contents(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture(params=["rename", "copy"])
def how(request, monkeypatch):
    if request.param == "copy":  # another drive: rename fails
        def no_rename(a, b):
            raise OSError(18, "Invalid cross-device link")
        monkeypatch.setattr(data_folder.os, "rename", no_rename)
    return request.param


def test_moves_output_and_downloads_not_the_engine(tmp_path, how):
    old, new = tmp_path / "old", tmp_path / "new"
    make_data(old)
    before = contents(old)
    seen = []
    data_folder.move(old, new, lambda d, t: seen.append((d, t)), threading.Event())
    assert contents(new) == {k: v for k, v in before.items() if not k.startswith("engine-env")}
    assert not (old / "output").exists() and not (old / "downloads").exists()
    assert (old / "engine-env" / "python").exists()
    assert seen[-1] == (1502, 1502)


def test_cancelled_copy_changes_nothing(tmp_path, how):
    old, new = tmp_path / "old", tmp_path / "new"
    make_data(old)
    before = contents(old)
    stop = threading.Event()
    stop.set()
    if how == "rename":  # renames are instant; cancelling only matters while copying
        return
    with pytest.raises(Cancelled):
        data_folder.move(old, new, lambda d, t: None, stop)
    assert contents(old) == before
    assert not (new / "output").exists() and not (new / "downloads").exists()


def test_a_later_failure_undoes_earlier_renames(tmp_path, monkeypatch):
    old, new = tmp_path / "old", tmp_path / "new"
    make_data(old)
    before = contents(old)
    real = os.rename

    def rename(a, b):
        if str(a).endswith("downloads") and str(b).startswith(str(new)):
            raise OSError(18, "cross-device")
        return real(a, b)
    monkeypatch.setattr(data_folder.os, "rename", rename)
    monkeypatch.setattr(data_folder, "_copy_tree", lambda *a: (_ for _ in ()).throw(OSError(28, "disk full")))
    with pytest.raises(OSError):
        data_folder.move(old, new, lambda d, t: None, threading.Event())
    assert contents(old) == before


def test_conflicts_and_has_data(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    assert not data_folder.has_data(old)
    make_data(old)
    assert data_folder.has_data(old) and data_folder.data_size(old) == 1502
    (new / "output").mkdir(parents=True)
    assert data_folder.conflicts(old, new) == []  # empty: fine
    (new / "output" / "state.json").write_text("{}")
    assert data_folder.conflicts(old, new) == [new / "output"]
    with pytest.raises(FileExistsError):
        data_folder.move(old, new, lambda d, t: None, threading.Event())


def test_delete_data_removes_only_our_entries(tmp_path):
    d = tmp_path / "data"
    make_data(d)
    (d / "gui-error.log").write_text("x")
    (d / "uv-cache").mkdir()
    (d / "uv-cache" / "torch.whl").write_bytes(b"c" * 10)
    (d / "notes.txt").write_text("mine")
    assert data_folder.folder_size(d) == 1502 + 1 + 1 + 10  # output + downloads, engine-env, the log, uv's cache
    data_folder.delete_data(d)
    assert sorted(p.name for p in d.iterdir()) == [".eq-hd-data", "notes.txt"]  # still ours, still has notes
    (d / "notes.txt").unlink()
    make_data(d)
    data_folder.delete_data(d)
    assert not d.exists()  # empty afterwards: the folder goes too


def test_a_folder_with_other_things_in_it_gets_an_eq_hd_subfolder(tmp_path):
    drive = tmp_path / "D"
    (drive / "Downloads").mkdir(parents=True)  # the player's own Downloads: "downloads" on Windows
    (drive / "Downloads" / "photo.jpg").write_bytes(b"mine")
    assert data_folder.owned(drive) == drive / "eq-hd"
    assert data_folder.owned(tmp_path / "empty") == tmp_path / "empty"  # doesn't exist yet: fine
    (tmp_path / "empty2").mkdir()
    assert data_folder.owned(tmp_path / "empty2") == tmp_path / "empty2"
    ours = tmp_path / "ours"
    make_data(ours)
    assert data_folder.owned(ours) == ours


def test_delete_and_move_refuse_a_folder_that_isnt_ours(tmp_path):
    drive = tmp_path / "D"
    (drive / "Downloads").mkdir(parents=True)
    (drive / "Downloads" / "photo.jpg").write_bytes(b"mine")
    (drive / "output").mkdir()
    (drive / "output" / "report.docx").write_bytes(b"mine")
    with pytest.raises(data_folder.NotOurFolder):
        data_folder.delete_data(drive)
    with pytest.raises(data_folder.NotOurFolder):
        data_folder.move(drive, tmp_path / "new", lambda d, t: None, threading.Event())
    assert (drive / "Downloads" / "photo.jpg").read_bytes() == b"mine"
    assert (drive / "output" / "report.docx").read_bytes() == b"mine"


def test_adopt_marks_only_folders_that_hold_nothing_but_ours(tmp_path):
    old = tmp_path / "eq-hd"  # made by an earlier version, before the marker existed
    for name in ("output", "engine-env", "downloads", "uv-cache"):
        (old / name).mkdir(parents=True)
    (old / "settings.ini").write_text("")
    (old / "gui-error.log").write_text("")
    assert data_folder.adopt(old) and data_folder.is_ours(old)
    other = tmp_path / "other"
    (other / "Downloads").mkdir(parents=True)
    assert not data_folder.adopt(other) and not data_folder.is_ours(other)
    fresh = tmp_path / "fresh"
    assert data_folder.adopt(fresh) and data_folder.is_ours(fresh)


def test_moving_marks_the_new_folder(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    make_data(old)
    data_folder.move(old, new, lambda d, t: None, threading.Event())
    assert data_folder.is_ours(new)
