"""Fingerprints and texture counts are remembered, so a second check doesn't redo them."""

import os

import pytest

from eq_hd import cli, upscaler

from .helpers import FakeUpscaler, make_archive, make_dds


@pytest.fixture
def setup(tmp_path, monkeypatch):
    eq = tmp_path / "EverQuest"
    eq.mkdir()
    (eq / "eqgame.exe").write_bytes(b"MZ")
    for i in range(12):
        (eq / f"z{i}.s3d").write_bytes(make_archive({"z.wld": b"w", "t.dds": make_dds(32, 32, seed=i)}))
    (tmp_path / "m.pth").write_bytes(b"x")
    monkeypatch.setattr(upscaler, "Upscaler", FakeUpscaler)
    hashed, counted = [], []
    real_hash, real_count = cli._hash_file, cli.count_textures
    monkeypatch.setattr(cli, "_hash_file", lambda p: (hashed.append(p.name), real_hash(p))[1])
    monkeypatch.setattr(cli, "count_textures", lambda ps, *a: (counted.extend(p.name for p in ps), real_count(ps, *a))[1])

    def run(*extra):
        hashed.clear(), counted.clear()
        return cli.main(["--eq-directory", str(eq), "--output", str(tmp_path / "out"), "--model",
                         str(tmp_path / "m.pth"), "--device", "cpu", "--yes", *extra])
    return eq, run, hashed, counted


def test_a_second_check_reuses_fingerprints_and_counts(setup):
    eq, run, hashed, counted = setup
    assert run("--dry-run") == 0
    assert len(hashed) == 12 and len(counted) == 12
    assert run("--dry-run") == 0
    assert hashed == [] and counted == []


def test_a_changed_archive_is_checked_again(setup):
    eq, run, hashed, counted = setup
    run("--dry-run")
    z = eq / "z3.s3d"
    data = bytearray(z.read_bytes())
    data[100] ^= 0xFF                   # same size, different content...
    z.write_bytes(bytes(data))
    st = z.stat()
    os.utime(z, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))  # ...and a new time, as any write gives
    run("--dry-run")
    assert hashed == ["z3.s3d"] and counted == ["z3.s3d"]


def test_render_after_a_check_uses_the_remembered_results(setup):
    eq, run, hashed, counted = setup
    run("--dry-run")
    assert run() == 0
    assert hashed == [] and counted == []
