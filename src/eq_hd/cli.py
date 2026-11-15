"""eq-hd: upscale EverQuest .s3d textures into a separate output directory.

  uv run eq-hd --eq-directory /mnt/c/EverQuest
  uv run eq-hd --eq-directory 'C:\\EverQuest' --dry-run

The EverQuest directory is only ever read. Upscaled archives are written to
output/s3d/ (in this repo by default); copy them into the EverQuest directory
yourself. Re-running only processes archives that are new, changed (e.g. by a
patch), or were processed with different settings.
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import hashlib
import os
import shutil
import signal
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

from rich.console import Console
from rich.progress import (BarColumn, DownloadColumn, MofNCompleteColumn, Progress, SpinnerColumn,
                           TaskProgressColumn, TextColumn, TimeElapsedColumn)

from . import PIPELINE_VERSION, models, paths, pfs, pipeline
from . import state as st

# Speed assumed before any run has been measured: test runs on an RTX 3090 averaged
# 0.66 s per texture at 51,500 source pixels each. After a run, measured speed is used.
DEFAULT_SECONDS_PER_MEGAPIXEL = 12.8
WORST_CASE_GROWTH = 16  # output archive size / source size; observed 6-15x
SAFETY_MARGIN = 1 * 2**30


class Abort(Exception):
    pass


# ---------------------------------------------------------------- output helpers

class Log:
    """Timestamped lines to the terminal and run.log."""

    def __init__(self, file: Path | None = None):
        self.fh = None
        self.console = Console(highlight=False, soft_wrap=True)
        self.tty = self.console.is_terminal
        if file:
            self.attach(file)

    def attach(self, file: Path) -> None:
        self.fh = file.open("a", encoding="utf-8")

    def __call__(self, msg: str = "", terminal: bool = True) -> None:
        line = f"{time.strftime('%H:%M:%S')} {msg}" if msg else ""
        if terminal:
            self.console.print(line, markup=False)
        if self.fh:
            self.fh.write(line + "\n")
            self.fh.flush()

    @contextlib.contextmanager
    def task(self, description: str, total: float | None, in_bytes: bool = False):
        """A temporary progress bar for a preparation step (terminal only).

        Yields advance(n=1). total=None shows an indeterminate bar.
        """
        if not self.tty:
            yield lambda n=1: None
            return
        with Progress(SpinnerColumn(), TextColumn(description, markup=False),
                      BarColumn(bar_width=40, complete_style="green", finished_style="green"),
                      TaskProgressColumn(), DownloadColumn() if in_bytes else MofNCompleteColumn(),
                      TimeElapsedColumn(), console=self.console, transient=True) as p:
            t = p.add_task(description, total=total)
            yield lambda n=1: p.advance(t, n)


class Bars:
    """Live progress bars under the log lines, in a terminal only: the archive
    being upscaled, and the whole run. Both are measured in source pixels."""

    def __init__(self, log: Log, total_px: int):
        self.p = None
        if not log.tty:
            return
        self.p = Progress(
            SpinnerColumn(finished_text=" "),
            TextColumn("{task.description}", markup=False, style="bold"),
            BarColumn(bar_width=40, complete_style="green", finished_style="green"),
            TaskProgressColumn(),
            TextColumn("{task.fields[info]}", markup=False, style="dim"),
            console=log.console, transient=True, refresh_per_second=8)
        self.archive = self.p.add_task("", total=1, info="", visible=False)
        self.overall = self.p.add_task("overall", total=max(1, total_px), info="")

    def __enter__(self):
        if self.p:
            self.p.start()
        return self

    def __exit__(self, *exc):
        if self.p:
            self.p.stop()

    def archive_update(self, name: str, k: int, of: int, px: int, px_of: int, spent: float) -> None:
        if self.p:
            frac = px / max(1, px_of)
            left = f" · ~{fmt_time(spent / frac * (1 - frac))} left" if frac > 0.02 else ""
            self.p.update(self.archive, description=name, completed=px, total=max(1, px_of),
                          info=f"{k}/{of} textures · {fmt_time(spent)}{left}", visible=True)

    def overall_update(self, done_px: int, total_px: int, info: str) -> None:
        if self.p:
            self.p.update(self.overall, completed=done_px, total=max(1, total_px), info=info)


def fmt_time(seconds: float) -> str:
    seconds = int(seconds)
    h, m = divmod(seconds // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{seconds % 60:02d}s"


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return ""


class Lock:
    """One run per output directory. A lock left by a dead process is cleared."""

    def __init__(self, path: Path):
        self.path = path
        self.held = False

    def __enter__(self):
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self.held = True
                return self
            except FileExistsError:
                try:
                    pid = int(self.path.read_text().strip() or 0)
                except (OSError, ValueError):
                    pid = 0
                if pid and _alive(pid):
                    raise Abort(f"another eq-hd run (pid {pid}) is using this output directory. "
                                f"If that's wrong, delete {self.path}.") from None
                self.path.unlink(missing_ok=True)
        raise Abort(f"couldn't take the lock {self.path}")

    def __exit__(self, *exc):
        if self.held:
            self.path.unlink(missing_ok=True)


def _alive(pid: int) -> bool:
    if os.name == "nt":
        return True  # can't check cheaply; the user can delete the lock file
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


# ---------------------------------------------------------------- steps

def parse_args(argv):
    ap = argparse.ArgumentParser(prog="eq-hd", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--eq-directory", required=True, metavar="PATH",
                    help="EverQuest install (read only). Windows paths like C:\\EverQuest work under WSL.")
    ap.add_argument("--output", metavar="PATH", type=Path, default=None,
                    help="where results go (default: output/ in this repo). Must be outside the EQ directory.")
    ap.add_argument("--include", metavar="GLOB", action="append",
                    help="only archives matching this name pattern, e.g. 'nexus*.s3d' (repeatable; default: all .s3d)")
    ap.add_argument("--model", metavar="PATH", type=Path, default=None,
                    help=f"model file (default: models/{models.DEFAULT_MODEL.filename}, downloaded and verified if missing)")
    ap.add_argument("--min-size", type=int, default=pipeline.Limits.min_side, metavar="PX",
                    help="skip textures whose largest side is smaller than this (default %(default)s)")
    ap.add_argument("--max-size", type=int, default=pipeline.Limits.max_side, metavar="PX",
                    help="skip textures whose largest side is larger than this (default %(default)s)")
    ap.add_argument("--force", action="store_true", help="re-render archives even if they're up to date")
    ap.add_argument("--dry-run", action="store_true", help="show what would be done, then stop")
    ap.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    ap.add_argument("--device", default="cuda", help="torch device (default: cuda)")
    return ap.parse_args(argv)


def _hash_file(path: Path) -> tuple[str, int, bool]:
    """(sha256, size, carries eq-hd's output marker)."""
    h = hashlib.sha256()
    size, tail = 0, b""
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
            size += len(block)
            tail = (tail + block)[-pfs.FOOTER_LEN:]
    return h.hexdigest(), size, pfs.has_marker(tail)


def _count_worker(job):
    path, limits = job
    try:
        return path.name, pipeline.count_eligible(path.read_bytes(), limits)
    except Exception:
        return path.name, (0, 0)  # unreadable; the main loop will record the error


def check_output_consistency(s3d_dir: Path, state: st.State, log: Log) -> None:
    for tmp in s3d_dir.glob("*.tmp"):
        log(f"removing partial file from an interrupted run: {tmp.name}")
        tmp.unlink()
    unknown = {p.name for p in s3d_dir.glob("*.s3d")} - set(state.archives)
    if unknown:
        log(f"note: {len(unknown)} archive(s) in {s3d_dir} aren't in the state file and will be "
            "overwritten if they're processed again: "
            + ", ".join(sorted(unknown)[:5]) + (" ..." if len(unknown) > 5 else ""))


def resolve_model(args, log: Log) -> tuple[Path, models.ModelSpec | None]:
    if args.model is not None:
        path = args.model.expanduser().resolve()
        if not path.is_file():
            raise Abort(f"model file {path} not found")
        return path, None
    return paths.repo_dir() / "models" / models.DEFAULT_MODEL.filename, models.DEFAULT_MODEL


def confirm(args, question: str) -> None:
    if args.yes:
        return
    if not sys.stdin.isatty():
        raise Abort("not running interactively; re-run with --yes to proceed without confirmation")
    if input(f"{question} [y/N] ").strip().lower() not in ("y", "yes"):
        raise Abort("cancelled")


def run(args) -> int:
    log = Log()
    limits = pipeline.Limits(args.min_size, args.max_size)
    if not (1 <= limits.min_side <= limits.max_side):
        raise Abort("--min-size must be at least 1 and no larger than --max-size")

    # 1. paths
    try:
        eq = paths.find_eq_install(args.eq_directory)
        out = paths.check_output_dir(args.output or paths.default_output_dir(), eq)
    except paths.PathError as e:
        raise Abort(str(e)) from None
    archives = eq.archives
    if args.include:
        archives = [p for p in archives if any(fnmatch.fnmatch(p.name.lower(), g.lower()) for g in args.include)]
        if not archives:
            raise Abort(f"no .s3d archives in {eq.path} match {args.include}")

    s3d_dir = out / "s3d"
    s3d_dir.mkdir(parents=True, exist_ok=True)
    log.attach(out / "run.log")
    log(f"EverQuest directory (read only): {eq.path}  [{len(eq.archives)} .s3d archives]")
    log(f"output directory:                {out}")

    with Lock(out / ".lock"):
        state = st.State(out / "state.json")
        check_output_consistency(s3d_dir, state, log)

        # 2. model and settings
        model_path, spec = resolve_model(args, log)
        need_download = spec is not None and not model_path.exists()
        model_sha = spec.sha256 if need_download else models.verify(model_path, spec)
        key = st.settings_key(PIPELINE_VERSION, model_sha, limits.min_side, limits.max_side)
        log(f"model: {model_path.name}" + (" (will be downloaded)" if need_download else "")
            + f"; textures {limits.min_side}-{limits.max_side} px are upscaled")

        # 3. fingerprint
        log(f"fingerprinting {len(archives)} archives ...")
        t0 = time.time()
        prints = {}
        with ThreadPoolExecutor(8) as ex, \
                log.task("fingerprinting", sum(p.stat().st_size for p in archives), in_bytes=True) as advance:
            for p, fp in zip(archives, ex.map(_hash_file, archives)):
                prints[p] = fp
                advance(fp[1])
        log(f"  done in {fmt_time(time.time() - t0)}")
        plan: dict[str, list[Path]] = {}
        for p in archives:
            sha, _size, marked = prints[p]
            status = st.classify(p.name, sha, state.archives.get(p.name), key, s3d_dir / p.name, args.force, marked)
            plan.setdefault(status, []).append(p)
        for status in (st.CURRENT, st.INSTALLED, *st.TO_PROCESS, st.BLOCKED):
            if plan.get(status):
                log(f"  {status:17} {len(plan[status])}")
        if plan.get(st.BLOCKED):
            log("warning: these EverQuest archives are already upscaled (this tool's output from an older run, "
                "older settings, or a lost state file), so they can't be re-rendered from. To re-render them, "
                "restore the originals (e.g. the launcher's file check) and run again: " + ", ".join(p.name for p in plan[st.BLOCKED][:10])
                + (" ..." if len(plan[st.BLOCKED]) > 10 else ""))
        todo = [p for s in st.TO_PROCESS for p in plan.get(s, [])]
        todo.sort(key=lambda p: p.name.lower())
        if not todo:
            log("nothing to do")
            return 0

        # 4. estimate
        log(f"counting textures in {len(todo)} archives ...")
        counts = {}
        with ProcessPoolExecutor(min(8, os.cpu_count() or 1)) as ex, \
                log.task("counting textures", len(todo)) as advance:
            for name, c in ex.map(_count_worker, [(p, limits) for p in todo], chunksize=4):
                counts[name] = c
                advance()
        total = sum(c[0] for c in counts.values())
        total_px = sum(c[1] for c in counts.values())
        src_bytes = sum(prints[p][1] for p in todo)
        need = src_bytes * WORST_CASE_GROWTH + SAFETY_MARGIN
        free = shutil.disk_usage(out).free
        measured = st.seconds_per_megapixel(state)
        if measured:
            rate, basis = measured[0], f"measured speed from {measured[1]} archives done earlier"
        else:
            rate, basis = DEFAULT_SECONDS_PER_MEGAPIXEL, "rough default for an RTX 3090; later runs use measured speed"
        log(f"to process: {len(todo)} archives, {total} textures ({total_px / 1e6:.0f} megapixels), "
            f"source {fmt_bytes(src_bytes)}")
        log(f"estimate:   ~{fmt_time(total_px / 1e6 * rate)} ({basis})")
        log(f"disk:       up to {fmt_bytes(src_bytes * WORST_CASE_GROWTH)} of output ({fmt_bytes(free)} free)")
        if args.dry_run:
            for p in todo[:40]:
                log(f"  would process {p.name} ({counts.get(p.name, (0, 0))[0]} textures)")
            if len(todo) > 40:
                log(f"  ... and {len(todo) - 40} more")
            log("dry run: nothing written")
            return 0
        if free < need:
            raise Abort(f"not enough free space in {out}: need up to {fmt_bytes(need)}, have {fmt_bytes(free)}")
        confirm(args, f"Upscale {len(todo)} archives into {s3d_dir}?")

        # 5. model load
        if need_download:
            try:
                models.download(spec, model_path, log)
            except (OSError, models.ModelError) as e:
                raise Abort(f"model download failed: {e}") from None
        import torch  # deferred: slow to import, not needed for --dry-run
        if args.device.startswith("cuda") and not torch.cuda.is_available():
            raise Abort("CUDA isn't available to PyTorch. A CUDA GPU is needed (CPU would take weeks); "
                        "pass --device cpu to try anyway.")
        from .upscaler import Upscaler
        upscaler = Upscaler(model_path, args.device)
        log(f"loaded {upscaler.name} on {torch.cuda.get_device_name(0) if args.device.startswith('cuda') else args.device}")

        # 6. process
        return process_all(todo, counts, prints, upscaler, limits, key, state, s3d_dir, rate, log)


def process_all(todo, counts, prints, upscaler, limits, key, state, s3d_dir, rate, log) -> int:
    """rate: seconds per megapixel to assume until this run has measured its own."""
    total = sum(c[0] for c in counts.values())
    total_px = sum(c[1] for c in counts.values())
    run_start, done_tex, done_px, errors = time.time(), 0, 0, 0
    tmp: Path | None = None

    def overall_info(tex: int, px: int) -> tuple[str, float, float]:
        # ETA from this run's own speed (seconds per source megapixel) once a few
        # megapixels are done; until then, the speed the estimate used.
        elapsed = time.time() - run_start
        r = elapsed / (px / 1e6) if px >= 2_000_000 else rate
        eta = max(0, total_px - px) / 1e6 * r
        return f"{tex}/{total} textures · {fmt_time(elapsed)} · ETA {fmt_time(eta)}", elapsed, eta

    with ThreadPoolExecutor(8) as pool, Bars(log, total_px) as bars:
        try:
            for n, path in enumerate(todo, 1):
                tag = f"[{n}/{len(todo)}] {path.name}"
                log(f"{tag}: {counts.get(path.name, (0, 0))[0]} textures")
                t0 = time.time()
                out_file = s3d_dir / path.name
                last = {"logged": 0}

                def progress(k, of, px, px_of, start=t0, last=last, name=path.name):
                    now = time.time()
                    bars.archive_update(name, k, of, px, px_of, now - start)
                    bars.overall_update(done_px + px, total_px, overall_info(done_tex + k, done_px + px)[0])
                    # run.log (and the terminal when there are no bars): a line per chunk.
                    step = k // pipeline.CHUNK if k < of else of
                    if of > pipeline.CHUNK and step > last["logged"]:
                        last["logged"] = step
                        log(f"    {k}/{of} textures ({fmt_time(now - start)})", terminal=not log.tty)

                try:
                    buf = path.read_bytes()
                    sha = hashlib.sha256(buf).hexdigest()
                    if sha != prints[path][0]:
                        log("    note: archive changed since fingerprinting; using the current file")
                    data, result = pipeline.process(buf, path.name, upscaler, limits, pool, progress)
                    entry = st.Entry(sha, len(buf), key, "upscaled" if data else "nothing",
                                     textures=result.upscaled, pixels=result.pixels,
                                     seconds=round(time.time() - t0, 1))
                    if data is None:
                        out_file.unlink(missing_ok=True)  # a stale output from an older version
                        log(f"{tag}: nothing eligible")
                    else:
                        tmp = out_file.with_name(out_file.name + ".tmp")
                        tmp.write_bytes(data)
                        os.replace(tmp, out_file)
                        tmp = None
                        entry.output_sha256 = hashlib.sha256(data).hexdigest()
                        entry.output_size = len(data)
                        log(f"{tag}: done in {fmt_time(entry.seconds)}, {fmt_bytes(len(buf))} -> {fmt_bytes(len(data))}")
                    done_tex += result.upscaled
                    done_px += result.pixels
                except Exception as e:  # record, keep going; retried next run
                    errors += 1
                    entry = st.Entry(prints[path][0], prints[path][1], key, "error", error=repr(e))
                    total_px -= counts.get(path.name, (0, 0))[1]
                    log(f"{tag}: ERROR {e!r}")
                state.record(path.name, entry)
                state.save()
                info, elapsed, eta = overall_info(done_tex, done_px)
                bars.overall_update(done_px, total_px, info + (f" · {errors} errors" if errors else ""))
                # The bars show this live in a terminal; run.log always gets it.
                log(f"    overall {done_tex}/{total} textures ({100 * done_px / max(1, total_px):.1f}% of pixels), "
                    f"elapsed {fmt_time(elapsed)}, ETA {fmt_time(eta)}" + (f", {errors} errors" if errors else ""),
                    terminal=not log.tty)
        except KeyboardInterrupt:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
            log("stopped. Finished archives are saved; run the same command again to continue.")
            return 130
    log(f"finished: {done_tex} textures in {fmt_time(time.time() - run_start)}, {errors} errors")
    log(f"copy the contents of {s3d_dir} into your EverQuest directory to use them")
    return 1 if errors else 0


def _stop_on_sigterm() -> None:
    """Treat `kill <pid>` like Ctrl-C: finish cleanly, keep completed work."""
    def handler(signum, frame):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, handler)
    except (ValueError, OSError):  # not in the main thread, or unsupported
        pass


def main(argv=None) -> int:
    args = parse_args(argv)
    _stop_on_sigterm()
    try:
        return run(args)
    except Abort as e:
        print(f"eq-hd: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("eq-hd: stopped", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
