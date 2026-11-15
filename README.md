# eq-hd-textures

Upscales the textures in EverQuest's `.s3d` archives 4× with a local
super-resolution model, and writes repacked archives you can drop into your
install. It covers zones, zone objects, characters and equipment.

```bash
uv run eq-hd --eq-directory /mnt/c/EverQuest
```

Results land in `output/s3d/`. Copy them into your EverQuest directory yourself:

```bash
cp output/s3d/*.s3d /mnt/c/EverQuest/
```

The tool **never writes to the EverQuest directory**. To undo, run the launcher's
file check (it restores modified files), or copy your own backups back.

## Requirements

- An NVIDIA GPU with CUDA. The full install is about 108,000 textures, roughly
  21 hours on an RTX 3090.
- [uv](https://docs.astral.sh/uv/). It installs Python 3.12, PyTorch (CUDA 12.8)
  and the other dependencies on first run.
- Free disk space for the output: about 9× the size of the source archives
  (about 35 GB for a full live install). The tool checks before starting.

Works under WSL, where Windows paths are accepted (`--eq-directory 'C:\EverQuest'`),
as well as on Linux and Windows.

## Usage

```bash
uv run eq-hd --eq-directory PATH [options]
```

| Option | |
|---|---|
| `--dry-run` | Show what would be processed, the time estimate and disk needs, then stop |
| `--include GLOB` | Only some archives, e.g. `--include 'gequip*.s3d' --include 'nexus*.s3d'` |
| `-y`, `--yes` | Don't ask before starting (needed when not run from a terminal) |
| `--output PATH` | Somewhere other than `output/` (never inside the EQ directory) |
| `--force` | Re-render archives that are already up to date |
| `--min-size`, `--max-size` | Texture size range to upscale (default 17–512 px, largest side) |
| `--model PATH` | A different Spandrel-compatible model |

Progress goes to the terminal and to `output/run.log`, so you can follow a long
run from another terminal with `tail -f output/run.log`. Before starting, a run
estimates its duration from the speed measured on earlier runs (a rough RTX 3090
figure the first time). During the run, the ETA uses the run's own speed. Both
work per megapixel of source texture, so a mix of small and large textures
doesn't skew them.

### Stopping, resuming and patch days

Stop at any time with Ctrl-C and run the same command again. Archives that
finished are kept, and the one in progress starts over.

Every run fingerprints each archive (sha256) and compares it with
`output/state.json`. Only archives that are new, have changed (for example
after a patch), failed last time, or were made with different settings are
processed. A full fingerprint pass takes a few seconds.

Every output archive also carries an `EQHD` marker in its footer, a field the
client ignores. When you've copied outputs into the EverQuest directory, a later
run recognises them as its own and never upscales them twice. That works even if
`output/` has been deleted. If an installed output is out of date (you changed
settings, or a newer version of the tool would render it differently), the run
reports it as *installed, stale*. Restore the original with the launcher's file
check, then run again.

After copying, you can delete `output/s3d/` to save space. Keeping
`output/state.json` is what makes later runs incremental. Without it, every
archive that isn't installed is processed again.

## What it does to each archive

- **Finds textures by content.** Live stores DDS data in many files still named
  `.bmp`.
- **Upscales** DXT1/DXT5 DDS and classic 8-bit palettized BMPs whose largest
  side is 17–512 px. Smaller ones are mostly solid-colour swatches; larger ones
  are already high resolution. Everything else (geometry, DXT3,
  uncompressed formats) is copied through byte for byte.
- **Prepares the edges.** Colours are bled into transparent pixels first, so the
  model doesn't pull dark halos into visible edges. Zone textures are
  wrap-padded so tiling surfaces stay seamless, and character and equipment
  skins are mirror-padded.
- **Keeps each texture's format.**
  - DDS keeps its original header and format. Textures that had a mip chain
    get a full new one.
  - On/off transparency gets smooth outlines rather than enlarged pixel steps,
    without losing 1–2 px features like wires or grass. Graded alpha (DXT5
    masks) is resized smoothly.
  - 8-bit BMPs stay 8-bit BMPs with the same palette. The upscaled colours are
    mapped back onto palette entries 1–255, and index 0 (EQ's transparent key
    colour on masked materials) is put back exactly where the original had it.
    Transparency then behaves as before, without needing the zone's material
    data.
- **Repacks the archive faithfully**: same file order, filename-table marker,
  name case and stored checksums, including the few shipped entries whose
  checksum doesn't match their name.

## Model

The default is [4x-PBRify_UpscalerV4](https://openmodeldb.info/models/4x-PBRify-UpscalerV4)
(DAT2 architecture, CC0-1.0). It's downloaded into `models/` on first use and
checked against a pinned sha256. Models are loaded with PyTorch's
`weights_only=True`, so a `.pth` file can't run code.

## Development

```bash
make test        # or: uv run pytest
```

The tests build synthetic archives and use a stand-in upscaler, so they need
neither an EverQuest install nor a GPU.

```
src/eq_hd/
  cli.py        command line, safety checks, run loop, progress
  paths.py      EQ / output directory resolution and checks (incl. Windows paths)
  state.py      fingerprints and the up-to-date rules
  pipeline.py   per-archive selection and the CPU/GPU pipeline
  textures.py   DDS / BMP parsing, alpha handling, re-encoding
  upscaler.py   batched model inference (Spandrel + PyTorch)
  pfs.py        PFS archive reader / writer
  models.py     default model download and verification
```

Bump `PIPELINE_VERSION` in `src/eq_hd/__init__.py` whenever a change alters
output bytes, so existing outputs get re-rendered.

## Note

The upscaled textures are derived from Daybreak's copyrighted assets. This
repo distributes the tool, not the textures.
