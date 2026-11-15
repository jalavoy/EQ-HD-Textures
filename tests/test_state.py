from eq_hd import state as st


def entry(**kw):
    base = dict(source_sha256="src", source_size=10, settings_key="k", result="upscaled",
                output_sha256="out", output_size=3, outputs_seen=["out"])
    base.update(kw)
    return st.Entry(**base)


def test_classify(tmp_path):
    out = tmp_path / "a.s3d"
    out.write_bytes(b"abc")
    c = lambda sha, e, key="k", force=False: st.classify("a.s3d", sha, e, key, out, force)
    assert c("src", None) == st.NEW
    assert c("src", entry()) == st.CURRENT
    assert c("patched", entry()) == st.CHANGED
    assert c("src", entry(), key="other") == st.SETTINGS
    assert c("src", entry(), force=True) == st.SETTINGS
    assert c("src", entry(result="error")) == st.RETRY
    assert c("src", entry(result="nothing", output_sha256=None, output_size=None, outputs_seen=[])) == st.CURRENT
    assert c("out", entry()) == st.INSTALLED
    assert c("out", entry(), key="other") == st.BLOCKED
    assert c("old-out", entry(outputs_seen=["old-out", "out"])) == st.BLOCKED
    out.unlink()
    assert c("src", entry()) == st.MISSING


def test_state_roundtrip_and_output_history(tmp_path):
    s = st.State(tmp_path / "state.json")
    s.record("a.s3d", entry(output_sha256="o1", outputs_seen=[]))
    s.record("a.s3d", entry(output_sha256="o2", outputs_seen=[]))
    s.save()
    again = st.State(tmp_path / "state.json")
    assert again.archives["a.s3d"].outputs_seen == ["o1", "o2"]
    assert again.archives["a.s3d"].output_sha256 == "o2"


def test_settings_key_changes_with_settings():
    a = st.settings_key("1", "m", 17, 512)
    assert a == st.settings_key("1", "m", 17, 512)
    assert a != st.settings_key("2", "m", 17, 512)
    assert a != st.settings_key("1", "n", 17, 512)
    assert a != st.settings_key("1", "m", 17, 256)


def test_classify_trusts_the_marker_without_state(tmp_path):
    assert st.classify("a.s3d", "x", None, "k", tmp_path / "a.s3d", False, marked=True) == st.BLOCKED


def test_measured_speed_needs_enough_history(tmp_path):
    s = st.State(tmp_path / "state.json")
    assert st.seconds_per_megapixel(s) is None
    s.record("a.s3d", entry(pixels=1_000_000, seconds=10.0))
    assert st.seconds_per_megapixel(s) is None  # under 2 megapixels of history
    s.record("b.s3d", entry(pixels=3_000_000, seconds=50.0))
    rate, n = st.seconds_per_megapixel(s)
    assert n == 2 and abs(rate - 15.0) < 1e-9


def test_measured_speed_ignores_mostly_reused_archives(tmp_path):
    s = st.State(tmp_path / "state.json")
    index = {f"t{i}": "sha" for i in range(10)}
    s.record("a.s3d", entry(pixels=3_000_000, seconds=30.0, textures=10, textures_index=index))
    # 1 of 10 textures upscaled, 9 re-used: its time was mostly reading and repacking
    s.record("b.s3d", entry(pixels=300_000, seconds=40.0, textures=1, textures_index=index))
    rate, n = st.seconds_per_megapixel(s)
    assert n == 1 and abs(rate - 10.0) < 1e-9
