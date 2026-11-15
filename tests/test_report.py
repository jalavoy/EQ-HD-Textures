import io
import json

from eq_hd.report import JsonReporter


def lines(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_json_reporter_writes_one_object_per_line():
    out = io.StringIO()
    r = JsonReporter(stream=out)
    r.log("hello")
    r.archive("a.s3d", "reading", 5, 10, None, 0.1)
    r.result(0, 0)
    events = lines(out)
    assert [e["kind"] for e in events] == ["log", "phase", "result"]
    assert events[0]["message"] == "hello"
    phase = events[1]
    assert (phase["step"], phase["archive"], phase["sub"], phase["unit"]) == ("archive", "a.s3d", "reading", "bytes")
    assert (phase["done"], phase["total"]) == (5, 10)


def test_json_reporter_throttles_but_keeps_final():
    out = io.StringIO()
    r = JsonReporter(stream=out)
    for i in range(1000):
        r.archive("a.s3d", "upscaling", i, 999, (i, 999), 0.0)
    events = lines(out)
    assert len(events) < 50
    assert events[-1]["done"] == 999
    assert events[-1]["unit"] == "pixels" and events[-1]["textures"] == [999, 999]


def test_json_reporter_task_reports_bytes():
    out = io.StringIO()
    r = JsonReporter(stream=out)
    with r.task("download", 100, "bytes") as advance:
        advance(40)
        advance(60)
    last = lines(out)[-1]
    assert (last["step"], last["done"], last["total"], last["unit"]) == ("download", 100, 100, "bytes")


def test_texture_rate_is_shown_once_there_is_enough_to_average():
    from eq_hd.report import fmt_rate
    assert fmt_rate(120, 60) == "120 textures/min"
    assert fmt_rate(1500, 600) == "150 textures/min"
    assert fmt_rate(10, 60) == ""        # too few textures to say
    assert fmt_rate(100, 20) == ""       # too early to say
