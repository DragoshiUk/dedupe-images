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
  `review_gui.py`, nothing else uses it.

## Conventions

- Nothing is destructive by default — everything quarantines rather than
  deletes, with permanent-delete gated behind an explicit opt-in
  checkbox/button in the GUI. Preserve that default when touching any of
  this code. Upscale is the one deliberate exception to "everything goes
  through quarantine": it's additive (writes a new `_upscaled` file,
  never touches the original), so it has no manifest/restore involvement
  at all - don't try to route it through the quarantine system.
- Keep `README.md` to what it is / how to use it / safety model — no
  debugging narrative, bug writeups, or changelog-style entries. Put
  that kind of detail in commit messages instead.
