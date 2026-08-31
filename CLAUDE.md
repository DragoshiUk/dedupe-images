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
- Keep `README.md` to what it is / how to use it / safety model — no
  debugging narrative, bug writeups, or changelog-style entries. Put
  that kind of detail in commit messages instead.
