"""Installing the engine environment from uv.lock, with our own download progress."""

import hashlib
import http.server
import threading

import pytest
from packaging import tags as ptags

from eq_hd import paths
from eq_hd.gui import setup_env

WINDOWS = dict(os_name="nt", sys_platform="win32", platform_system="Windows", platform_machine="AMD64",
               platform_release="10", platform_version="10.0.19045", python_version="3.12",
               python_full_version="3.12.10", implementation_name="cpython",
               platform_python_implementation="CPython", implementation_version="3.12.10")
WIN_TAGS = list(ptags.cpython_tags((3, 12), platforms=["win_amd64"])) + \
    list(ptags.compatible_tags((3, 12), platforms=["win_amd64"]))


def test_needed_wheels_picks_platform_wheels():
    wheels = setup_env.needed_wheels(paths.repo_dir() / "uv.lock", tags=WIN_TAGS, env=WINDOWS)
    by_name = {w.name: w for w in wheels}
    torch = by_name["torch"]
    assert torch.filename.endswith("win_amd64.whl") and "+cu128" in torch.filename
    assert torch.url.startswith("https://download") and len(torch.sha256) == 64
    assert {"numpy", "pillow", "rich", "spandrel", "torchvision"} <= set(by_name)
    assert not any(n.startswith(("pyside6", "nvidia", "triton", "pytest")) for n in by_name)  # gui/dev/linux-only


class RangeHandler(http.server.BaseHTTPRequestHandler):
    payload = b""
    refuse_ranges_past_end = False

    def log_message(self, *a):
        pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()

    def do_GET(self):
        start = 0
        rng = self.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            if self.refuse_ranges_past_end and start >= len(self.payload):
                self.send_response(416)
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(self.payload) - 1}/{len(self.payload)}")
        else:
            self.send_response(200)
        body = self.payload[start:]
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server():
    RangeHandler.payload = bytes(range(256)) * 400  # 100 KiB
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/pkg-1.0-py3-none-any.whl", RangeHandler.payload
    httpd.shutdown()


def test_download_resumes_and_verifies(server, tmp_path, monkeypatch):
    url, payload = server
    monkeypatch.setattr(setup_env, "CHUNK", 4096)
    wheel = setup_env.Wheel("pkg", "1.0", url, None, hashlib.sha256(payload).hexdigest(), "pkg-1.0-py3-none-any.whl")
    stop, seen = threading.Event(), []

    def progress(done, total):
        seen.append((done, total))
        if len(seen) == 3:
            stop.set()
    with pytest.raises(setup_env.Cancelled):
        setup_env.download([wheel], tmp_path, progress, stop)
    part = tmp_path / (wheel.filename + ".part")
    assert 0 < part.stat().st_size < len(payload)
    assert seen[0][1] == len(payload)  # size came from a HEAD request

    resumed_from = part.stat().st_size
    seen.clear()
    setup_env.download([wheel], tmp_path, progress, threading.Event())
    assert (tmp_path / wheel.filename).read_bytes() == payload
    assert seen[0][0] >= resumed_from and seen[-1] == (len(payload), len(payload))
    assert not part.exists()


def test_download_rejects_a_bad_hash(server, tmp_path):
    url, payload = server
    wheel = setup_env.Wheel("pkg", "1.0", url, len(payload), "0" * 64, "pkg-1.0-py3-none-any.whl")
    with pytest.raises(ValueError, match="sha256"):
        setup_env.download([wheel], tmp_path, lambda d, t: None, threading.Event())
    assert not list(tmp_path.iterdir())


def test_present_files_count_as_done(server, tmp_path):
    url, payload = server
    (tmp_path / "pkg-1.0-py3-none-any.whl").write_bytes(payload)
    wheel = setup_env.Wheel("pkg", "1.0", url, len(payload), hashlib.sha256(payload).hexdigest(),
                            "pkg-1.0-py3-none-any.whl")
    seen = []
    setup_env.download([wheel], tmp_path, lambda d, t: seen.append((d, t)), threading.Event())
    assert seen[-1] == (len(payload), len(payload))


def test_a_complete_part_file_is_verified_not_re_requested(server, tmp_path):
    url, payload = server
    RangeHandler.refuse_ranges_past_end = True
    try:
        (tmp_path / "pkg-1.0-py3-none-any.whl.part").write_bytes(payload)  # cancelled just before the rename
        wheel = setup_env.Wheel("pkg", "1.0", url, len(payload), hashlib.sha256(payload).hexdigest(),
                                "pkg-1.0-py3-none-any.whl")
        setup_env.download([wheel], tmp_path, lambda d, t: None, threading.Event())
        assert (tmp_path / wheel.filename).read_bytes() == payload
    finally:
        RangeHandler.refuse_ranges_past_end = False


def test_install_errors_include_what_uv_said(monkeypatch, tmp_path):
    import subprocess

    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 2, b"", b"error: No solution found\n  hint: torch 9.9 isn't there\n")
    monkeypatch.setattr(setup_env.subprocess, "run", fake_run)
    monkeypatch.setattr(setup_env, "_uv", lambda: "uv")
    with pytest.raises(RuntimeError) as e:
        setup_env.install(tmp_path / "env", tmp_path / "wheels")
    assert "uv export" in str(e.value) and "torch 9.9 isn't there" in str(e.value)


def test_bundled_uv_is_found_next_to_the_python_folder(tmp_path, monkeypatch):
    import os
    py = tmp_path / "runtime" / "python" / "pythonw.exe"
    py.parent.mkdir(parents=True)
    py.write_bytes(b"")
    uv = tmp_path / "runtime" / ("uv.exe" if os.name == "nt" else "uv")
    uv.write_bytes(b"")
    assert setup_env.bundled_uv(str(py)) == uv
    monkeypatch.setenv("UV", "/elsewhere/uv")
    monkeypatch.setattr(setup_env.sys, "executable", str(py))
    assert setup_env._uv() == str(uv)  # bundled wins over $UV
    uv.unlink()
    assert setup_env.bundled_uv(str(py)) is None and setup_env._uv() == "/elsewhere/uv"


def test_base_python_swaps_pythonw_for_python(tmp_path):
    w = tmp_path / "pythonw.exe"
    w.write_bytes(b"")
    assert setup_env.base_python(str(w)) == str(w)  # no sibling: unchanged
    (tmp_path / "python.exe").write_bytes(b"")
    assert setup_env.base_python(str(w)) == str(tmp_path / "python.exe")
    assert setup_env.base_python(str(tmp_path / "PythonW.EXE")) == str(tmp_path / "python.exe")


def test_engine_stamp_changes_with_lock_and_python_path(tmp_path):
    lock = tmp_path / "uv.lock"
    lock.write_text("a")
    s1 = setup_env.engine_stamp(lock, "/copy1/python/python.exe")
    assert s1 == setup_env.engine_stamp(lock, "/copy1/python/python.exe")
    assert s1 != setup_env.engine_stamp(lock, "/copy2/python/python.exe")
    lock.write_text("b")
    assert s1 != setup_env.engine_stamp(lock, "/copy1/python/python.exe")


def test_uv_calls_never_download_a_python(monkeypatch, tmp_path):
    import subprocess
    seen = []

    def fake_run(cmd, **kw):
        seen.append(kw["env"])
        return subprocess.CompletedProcess(cmd, 0, b"", b"")
    monkeypatch.setattr(setup_env.subprocess, "run", fake_run)
    monkeypatch.setattr(setup_env, "_uv", lambda: "uv")
    setup_env.install(tmp_path / "env", tmp_path / "wheels")
    assert len(seen) == 3 and all(e["UV_PYTHON_DOWNLOADS"] == "never" for e in seen)
    # uv's cache lives in the data folder (deleted with it), and a player's uv config is ignored
    assert all(e["UV_CACHE_DIR"] == str(tmp_path / "uv-cache") and e["UV_NO_CONFIG"] == "1" for e in seen)
