"""eq-hd: upscale EverQuest .s3d textures into a separate output directory.

  uv run eq-hd --eq-directory /mnt/c/EverQuest
  uv run eq-hd --eq-directory 'C:\\EverQuest' --dry-run

Upscaled archives are written to output/s3d/ (in this repo by default). The
EverQuest directory is only read, unless --install is given: then finished
archives are copied into it (only over the untouched originals, never while the
game runs). Re-running only processes archives that are new, changed (e.g. by a
patch), or were processed with different settings.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import multiprocessing
import os
import shutil
import signal
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

from . import PIPELINE_VERSION, install, models, paths, pfs, pipeline
from . import state as st
from .cache import Counts, Fingerprints
from .control import Control
from .osutil import pid_alive
from .reuse import Library
from .untracked import UNTRACKED
from .report import JsonReporter, Reporter, TerminalReporter, fmt_bytes, fmt_time

# Speed assumed before any run has been measured: test runs on an RTX 3090 averaged
# 0.66 s per texture at 51,500 source pixels each. After a run, measured speed is used.
DEFAULT_SECONDS_PER_MEGAPIXEL = 12.8
WORST_CASE_GROWTH = 16  # output archive size / source size; observed 6-15x
SAFETY_MARGIN = 1 * 2**30


class Abort(Exception):
    pass


# ---------------------------------------------------------------- helpers

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
                if pid and pid_alive(pid):
                    raise Abort(f"another eq-hd run (pid {pid}) is using this output directory. "
                                f"If that's wrong, delete {self.path}.") from None
                self.path.unlink(missing_ok=True)
        raise Abort(f"couldn't take the lock {self.path}")

    def __exit__(self, *exc):
        if self.held:
            self.path.unlink(missing_ok=True)


# ---------------------------------------------------------------- steps

def parse_args(argv):
    ap = argparse.ArgumentParser(prog="eq-hd", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--eq-directory", required=True, metavar="PATH",
                    help="EverQuest install (only read, unless --install). Windows paths like C:\\EverQuest work under WSL.")
    ap.add_argument("--install", action="store_true",
                    help="copy finished archives into the EverQuest directory (as each one finishes; "
                         "held while the game is running). Start the game with eqgame.exe patchme: "
                         "the launcher puts the original files back.")
    ap.add_argument("--output", metavar="PATH", type=Path, default=None,
                    help="where results go (default: output/ in this repo). Must be outside the EQ directory.")
    ap.add_argument("--include", metavar="GLOB", action="append",
                    help="only archives matching this name pattern, e.g. 'nexus*.s3d' (repeatable; default: all .s3d)")
    ap.add_argument("--model", metavar="PATH", type=Path, default=None,
                    help=f"model file (default: models/{models.DEFAULT_MODEL.filename}, downloaded and verified if missing)")
    ap.add_argument("--force", action="store_true", help="re-render archives even if they're up to date")
    ap.add_argument("--dry-run", action="store_true", help="show what would be done, then stop")
    ap.add_argument("--no-render", action="store_true",
                    help="with --install: only copy archives that are already rendered; render nothing")
    ap.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    ap.add_argument("--device", default="cuda", help="torch device (default: cuda)")
    ap.add_argument("--events", action="store_true",
                    help="machine-readable output for front ends: one JSON object per line on stdout")
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
    """(name, [(texture, sha, pixels), ...]), or (name, None) if the archive is damaged."""
    path, limits = job
    try:
        return path.name, pipeline.count_eligible(path.read_bytes(), limits)
    except pfs.PfsError:
        return path.name, None
    except Exception:
        return path.name, []  # unreadable for another reason; the main loop will record the error


def count_textures(paths_: list[Path], limits: pipeline.Limits, log: Reporter) -> dict:
    """Eligible textures of each archive, read in parallel: {name: items or None (damaged)}."""
    found = {}
    # "spawn", not fork: with --events a thread is blocked reading stdin, and a forked
    # child closing its inherited stdin would wait forever on that thread's lock.
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(min(8, os.cpu_count() or 1), mp_context=ctx) as ex, \
            log.task("count", len(paths_)) as advance:
        for name, items in ex.map(_count_worker, [(p, limits) for p in paths_], chunksize=4):
            found[name] = items
            advance()
    return found


def remove_untracked_renders(s3d_dir: Path, state: st.State, log: Reporter) -> None:
    """Outputs of archives that are left out now (untracked.py), rendered by an earlier
    version: delete them and forget them, so they don't sit in the data folder."""
    names = sorted(n for n in state.archives if n.lower() in UNTRACKED)
    if not names:
        return
    for name in names:
        (s3d_dir / name).unlink(missing_ok=True)
        del state.archives[name]
    state.save()
    log(f"removed {len(names)} leftover render(s) of archives the EverQuest launcher doesn't track")


def check_output_consistency(s3d_dir: Path, state: st.State, log: Reporter) -> None:
    for tmp in s3d_dir.glob("*.tmp"):
        log(f"removing partial file from an interrupted run: {tmp.name}")
        tmp.unlink()
    unknown = {p.name for p in s3d_dir.glob("*.s3d")} - set(state.archives)
    if unknown:
        log(f"note: {len(unknown)} archive(s) in {s3d_dir} aren't in the state file and will be "
            "overwritten if they're processed again: "
            + ", ".join(sorted(unknown)[:5]) + (" ..." if len(unknown) > 5 else ""))


def resolve_model(args) -> tuple[Path, models.ModelSpec | None]:
    if args.model is not None:
        path = args.model.expanduser().resolve()
        if not path.is_file():
            raise Abort(f"model file {path} not found")
        return path, None
    return paths.repo_dir() / "models" / models.DEFAULT_MODEL.filename, models.DEFAULT_MODEL


def confirm(args, question: str) -> None:
    if args.yes:
        return
    if args.events or not sys.stdin.isatty():
        raise Abort("not running interactively; re-run with --yes to proceed without confirmation")
    if input(f"{question} [y/N] ").strip().lower() not in ("y", "yes"):
        raise Abort("cancelled")


def run(args, log: Reporter, control: Control | None = None) -> int:
    control = control or Control(log)
    if args.no_render and not args.install:
        raise Abort("--no-render only makes sense with --install")
    limits = pipeline.Limits()  # fixed: 17-512 px (not a user setting)

    # 1. paths
    try:
        eq = paths.find_eq_install(args.eq_directory)
        out = paths.check_output_dir(args.output or paths.default_output_dir(), eq)
    except paths.PathError as e:
        raise Abort(str(e)) from None
    archives = [p for p in eq.archives if p.name.lower() not in UNTRACKED]
    untracked = len(eq.archives) - len(archives)
    if args.include:
        archives = [p for p in archives if any(fnmatch.fnmatch(p.name.lower(), g.lower()) for g in args.include)]
        if not archives:
            raise Abort(f"no .s3d archives in {eq.path} match {args.include}")

    s3d_dir = out / "s3d"
    s3d_dir.mkdir(parents=True, exist_ok=True)
    log.attach(out / "run.log")
    log(f"EverQuest directory ({'will copy into it' if args.install else 'read only'}): {eq.path}  "
        f"[{len(eq.archives)} .s3d archives]")
    log(f"output directory:                {out}")
    if untracked:
        log(f"leaving out {untracked} archive(s) the EverQuest launcher doesn't track (it could never "
            "restore them, so they're not upscaled or installed)")

    with Lock(out / ".lock"):
        state = st.State(out / "state.json")
        if not args.dry_run:
            remove_untracked_renders(s3d_dir, state, log)
        check_output_consistency(s3d_dir, state, log)
        if args.install and not args.dry_run:
            n = install.clean_tmp(eq.path, [p.name for p in eq.archives])
            if n:
                log(f"removed {n} partial copy file(s) left in the EverQuest directory by an interrupted run")

        # 2. model and settings
        model_path, spec = resolve_model(args)
        need_download = spec is not None and not model_path.exists()
        model_sha = spec.sha256 if need_download else models.verify(model_path, spec)
        key = st.settings_key(PIPELINE_VERSION, model_sha, limits.min_side, limits.max_side)
        log(f"model: {model_path.name}" + (" (will be downloaded)" if need_download else "")
            + f"; textures {limits.min_side}-{limits.max_side} px are upscaled")

        # 3. fingerprint
        log(f"fingerprinting {len(archives)} archives ...")
        t0 = time.time()
        fingerprints = Fingerprints(out / "fingerprints.json")
        stats = {p: p.stat() for p in archives}
        prints = {p: fp for p in archives if (fp := fingerprints.get(p.name, stats[p]))}
        to_hash = [p for p in archives if p not in prints]  # new, or changed since last time
        with ThreadPoolExecutor(8) as ex, \
                log.task("fingerprint", sum(stats[p].st_size for p in to_hash), "bytes") as advance:
            for p, fp in zip(to_hash, ex.map(_hash_file, to_hash)):
                prints[p] = fp
                fingerprints.put(p.name, stats[p], fp)
                advance(fp[1])
        fingerprints.save(keep={p.name for p in eq.archives})
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

        # Outputs made before re-use existed have no texture index. Their sources
        # are unchanged, so index them now (reading only) and they can be re-used.
        backfill = [p for p in plan.get(st.CURRENT, [])
                    if state.archives[p.name].result == "upscaled" and not state.archives[p.name].textures_index]
        counts_cache = Counts(out / "counts.json")

        def count(paths_: list[Path]) -> dict:
            """count_textures(), remembered by archive sha256 and size limits."""
            keys = {p: Counts.key(prints[p][0], limits.min_side, limits.max_side) for p in paths_}
            found = {p.name: counts_cache.get(keys[p]) for p in paths_ if counts_cache.has(keys[p])}
            missing = [p for p in paths_ if p.name not in found]
            if missing:
                by_name = {p.name: p for p in missing}
                for name, items in count_textures(missing, limits, log).items():
                    found[name] = items
                    counts_cache.put(keys[by_name[name]], items)
            counts_cache.save(keep={Counts.key(fp[0], limits.min_side, limits.max_side) for fp in prints.values()})
            return found

        if backfill and not args.dry_run:
            log(f"indexing textures of {len(backfill)} earlier outputs for re-use ...")
            for name, items in count(backfill).items():
                if items:
                    state.archives[name].textures_index = {t: sha for t, sha, _ in items}
            state.save()

        statuses = {status: len(ps) for status, ps in plan.items()}
        # --install: rendered archives whose original is (back) in the EQ folder get copied.
        installer = install.Installer(eq.path, s3d_dir, state, log, control) if args.install else None
        to_copy = [p.name for p in plan.get(st.CURRENT, [])
                   if state.archives[p.name].result == "upscaled"] if installer else []

        def nothing_to_render() -> int:
            log.plan(dict(statuses=statuses, to_render=0, to_rebuild=0, to_copy=len(to_copy), textures=0, megapixels=0,
                          estimate_seconds=0, disk_needed=0, disk_free=shutil.disk_usage(out).free))
            if not to_copy:
                log("nothing to do")
                return 0
            log(f"to copy: {len(to_copy)} rendered archives into {eq.path}")
            if args.dry_run:
                log("dry run: nothing written")
                return 0
            confirm(args, f"Copy {len(to_copy)} archives into {eq.path}?")
            control.start_listening()
            for name in to_copy:
                installer.queue(name)
            installer.flush(wait=True)
            if installer.problem():
                raise Abort(installer.problem())
            return 0

        if not todo:
            return nothing_to_render()

        # 4. estimate
        log(f"counting textures in {len(todo)} archives ...")
        library = None if args.force else Library(state, key, s3d_dir)
        found = count(todo)
        # counts: {name: (textures, pixels) to upscale}, excluding what can be re-used
        counts, reusable, rebuild = {}, 0, 0
        for name, items in found.items():
            if items is None:
                counts[name] = None
                continue
            tiling = pipeline.is_tiling_archive(name)
            new = [(t, px) for t, sha, px in items if not (library and library.known(sha, tiling))]
            reusable += len(items) - len(new)
            counts[name] = (len(new), sum(px for _, px in new))
            if items and not new:
                rebuild += 1  # every texture re-usable (e.g. a geometry-only patch): repack only
        # Archives that are broken as shipped (eye_chr.s3d's name table lists a file
        # it doesn't contain) can't be repacked. Skip them quietly; a patch that fixes
        # one changes its fingerprint, and it's picked up again.
        damaged = [p for p in todo if counts.get(p.name, (0, 0)) is None]
        for p in damaged:
            log(f"skipping {p.name}: damaged in the EverQuest install (it can't be unpacked), left as it is")
            del counts[p.name]
            if not args.dry_run:
                state.record(p.name, st.Entry(prints[p][0], prints[p][1], key, "damaged"))
        if damaged:
            if not args.dry_run:
                state.save()
            todo = [p for p in todo if p not in damaged]
            if not todo:
                return nothing_to_render()
        if args.no_render:
            pending = sum(1 for p in todo if found.get(p.name))
            if pending:
                log(f"{pending} archive(s) still need rendering; they aren't installed until they are "
                    "(run without --no-render, or click Render in the window)")
            return nothing_to_render()
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
            f"source {fmt_bytes(src_bytes)}" + (f"; {reusable} more re-used from earlier outputs" if reusable else ""))
        log(f"estimate:   ~{fmt_time(total_px / 1e6 * rate)} ({basis})")
        log(f"disk:       up to {fmt_bytes(src_bytes * WORST_CASE_GROWTH)} of output ({fmt_bytes(free)} free)")
        log.plan(dict(statuses=statuses, to_render=sum(1 for p in todo if counts.get(p.name, (0, 0))[0]),
                      to_rebuild=rebuild, to_copy=len(to_copy), textures=total, megapixels=round(total_px / 1e6, 1),
                      estimate_seconds=round(total_px / 1e6 * rate), disk_needed=need, disk_free=free))
        if args.dry_run:
            for p in todo[:40]:
                log(f"  would process {p.name} ({counts.get(p.name, (0, 0))[0]} textures)")
            if len(todo) > 40:
                log(f"  ... and {len(todo) - 40} more")
            if to_copy:
                log(f"  and copy {len(to_copy)} already rendered archives into {eq.path}")
            log("dry run: nothing written")
            return 0
        if free < need:
            raise Abort(f"not enough free space in {out}: need up to {fmt_bytes(need)}, have {fmt_bytes(free)}")
        confirm(args, f"Upscale {len(todo)} archives into {s3d_dir}"
                + (f" and copy them (and {len(to_copy)} already rendered) into {eq.path}?" if installer else "?"))
        control.start_listening()
        if installer:  # copy what's already rendered right away (unless the game is running)
            for name in to_copy:
                installer.queue(name)
            installer.flush(wait=False)

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
        if args.device.startswith("cuda"):
            control.on_pause = torch.cuda.empty_cache  # let a game started meanwhile have the VRAM

        # 6. process
        code = process_all(todo, counts, prints, upscaler, limits, key, state, s3d_dir, rate, log, control,
                           library, installer)
        if installer and installer.problem() and code == 0:
            raise Abort(installer.problem())  # rendered fine, but couldn't (fully) install: say so
        return code


def process_all(todo, counts, prints, upscaler, limits, key, state, s3d_dir, rate, log, control,
                library: Library | None = None, installer: install.Installer | None = None) -> int:
    """rate: seconds per megapixel to assume until this run has measured its own."""
    total = sum(c[0] for c in counts.values())
    total_px = sum(c[1] for c in counts.values())
    run_start, done_tex, done_px, errors = time.time(), 0, 0, 0
    tmp: Path | None = None

    def overall(tex: int, px: int) -> tuple[float, float]:
        # ETA from this run's own speed (seconds per source megapixel) once a few
        # megapixels are done; until then, the speed the estimate used.
        elapsed = time.time() - run_start
        r = elapsed / (px / 1e6) if px >= 2_000_000 else rate
        eta = max(0, total_px - px) / 1e6 * r
        log.overall(px, total_px, tex, total, elapsed, eta, errors)
        return elapsed, eta

    with ThreadPoolExecutor(8) as pool, log.display(total_px):
        try:
            for n, path in enumerate(todo, 1):
                tag = f"[{n}/{len(todo)}] {path.name}"
                log(f"{tag}: {counts.get(path.name, (0, 0))[0]} textures")
                t0 = time.time()
                out_file = s3d_dir / path.name
                last = {"logged": 0}

                def progress(phase, d, of, tex, start=t0, last=last, name=path.name):
                    now = time.time()
                    log.archive(name, phase, d, of, tex, now - start)
                    if phase == "upscaling":
                        k, n_tex = tex
                        overall(done_tex + k, done_px + d)
                        # run.log (and a terminal without bars): a line per chunk.
                        step = k // pipeline.CHUNK if k < n_tex else n_tex
                        if n_tex > pipeline.CHUNK and step > last["logged"]:
                            last["logged"] = step
                            log(f"    {k}/{n_tex} textures ({fmt_time(now - start)})", terminal=not log.live)

                try:
                    buf = path.read_bytes()
                    sha = hashlib.sha256(buf).hexdigest()
                    if sha != prints[path][0]:
                        log("    note: archive changed since fingerprinting; using the current file")
                    data, result = pipeline.process(buf, path.name, upscaler, limits, pool, progress,
                                                    checkpoint=control.checkpoint,
                                                    reuse=library.reuser(path.name) if library else None)
                    entry = st.Entry(sha, len(buf), key, "upscaled" if data else "nothing",
                                     textures=result.upscaled, pixels=result.pixels,
                                     seconds=round(time.time() - t0, 1), textures_index=result.index)
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
                        log(f"{tag}: done in {fmt_time(entry.seconds)}, {fmt_bytes(len(buf))} -> {fmt_bytes(len(data))}"
                            + (f", {result.reused} re-used" if result.reused else ""))
                    done_tex += result.upscaled
                    done_px += result.pixels
                    # Textures counted before the run but re-used from an archive rendered
                    # earlier in this run weren't upscaled: drop them from the totals, so
                    # the overall progress ends full (and the ETA isn't padded).
                    counted_tex, counted_px = counts.get(path.name, (0, 0))
                    total += result.upscaled - counted_tex
                    total_px += result.pixels - counted_px
                except Exception as e:  # record, keep going; retried next run
                    errors += 1
                    log.errors = errors
                    entry = st.Entry(prints[path][0], prints[path][1], key, "error", error=repr(e))
                    old = state.archives.get(path.name)
                    if old and old.settings_key == key and old.textures_index and old.output_size:
                        # The earlier output is still there, untouched: keep its textures re-usable.
                        entry.textures_index, entry.output_size = old.textures_index, old.output_size
                    total_px -= counts.get(path.name, (0, 0))[1]
                    log(f"{tag}: ERROR {e!r}")
                state.record(path.name, entry)
                state.save()
                if library:
                    library.add(path.name, entry)
                if installer and entry.result == "upscaled":
                    installer.queue(path.name)  # copy as we go
                    installer.flush(wait=False)
                elapsed, eta = overall(done_tex, done_px)
                # Shown live by the bars or events; run.log always gets it.
                log(f"    overall {done_tex}/{total} textures ({100 * done_px / max(1, total_px):.1f}% of pixels), "
                    f"elapsed {fmt_time(elapsed)}, ETA {fmt_time(eta)}" + (f", {errors} errors" if errors else ""),
                    terminal=not log.live)
            if installer:
                installer.flush(wait=True)
        except KeyboardInterrupt:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
            log(f"stopped: {control.stop_reason or 'interrupted (Ctrl-C, or a signal from the console)'}")
            log("Finished archives are saved; run the same command again to continue.")
            return 130
    log(f"finished: {done_tex} textures in {fmt_time(time.time() - run_start)}, {errors} errors")
    if not installer:
        if isinstance(log, JsonReporter):  # the window
            log("click Install to copy them into EverQuest")
        else:
            log("to copy them into EverQuest, run the same command with --install")
    return 1 if errors else 0


def _stop_on_sigterm() -> None:
    """Treat `kill <pid>` like Ctrl-C: finish cleanly, keep completed work."""
    def handler(signum, frame):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, handler)
    except (ValueError, OSError):  # not in the main thread, or unsupported
        pass


def events_stream():
    """(stream for events, function that undoes this). Events get their own copy of
    stdout, and file descriptor 1 is pointed at stderr, so anything else written to
    stdout (C libraries, worker processes, which inherit descriptor 1) lands in
    stderr instead of in the middle of the events. When stdout isn't the process's
    own (e.g. captured in tests), events just go to sys.stdout."""
    try:
        if sys.stdout is not sys.__stdout__:
            raise ValueError
        sys.stdout.flush()
        fd = os.dup(1)
        os.dup2(2, 1)
    except (OSError, ValueError, AttributeError):
        return sys.stdout, lambda: None
    stream = os.fdopen(fd, "w", encoding="utf-8", newline="\n", closefd=False)

    def restore() -> None:
        stream.flush()
        sys.stderr.flush()
        os.dup2(fd, 1)
        os.close(fd)

    return stream, restore


def main(argv=None) -> int:
    args = parse_args(argv)
    _stop_on_sigterm()
    stdout = sys.stdout
    restore = lambda: None  # noqa: E731
    if args.events:
        # stdout carries only events; anything else printed (library warnings,
        # stray prints) goes to stderr.
        events, restore = events_stream()
        log: Reporter = JsonReporter(stream=events)
        sys.stdout = sys.stderr
    else:
        log = TerminalReporter()
    # pause / resume / stop from the front end; read once the worker pools are done
    control = Control(log, input=sys.stdin if args.events else None)
    code = 2
    try:
        code = run(args, log, control)
    except Abort as e:
        log.error(str(e))
        code = 2
    except KeyboardInterrupt:
        reason = control.stop_reason or "interrupted (Ctrl-C, or a signal from the console)"
        log(f"stopped: {reason}")
        if not args.events:
            print("eq-hd: stopped", file=sys.stderr)
        code = 130
    finally:
        sys.stdout = stdout
        log.result(code, log.errors)
        restore()
    return code


if __name__ == "__main__":
    sys.exit(main())
