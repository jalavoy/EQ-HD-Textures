"""Stands in for `eq-hd --events`: canned events, optionally obeying stdin commands."""

import json
import sys


def emit(kind, **fields):
    print(json.dumps({"kind": kind, **fields}), flush=True)


args = sys.argv[1:]
if "--dry-run" in args:
    emit("plan", statuses={"new": 2, "installed": 1}, to_render=2, to_copy=1, textures=10, megapixels=0.5,
         estimate_seconds=90, disk_needed=10**9, disk_free=10**11)
    emit("result", code=0, errors=0)
    sys.exit(0)

emit("log", message="starting")
print("some library chatter that isn't JSON", flush=True)
print("a warning on stderr", file=sys.stderr, flush=True)
if "--wait-for-stop" in args:
    for line in sys.stdin:
        cmd = line.strip()
        if cmd == "pause":
            emit("paused", on=True)
        elif cmd == "resume":
            emit("paused", on=False)
        elif cmd == "stop":
            emit("result", code=130, errors=0)
            sys.exit(130)
    sys.exit(130)
emit("phase", step="archive", archive="zone.s3d", sub="upscaling", done=50, total=100, unit="pixels", textures=[1, 2])
emit("phase", step="archive", archive="zone.s3d", sub="upscaling", done=100, total=100, unit="pixels", textures=[2, 2])
emit("overall", done_px=100, total_px=100, textures_done=2, textures_total=2, elapsed=1.0, eta=0.0, errors=0)
emit("result", code=0, errors=0)
