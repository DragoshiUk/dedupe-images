# dedupe-images

Tools for `/mnt/dragonhoard/tuqiri/commissions` (a flat root directory
plus nested per-artist subdirectories, some of which contain identical
files, some of which are themselves duplicated as sibling folders like
`Foo`/`Foo_1`, and some of which have inconsistent letter casing). Note:
this directory appears to be actively synced from elsewhere (mixed
`dragoshi`/`onepassword-cli` ownership, new files showing up unprompted)
— that's independent of these tools and not something they need to
account for beyond not assuming the tree is static between runs.

**Start here: `review_gui.py`** — "Image Collection Super De-Duper", a
local browser GUI covering three operations from one page: **Identical
Files** (exact-hash dedupe), **Normalisation** (directory merging +
lowercase renaming, combined), and **Visually Similar** (interactive
perceptual near-duplicate review). Three menus: **Operations** (inspect
each of the three above, or make Visually-Similar decisions — nothing
runs from here), **Jobs** (Pending Jobs: tick any mix of the three,
preview one combined summary, then Start), **Quarantine** (what's parked
in `_duplicates_quarantine/`, with a permanent-delete option once you're
happy). The CLI scripts (`dedupe_images.py`, `find_near_duplicates.py`,
`apply_review.py`) still work standalone for scripting/automation, and
the GUI is built on top of the exact same functions they use — there's
one implementation of every safety rule, not two.

Two deliberate, separately-warned exceptions to "nothing is ever
deleted": Pending Jobs' **"skip quarantine"** checkbox permanently
deletes duplicates immediately instead of quarantining them (still
logged in the manifest as an audit trail, but unrestorable), and the
Quarantine tab's **delete** button permanently empties the quarantine
folder. Both require an explicit tick before the button is even
clickable, and say plainly that they cannot be undone.

```bash
cd /home/dragoshi/Projects/dedupe-images
uv run review_gui.py                                     # picker starts at $HOME
uv run review_gui.py /mnt/dragonhoard/tuqiri/commissions  # scans immediately
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
  completed. `dedupe_images.py --restore` undoes *any* of it — GUI or
  CLI, any of the four operations, any mix of them, right up until the
  quarantine folder is actually deleted — because they all write to the
  same manifest with the same schema. A permanently-deleted entry (from
  either opt-in above) is logged as `type: "deleted"` — `--restore` skips
  it with a clear "cannot be restored" message rather than crashing, and
  leaves it in the manifest as a permanent record.
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
Normalisation — inspect or decide, nothing runs from here) → **Jobs**
(Pending Jobs: pick a mix, review, Start) → **Quarantine**.

**Operations → Identical Files** — `dedupe_images.py`'s SHA-256 pass,
read-only preview. Pick a `--prefer`-equivalent keeper strategy, Rescan.

**Operations → Visually Similar** — the interactive perceptual-hash
reviewer. Click an image (or press its number key) to toggle it between
kept and discarded; more than one image per group can be kept, since a
group isn't always "one true original, N copies" — sometimes near-hash-
matches are legitimately different images (or crops/edits) worth keeping
both of. Each image is captioned with objective numbers (resolution, an
edge-variance sharpness estimate, file size); the highest-resolution one
is badged "suggested" as a starting point, not a verdict. `Enter`
confirms the current group's keep/discard split and advances; `S` skips
without deciding; arrow keys revisit past groups. Decisions save to disk
as you go; actually running them happens later, from Pending Jobs.

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

**Quarantine** — shows what's currently sitting in
`_duplicates_quarantine/` (file count, total size — correctly singular
when there's exactly one), with a "Delete quarantine folder permanently"
button gated behind a warning box and a must-tick "I understand this
cannot be undone" checkbox before the button even becomes clickable.

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
Pillow into an ephemeral environment automatically. Binds to `127.0.0.1`
only, never reachable from the network.

```bash
uv run review_gui.py /mnt/dragonhoard/tuqiri/commissions
# --threshold N     Visually-Similar Hamming distance out of 64, default 8
# --port N          default 8765
# --no-browser      don't try to auto-open a tab
```

## dedupe_images.py (CLI)

The same four operations (minus interactive near-dup review, which needs
a human looking at images) as a scriptable CLI. Dry-run by default:

```bash
python3 dedupe_images.py /mnt/dragonhoard/tuqiri/commissions                                   # dry run
python3 dedupe_images.py /mnt/dragonhoard/tuqiri/commissions --merge-dirs --lowercase           # dry run, all passes
python3 dedupe_images.py /mnt/dragonhoard/tuqiri/commissions --merge-dirs --lowercase --execute # apply
python3 dedupe_images.py /mnt/dragonhoard/tuqiri/commissions --restore                          # undo (GUI actions too)
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
uv run find_near_duplicates.py /mnt/dragonhoard/tuqiri/commissions
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
python3 apply_review.py /mnt/dragonhoard/tuqiri/commissions          # dry run
python3 apply_review.py /mnt/dragonhoard/tuqiri/commissions --execute
```

