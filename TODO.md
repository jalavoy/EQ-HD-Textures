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
  atomic, partial files are removed, `--dry-run` is available, and a run refuses
  to start if outputs exist without a state file.
