# Before distribution

All four items below are done (2026-11-14). See README.md for how each works.

- [x] **Incremental re-runs.** `output/state.json` records each archive's sha256,
  the settings key (model hash, size limits, pipeline version) and every output
  hash produced. Re-runs process only new, changed, failed, or settings-changed
  archives, and recognise their own output after it has been copied into the
  EQ directory.
- [x] **Cleaned up for distribution.** The `src/eq_hd/` package has a single
  `eq-hd` entry point, tests in `tests/`, and a README. The prototype scripts are
  removed.
- [x] **Output stays in the repo.** Results go to `output/s3d/` (git-ignored). The
  tool never writes to the EQ directory, and refuses output paths inside it.
- [x] **One input: the EQ path.** `uv run eq-hd --eq-directory /mnt/c/EverQuest`,
  with Windows paths translated under WSL. Safety checks: the EQ install is
  validated (eqgame.exe and archives), a confirmation prompt shows both paths,
  free space is checked, one run per output directory (lock file), writes are
  atomic, partial files are removed and `--dry-run` is available. (Outputs carry
  an `EQHD` footer marker, so they're recognised even without a state file.)

# GUI (branch `gui`, 2026-11-15)

- [x] Engine: `--events`, pause/resume/stop on stdin, texture re-use, `--install`.
- [x] Window, engine setup with download progress.
- [x] Try the window on Windows (the user): working; full render running from the window.
- [x] Windows download: a portable zip (`make windows`; design and plan 2026-11-15-windows-zip).
- [ ] Try the zip on Windows (the user): extract, open, first-run engine setup, a short render.
- [ ] Push to GitHub; create environments `prerelease` and `release` with yourself as required reviewer; tag v0.1.0 (first real run of `windows-zip.yml`).
- [ ] `.eqg` archives: one-zone in-game test, then normal maps left alone and
      uncompressed DDS support. Its own plan.
- [ ] Spell effect textures: the loose files in `SpellEffects/`, `ActorEffects/`, `EnvEmitterEffects/`
      (677, used by the newer effects; the classic sprites in `gequip.s3d` are already done).
      Needs reading/installing loose files and `.tga`; check in game that glows upscale well first.
- [ ] Split `cli.run()` into named steps (with the `.eqg` work, which reshapes it anyway).
- [ ] Deferred minors from the reviews: two app copies rebuilding one engine env; Stop enabled during a
      delete; folder-size thread can stick on "calculating"; failed texture counts are cached; duplicate
      names in a PFS archive; case-sensitive output-inside-EQ check under WSL; corrupt state.json gives a
      raw error.
