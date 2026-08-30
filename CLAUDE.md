# dedupe-images

Image dedup/normalization tools for `/mnt/dragonhoard/tuqiri/commissions`.
See `README.md` for what each script/mode does and how to run them.

## Structure

- `review_gui.py` — local browser GUI, the primary entry point. Built on
  top of the same functions the CLI scripts use (one implementation of
  every safety rule, not two).
- `dedupe_images.py`, `find_near_duplicates.py`, `apply_review.py` —
  standalone CLI scripts for scripting/automation.

## Conventions

- Nothing is destructive by default — everything quarantines rather than
  deletes, with permanent-delete gated behind an explicit opt-in
  checkbox/button in the GUI. Preserve that default when touching any of
  this code.
- Keep `README.md` to what it is / how to use it / safety model — no
  debugging narrative, bug writeups, or changelog-style entries. Put
  that kind of detail in commit messages instead.
