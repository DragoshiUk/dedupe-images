# dedupe-images

Image dedup/normalization tools for a large, messy image collection.
See `README.md` for what each script/mode does and how to run them.

## Structure

- `review_gui.py` — local browser GUI, the primary entry point. Built on
  top of the same functions the CLI scripts use (one implementation of
  every safety rule, not two).
- `dedupe_images.py`, `find_near_duplicates.py`, `apply_review.py` —
  standalone CLI scripts for scripting/automation.
- `upscale.py` — AI upscale (Real-ESRGAN) logic backing the GUI's Upscale
  tab. Library module only, not a standalone CLI - imported by
  `review_gui.py`, nothing else uses it. It's the only part that shells
  out to a GPU tool and streams per-item progress/cancellation back
  through `review_gui.py`'s `Progress` object.
- Dependency management is split, deliberately, per file: `review_gui.py`
  and `find_near_duplicates.py` declare `pillow` via PEP 723 inline script
  metadata and are meant to be run with `uv run` (or directly, since
  they're executable - `uv` picks up the shebang); `dedupe_images.py` and
  `apply_review.py` are stdlib-only on purpose and run with plain
  `python3`. `upscale.py` imports `PIL` but is never run standalone, so it
  rides on whichever of the two uv-run entry points imported it.

## Conventions

- Nothing is destructive by default — everything quarantines rather than
  deletes, with permanent-delete gated behind an explicit opt-in
  checkbox/button in the GUI. Preserve that default when touching any of
  this code. Upscale is the one deliberate exception to "everything goes
  through quarantine": it's additive (writes a new file - affixed name
  and/or a separate output directory - never touches the original), so it
  has no manifest/restore involvement at all - don't try to route it
  through the quarantine system. Two things keep "additive" true and must
  stay: the empty-affix guard (only allowed alongside a separate output
  dir), and skipping any file whose output path already exists unless the
  "Overwrite Existing" opt-in is ticked.
- `realesrgan-ncnn-vulkan` (the upscale GPU tool) has three quirks that
  are worked around on purpose in `upscale.py` - don't undo them:
  `realesrgan-x4plus-anime` is 4x-only, so it's always run at `-s 4` and
  the exact size comes from a Lanczos resample (passing `-s 2`/`-s 3`
  corrupts its tile stitching into a checkerboard); it truncates a `-i`
  path at the first space (fed a space-free symlink instead); and it can
  exit 0 having written nothing / a partial image on VRAM pressure (each
  pass is retried down `TILE_LADDER` and a missing/empty output is treated
  as failure).
- An upscale run locks the whole webui (`body.upscale-running` disables
  the tabs / directory switcher) and replaces the Upscale pane with a live
  output list + Stop button. It streams to the bottom status bar, never a
  modal. Keep that shape if you touch the run flow.
- Upscale only considers `UPSCALE_EXTENSIONS` (png/jpg/jpeg/webp) in
  `upscale.py` — narrower than `DEFAULT_EXTENSIONS` (also gif/bmp/tif/tiff)
  used everywhere else. Not an oversight; Real-ESRGAN's usable input set
  is the reason. An image within `AI_MIN_FACTOR` (1.5x) of the target
  skips the GPU pass and gets a plain Lanczos resize instead — expected,
  not a bug, if someone reports "upscale didn't really upscale".
- `upscale.py` sets `Image.MAX_IMAGE_PIXELS = None`, disabling Pillow's
  decompression-bomb guard, deliberately: these are the user's own local
  art files, not untrusted uploads. Don't reinstate the guard here, and
  don't copy this pattern anywhere that might process untrusted input.
- `upscale.py`'s `RELEASE_URL` pins one specific `realesrgan-ncnn-vulkan`
  release and is bumped by hand, not auto-updated. `install_tool()`'s
  extraction logic assumes that release's exact zip layout (a
  `realesrgan-ncnn-vulkan` binary alongside a `models/` dir under some
  prefix) — if the URL is ever bumped, re-check the new archive's layout
  before assuming extraction still works.
- `dedupe_images.py`'s name-normalization (`transform_name`,
  `to_camel_case`) supports independent `case_style`
  (none/lower/camel) and `sep_style` (none/spaces-dash/spaces-underscore/
  dash-underscore/underscore-dash) axes, but the CLI's `--lowercase` flag
  only ever invokes it with `case_style="lower", sep_style="none"` — there
  is no CLI flag to reach camelCase or separator conversion. Only
  `review_gui.py`'s Normalisation tab exposes the other combinations (via
  `case_style`/`sep_style` on `/api/normalise/preview` and the run
  endpoints). This is a real gap, not a hidden feature to preserve as-is —
  if CLI parity is ever wanted, add `--case-style`/`--sep-style` choices
  mirroring `CASE_STYLES`/`SEPARATOR_STYLES` rather than reinventing the
  transform.
- Keep `README.md` to what it is / how to use it / safety model — no
  debugging narrative, bug writeups, or changelog-style entries. Put
  that kind of detail in commit messages instead.

## upscale-to-max.sh

Standalone bash upscaler, folded in from the retired `max-side-upscale`
repo on 2026-11-06. Independent of `upscale.py` (the GUI's library): it
shells out to `realesrgan-ncnn-vulkan` with `-s 2/3/4` where `upscale.py`
uses native 4x only, because `-s` is not native to the x4plus model and
can corrupt tile stitching there. If that bites the script too, align it
with `upscale.py`. GPU tooling tested on an RTX 4080 SUPER.
