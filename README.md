# dedupe-images

Tools for cleaning up a large, messy image collection: nested
subdirectories, some containing identical files, some themselves
duplicated as sibling folders (e.g. `Foo`/`Foo_1`), and inconsistent
letter casing. Assumes the tree may change between runs (e.g. if it's
being actively synced from elsewhere) — operations always rescan fresh
rather than trusting a stale plan.

**Start here: `review_gui.py`** — "Image Collection Super De-Duper", a
local browser GUI covering four operations from one page: **Identical
Files** (exact-hash dedupe), **Normalisation** (directory merging +
lowercase renaming, combined), **Visually Similar** (interactive
perceptual near-duplicate review), and **Upscale** (AI resolution
upscaling via Real-ESRGAN). Three menus: **Operations** (inspect each of
the first three above, make Visually-Similar decisions, or run Upscale
directly — nothing else runs from here), **Jobs** (Pending Jobs: tick any
mix of Identical Files / Normalisation / Visually Similar, preview one
combined summary, then Start), **Quarantine** (every file currently
parked in `_duplicates_quarantine/`, listed individually with what it
was a duplicate of and when, a per-file or restore-all button, and a
permanent-delete option once you're happy). The CLI scripts
(`dedupe_images.py`, `find_near_duplicates.py`, `apply_review.py`) still
work standalone for scripting/automation, and the GUI is built on top of
the exact same functions they use — there's one implementation of every
safety rule, not two.

Upscale sits outside that quarantine/restore system entirely: it's
additive, not destructive — it only ever writes a new file for an original
that's under the target resolution (by default `<name>_upscaled.<ext>`
next to the source; optionally a chosen prefix/suffix and/or a separate
output directory anywhere on the filesystem), never moves or deletes
anything, so it has no manifest entry and nothing to restore. It has its
own direct Start button on its own tab instead of going through Pending
Jobs.

Two deliberate, separately-warned exceptions to "nothing is ever
deleted": Pending Jobs' **"skip quarantine"** checkbox permanently
deletes duplicates immediately instead of quarantining them (still
logged in the manifest as an audit trail, but unrestorable), and the
Quarantine tab's **delete** button permanently empties the quarantine
folder. Both require an explicit tick before the button is even
clickable, and say plainly that they cannot be undone.

```bash
cd dedupe-images
uv run review_gui.py                    # picker starts at $HOME
uv run review_gui.py /path/to/images    # scans immediately
```

## Safety model (applies everywhere — GUI and CLI alike)

- Matches files by SHA-256 content hash only for "exact" operations —
  never perceptual/fuzzy similarity, so similar-but-different artwork
  can't be mistaken for a duplicate there.
- **Nothing is ever deleted by a dedupe/merge/rename operation itself,
  by default.** Everything that would be removed is *moved* into
  `_duplicates_quarantine/` inside the scanned root, preserving its
  relative path. The only real deletions are opt-in and separately
  warned: Pending Jobs' "skip quarantine" checkbox (or the CLI's
  `--delete`), and the Quarantine tab's delete button (or `rm -rf`
  yourself) — deliberate, disclosed exceptions, never a side effect of
  a normal run.
- **A name collision with genuinely different content is left alone and
  flagged as a conflict by default** — never overwritten or guessed at.
  Normalisation's "rename conflicting files" option (or the CLI's
  `--rename-conflicts`) is an opt-in way to resolve these automatically
  instead: the incoming file is renamed to a unique name so both
  survive, rather than needing manual review.
- Every move is recorded in one shared `dedupe_manifest.json` inside that
  quarantine folder, written in a `finally` block so even a run that
  errors partway through still saves a manifest for whatever it
  completed. That manifest can be undone right from the **Quarantine**
  tab (Restore a single file, or Restore all) or with
  `dedupe_images.py --restore` from the command line — both run the exact
  same restore logic, so they stay in sync. Works for any mix of
  Identical Files / Normalisation / Visually Similar output, right up
  until the quarantine folder is actually deleted, because they all write
  to the same manifest with the same schema. A permanently-deleted entry
  (from either opt-in above) is logged as `type: "deleted"` — restoring
  skips it with a clear "cannot be restored" message rather than
  crashing, and leaves it in the manifest as a permanent record.
- Perceptual (near-duplicate) matching is fuzzy by nature, so it's never
  auto-applied: a human picks which images in a group to keep by looking
  at them, and even that only produces a plan you still have to review
  and confirm before anything moves.
- **Pending Jobs' Start always recomputes Identical Files / Normalisation
  plans fresh** immediately before applying them — never replays the
  snapshot you reviewed, since the tree can change underneath a long
  review session (see the active-sync note above). Visually-Similar
  decisions *are* replayed exactly as decided (there's no "fresh" version
  of a human judgement call to recompute), and every individual move
  stays collision-safe regardless of which plan produced it — so this can
  never cause data loss, only mean the exact set that runs differs
  slightly from the reviewed snapshot on the rare occasion the folder
  changed in between.

## review_gui.py

Menu: **Operations** (sub-tabs Identical Files / Visually Similar /
Normalisation / Upscale — the first three are inspect-or-decide only,
nothing runs from there; Upscale runs directly from its own tab) →
**Jobs** (Pending Jobs: pick a mix of the first three, review, Start) →
**Quarantine**.

**Operations → Identical Files** — `dedupe_images.py`'s SHA-256 pass,
read-only preview. Pick a `--prefer`-equivalent keeper strategy, Rescan.

**Operations → Visually Similar** — the interactive perceptual-hash
reviewer. Click an image (or press its number key) to toggle it between
kept and discarded; more than one image per group can be kept, since a
group isn't always "one true original, N copies" — sometimes near-hash-
matches are legitimately different images (or crops/edits) worth keeping
both of. Each image is captioned with objective numbers (resolution, an
edge-variance sharpness estimate, file size); the highest-resolution one
is badged "suggested" as a starting point, not a verdict. A stat bar
above the carousel shows the image count, position in the group list, and
a live keep/discard split as you toggle. `Prev`/`Next` (also the arrow
keys, or `Enter` for next) always save the current group's keep/discard
split before moving — even if you didn't touch anything, so browsing past
a group can never silently leave it undecided; `S` skips without
deciding. Decisions save to disk as you go; actually running them happens
later, from Pending Jobs. **Rescan** re-walks the directory from scratch
(picks up anything added, removed, or quarantined since the last scan),
restoring prior decisions for any group it rediscovers unchanged.

**Operations → Upscale** — AI-upscales images (Real-ESRGAN on the GPU) so
their longest side reaches a target resolution you set with a slider (up
to 8192px/8K), preserving aspect ratio; an image already at or above the
target isn't touched or listed. The eligible list updates live as you
drag the slider. A warning appears once a lot of images are queued, since
GPU upscaling processes one image at a time and can take a while per
image. Each result is a new file — originals are never modified or
removed — so unlike the other three operations this has no
quarantine/manifest/restore involvement at all, and runs immediately from
its own Start button rather than through Pending Jobs. Two output options:

- **Save to** — alongside each original (default), or a directory you pick
  anywhere on the filesystem (the picker has a "New folder" field for
  making one on the spot), in which case the source tree's sub-folders are
  recreated under it so same-named files in different folders can't
  collide.
- **Filename prefix/suffix** — the affix added to each output name
  (default `_upscaled`), and whether it goes before or after the name.
  Must be non-empty unless a separate output directory is set, so an
  upscaled file can never overwrite its original.
- **Overwrite existing upscaled files** — off by default: a file whose
  output path already exists is skipped and reported in the run summary,
  never silently replaced (and in "alongside" mode a file that already
  carries the affix isn't re-processed). Tick it to replace instead.

Needs `realesrgan-ncnn-vulkan`. If it's missing, the command line says so
at startup and the Upscale tab shows a **Download** button that fetches
the self-contained portable build (binary + models, ~45 MB) into
`~/.local/share/dedupe-images/` — no sudo, nothing system-wide — with a
live progress bar and log; every other tab works without it. Or install
the `realesrgan-ncnn-vulkan` AUR package. The actual resize/upscale logic
lives in `upscale.py` — unlike `dedupe_images.py`, `find_near_duplicates.py`,
and `apply_review.py`, it's a library module for `review_gui.py` only, not
a standalone command-line tool.

**Operations → Normalisation** — directory-merge (`Foo`+`Foo_1` sibling
folders) and lowercase-renaming, combined into one read-only preview.
"Rename conflicting file names" toggles the preview between the default
(leave a genuine content conflict alone, flagged) and the opt-in
resolution (rename the incoming file to a unique name so both survive).

**Enabling an operation happens on its own Operations tab**, not on Jobs —
each of the three (Identical Files, Visually Similar, Normalisation) has
an "Add to Pending Jobs" checkbox (greyed out with a reason when there's
nothing for it to do, e.g. Visually Similar before you've decided
anything). **Jobs is purely a review/run page**: it shows nothing
("No pending jobs") until at least one operation is enabled elsewhere,
and once something is, it auto-builds one combined, thumbnailed summary
of every planned action (path, size, action tag, which operation flagged
it, what it's a duplicate of) — no separate "preview" button, it just
reflects whatever's currently pending. From here the only actions are:
**Cancel** an individual pending operation (removes it, same as
unchecking it on its own tab), the **"skip quarantine"** toggle
(permanently delete instead of quarantine — a red warning appears when
on), and **Start**. A confirmation dialog (with extra-scary wording and a
required "I understand" tick when skip-quarantine is on) appears before
anything actually moves. If quarantine wasn't skipped, the result message
afterward explicitly says to visit the Quarantine tab to review and
permanently remove the files when ready.

**Quarantine** — lists every file currently sitting in
`_duplicates_quarantine/` individually (thumbnail, original path, size,
when it was quarantined, and what file was kept instead — pulled straight
from the manifest), not just a bare count. Each file gets its own
**Restore** button, plus a **Restore all** button for the whole folder at
once — both call the same restore logic as `dedupe_images.py --restore`.
A "Delete quarantine folder permanently" button is gated behind a warning
box and a must-tick "I understand this cannot be undone" checkbox before
the button even becomes clickable.

Directory selection: the root argument is optional (picker seeded at
`$HOME` if omitted). The header has its own dedicated row for this —
"Current Image Collection" and the path, in a visually distinct bar right
below the title — with a "Change" button next to it, switching to a
different directory at any time without restarting the server, browsing
anywhere on the filesystem. Every tab switch re-verifies the chosen
directory against the live server state rather than trusting a
client-side flag, so the header and every tab always agree. Picking a
directory, building a summary, and running
all show a real progress bar (percentage + phase name, e.g. "hashing
247/373") via background jobs the page polls every ~350ms — only one such
job can run at a time; starting a second while one's in flight is
rejected rather than queued or raced.

Needs Pillow; it's a `uv` inline-script, so run via `uv run review_gui.py`
or directly (`./review_gui.py`, it's executable) — either installs
Pillow into an ephemeral environment automatically. The Upscale tab
additionally needs `realesrgan-ncnn-vulkan` (a GPU tool, not a Python
package) — the tab can download a self-contained copy for you, or install
it yourself; every other tab works fine without it. Binds to `127.0.0.1`
only, never reachable from the network.

```bash
uv run review_gui.py /path/to/images
# --threshold N     Visually-Similar Hamming distance out of 64, default 8
# --port N          default 8765
# --no-browser      don't try to auto-open a tab
```

## dedupe_images.py (CLI)

The same four operations (minus interactive near-dup review, which needs
a human looking at images) as a scriptable CLI. Dry-run by default:

```bash
python3 dedupe_images.py /path/to/images                                   # dry run
python3 dedupe_images.py /path/to/images --merge-dirs --lowercase           # dry run, all passes
python3 dedupe_images.py /path/to/images --merge-dirs --lowercase --execute # apply
python3 dedupe_images.py /path/to/images --restore                          # undo (GUI actions too)
```

- `--merge-dirs` / `--lowercase` — enable those passes (file-level dedupe
  always runs). Applied in that order — file dedupe, then dir merge, then
  lowercase — because each later pass re-scans the live tree rather than
  working off a stale plan.
- `--rename-conflicts` — with `--merge-dirs`/`--lowercase`, resolve a
  genuine-content-difference name collision by renaming the incoming file
  to a unique name instead of leaving it as an unresolved conflict.
- `--delete` — **dangerous, irreversible.** Permanently deletes verified
  duplicates instead of quarantining them; `--restore` cannot bring these
  back (still logged in the manifest as `type: "deleted"`, an audit
  record only). Default off — quarantine is always the safe default.
- `--prefer {oldest,newest,shortest-path,longest-path}` — which copy of a
  duplicate FILE to keep. Default `oldest`.
- `--ext EXT [EXT ...]` — file extensions for the exact-dedupe pass
  (default: png, jpg, jpeg, gif, bmp, webp, tif, tiff). Dir-merge and
  lowercase always consider all files.
- Empty-directory cleanup always runs last, automatically, after
  `--execute` — removes any directory left empty by the passes above (or
  already empty beforehand). Not manifest-tracked, not undone by
  `--restore` — an empty directory holds no data, nothing to lose.
- `--restore` undoes manifest entries in reverse order (a nested rename's
  recorded path assumes its parent already has its new name, so undoing
  has to happen child-first), then prunes empty leftover directories from
  the quarantine folder and removes it once it's empty.

## find_near_duplicates.py (standalone report)

Same perceptual-hash grouping as the GUI's near-duplicates tab, but as a
report-only CLI: prints each group's paths/dimensions/sizes and writes a
labeled side-by-side comparison montage PNG per group, for when you want
a static report rather than the interactive reviewer. Never touches
files.

```bash
uv run find_near_duplicates.py /path/to/images
# --threshold N, --montage-dir DIR, --no-montage
```

## apply_review.py (standalone apply)

Reads `<root>/_near_duplicate_review/decisions.json` (written by either
`review_gui.py`'s near-dup tab or manually) and quarantines the
"discard" files through the same mechanism as everything else. A
decision entry is `{"keep": [relpath, ...], "discard": [relpath, ...],
"skipped": bool}` — `keep` can hold more than one path. The GUI's
near-dup "Apply" button calls the same `build_apply_plan()`/`apply_plan()`
functions this script uses, in-process, rather than shelling out.

```bash
python3 apply_review.py /path/to/images          # dry run
python3 apply_review.py /path/to/images --execute
```

