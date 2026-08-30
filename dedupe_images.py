#!/usr/bin/env python3
"""
Safe, exact-hash image deduplicator with optional directory merging.

Finds byte-identical files (by SHA-256, not perceptual similarity, so
similar-but-different artwork is never at risk of being flagged) anywhere
under a root directory, including across nested subdirectories. For each
group of identical files it picks one "keeper" and, only when explicitly
told to, MOVES the rest into a quarantine folder (never deletes). Every
move is recorded in a JSON manifest so it can be undone with --restore.

With --merge-dirs, it also detects sibling directory pairs that look like
sync-tool duplicates (e.g. "Foo" and "Foo_1") and merges the suffixed
one's contents into the bare one:
  - a file that doesn't exist yet in the keeper dir is just moved over
  - a file that exists in both with IDENTICAL content is quarantined
    (same mechanism as the file-level pass, same manifest)
  - a file that exists in both with DIFFERENT content is left exactly
    where it is and reported as a conflict for manual review - it is
    never overwritten or guessed at
  - directories left completely empty by the merge are removed (safe -
    provably no data in them); directories with unresolved conflicts are
    left in place

Directory grouping only ever merges a suffixed directory into a *bare*
sibling that actually exists (e.g. "Foo_1" -> "Foo") - a set of only
suffixed directories like "Chapter_1"/"Chapter_2" with no bare "Chapter"
is left alone, since that's plausibly intentional separate content rather
than a duplicate.

With --lowercase, every file and directory name is normalized to
lowercase, applied top-down (parents before children) after file dedupe
and dir merge have run. A collision this creates is resolved the same
safe way throughout this script: identical content is quarantined,
differing content is left exactly where it is and reported as a conflict,
and a directory colliding with an existing lowercase directory is merged
into it (same mechanism as --merge-dirs) rather than overwritten.

Usage:
    # 1. Dry run first (default) - just reports what it *would* do.
    python3 dedupe_images.py /mnt/dragonhoard/tuqiri/commissions

    # Also detect/merge duplicate-looking sibling directories, and
    # normalize all names to lowercase:
    python3 dedupe_images.py /mnt/dragonhoard/tuqiri/commissions --merge-dirs --lowercase

    # 2. Once you've reviewed the report, actually apply it.
    python3 dedupe_images.py /mnt/dragonhoard/tuqiri/commissions --merge-dirs --lowercase --execute

    # 3. Changed your mind? Move everything back.
    python3 dedupe_images.py /mnt/dragonhoard/tuqiri/commissions --restore

Keeper selection (which copy of a duplicate FILE is left in place):
    Default "oldest" - the file with the earliest mtime is kept (best
    proxy for "the original"), ties broken by shortest relative path,
    then lexicographically. Override with --prefer.

Nothing is ever permanently deleted by this script.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff",
}

QUARANTINE_DIRNAME = "_duplicates_quarantine"
MANIFEST_NAME = "dedupe_manifest.json"
HASH_CHUNK = 1024 * 1024  # 1 MiB

DIR_SUFFIX_PATTERNS = [
    re.compile(r"^(.*)_\d+$"),
    re.compile(r"^(.*) \(\d+\)$"),
    re.compile(r"^(.*) [Cc]opy(?: \d+)?$"),
]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(HASH_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def unique_path(dest: Path) -> Path:
    """Return dest, or a __dupN-suffixed sibling of it if dest already exists."""
    if not dest.exists():
        return dest
    stem, suf = dest.stem, dest.suffix
    i = 1
    while True:
        candidate = dest.parent / f"{stem}__dup{i}{suf}"
        if not candidate.exists():
            return candidate
        i += 1


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_conflict(src: Path, dest: Path, root: Path, execute: bool, manifest: list,
                      rename_conflicts: bool) -> dict:
    """A collision was found at dest that isn't a verified-identical
    duplicate of src (differing content, or a file/directory type
    mismatch). Default: leave both alone, report a conflict. With
    rename_conflicts=True: rename src to a unique __dupN-suffixed sibling
    of dest instead, so both survive under distinct names rather than
    needing manual intervention."""
    rel = src.relative_to(root)
    if not rename_conflicts:
        print(f"  CONFLICT  {rel}  vs  {dest.relative_to(root)}  "
              "(different content - left in place, review manually)")
        return {"type": "conflict", "src": str(rel), "dest": str(dest.relative_to(root)), "kept": None}
    new_dest = unique_path(dest)
    print(f"  rename(conflict)  {rel}  ->  {new_dest.relative_to(root)}  "
          "(name collision with different content - kept separately)")
    if execute:
        new_dest.parent.mkdir(parents=True, exist_ok=True)
        src.rename(new_dest)
        manifest.append({
            "type": "rename", "hash": None,
            "original_path": str(rel), "new_path": str(new_dest.relative_to(root)),
            "kept_path": None, "moved_at": now_iso(),
        })
    return {"type": "rename", "src": str(rel), "dest": str(new_dest.relative_to(root)), "kept": None}


def normalize_manifest(manifest: list) -> list:
    """Upgrade manifest entries written by older versions of this script
    (which used "quarantined_path" and had no "type" field) to the current
    schema, so old manifests keep working with --restore / append."""
    for entry in manifest:
        if "new_path" not in entry and "quarantined_path" in entry:
            entry["new_path"] = entry.pop("quarantined_path")
        entry.setdefault("type", "quarantine")
    return manifest


def load_manifest(manifest_path: Path) -> list:
    if not manifest_path.exists():
        return []
    return normalize_manifest(json.loads(manifest_path.read_text()))


# ---------------------------------------------------------------------------
# File-level exact-duplicate pass
# ---------------------------------------------------------------------------

def iter_candidate_files(root: Path, extensions: set[str], quarantine_dir: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dp = Path(dirpath)
        dirnames[:] = [d for d in dirnames if (dp / d).resolve() != quarantine_dir.resolve()]
        for name in filenames:
            p = dp / name
            if p.suffix.lower() in extensions:
                yield p


def pick_keeper(paths: list[Path], root: Path, prefer: str) -> Path:
    def rel_depth(p: Path):
        return len(p.relative_to(root).parts)

    def mtime(p: Path):
        return p.stat().st_mtime

    if prefer == "oldest":
        key = lambda p: (mtime(p), rel_depth(p), str(p))
    elif prefer == "newest":
        key = lambda p: (-mtime(p), rel_depth(p), str(p))
    elif prefer == "shortest-path":
        key = lambda p: (rel_depth(p), mtime(p), str(p))
    elif prefer == "longest-path":
        key = lambda p: (-rel_depth(p), mtime(p), str(p))
    else:
        raise ValueError(f"unknown --prefer strategy: {prefer}")

    return min(paths, key=key)


def build_file_dupe_groups(root: Path, extensions: set[str], quarantine_dir: Path):
    by_size: dict[int, list[Path]] = {}
    all_files = list(iter_candidate_files(root, extensions, quarantine_dir))
    print(f"Scanning {len(all_files)} candidate file(s) under {root} ...")

    for p in all_files:
        try:
            size = p.stat().st_size
        except OSError as e:
            print(f"  ! skipping unreadable file {p}: {e}", file=sys.stderr)
            continue
        by_size.setdefault(size, []).append(p)

    by_hash: dict[str, list[Path]] = {}
    to_hash = [p for group in by_size.values() if len(group) > 1 for p in group]
    print(f"Hashing {len(to_hash)} file(s) that share a size with another file ...")

    for p in to_hash:
        try:
            digest = sha256_of(p)
        except OSError as e:
            print(f"  ! skipping unreadable file {p}: {e}", file=sys.stderr)
            continue
        by_hash.setdefault(digest, []).append(p)

    return {h: paths for h, paths in by_hash.items() if len(paths) > 1}


def plan_file_dedupe(root: Path, extensions: set[str], quarantine_dir: Path, prefer: str):
    groups = build_file_dupe_groups(root, extensions, quarantine_dir)
    plan = []
    for digest, paths in groups.items():
        keeper = pick_keeper(paths, root, prefer)
        movers = [p for p in paths if p != keeper]
        plan.append({"hash": digest, "keep": keeper, "move": movers, "size": keeper.stat().st_size})
    plan.sort(key=lambda e: str(e["keep"]))
    return plan


def report_file_dedupe(plan, root: Path, delete_duplicates: bool = False):
    if not plan:
        print("No exact file duplicates found.")
        return
    total_reclaimable = sum(e["size"] * len(e["move"]) for e in plan)
    verb = "permanently deleted" if delete_duplicates else "quarantined"
    print()
    print(f"[files] {len(plan)} duplicate group(s), "
          f"{sum(len(e['move']) for e in plan)} file(s) would be {verb}, "
          f"reclaiming {human(total_reclaimable)}.")
    print()
    for entry in plan:
        print(f"KEEP  {entry['keep'].relative_to(root)}  ({human(entry['size'])})")
        for m in entry["move"]:
            print(f"  dup  {m.relative_to(root)}")


def execute_file_dedupe(plan, root: Path, quarantine_dir: Path, manifest: list,
                         delete_duplicates: bool = False) -> int:
    """delete_duplicates=True permanently deletes each duplicate instead of
    quarantining it - no "new_path" to restore from. Still logged (type
    "deleted") for an audit trail, even though --restore can't act on it."""
    moved = 0
    for entry in plan:
        for m in entry["move"]:
            if not m.exists():
                # defensive: shouldn't happen (this pass runs before anything
                # else touches the tree), but never crash on a stale path -
                # skip and let the user re-run to pick up whatever's left
                print(f"  ! skipping, no longer present: {m.relative_to(root)}", file=sys.stderr)
                continue
            rel = m.relative_to(root)
            kept_rel = str(entry["keep"].relative_to(root))
            if delete_duplicates:
                print(f"  DELETE  {rel}  (identical to {kept_rel})")
                m.unlink()
                manifest.append({
                    "type": "deleted", "hash": entry["hash"],
                    "original_path": str(rel), "new_path": None,
                    "kept_path": kept_rel, "moved_at": now_iso(),
                })
            else:
                dest = unique_path(quarantine_dir / rel)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(m), str(dest))
                manifest.append({
                    "type": "quarantine", "hash": entry["hash"],
                    "original_path": str(rel), "new_path": str(dest.relative_to(root)),
                    "kept_path": kept_rel, "moved_at": now_iso(),
                })
            moved += 1
    return moved


# ---------------------------------------------------------------------------
# Directory-merge pass
# ---------------------------------------------------------------------------

def strip_dir_suffix(name: str):
    """Return the base name with a sync-tool-style duplicate suffix removed,
    or None if name has no such suffix (i.e. it's a "bare" name)."""
    for pat in DIR_SUFFIX_PATTERNS:
        m = pat.match(name)
        if m:
            return m.group(1)
    return None


def find_dir_merge_groups(root: Path, quarantine_dir: Path):
    """Yield (parent_dir, keeper_name, [loser_names]) for sibling directories
    that look like sync-tool duplicates of a bare directory that exists."""
    groups = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dp = Path(dirpath)
        if dp.resolve() == quarantine_dir.resolve():
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if (dp / d).resolve() != quarantine_dir.resolve()]

        by_base: dict[str, list[tuple[str, bool]]] = {}
        for d in dirnames:
            base = strip_dir_suffix(d)
            is_bare = base is None
            key = d if is_bare else base
            by_base.setdefault(key, []).append((d, is_bare))

        for entries in by_base.values():
            if len(entries) < 2:
                continue
            bare = [d for d, is_bare in entries if is_bare]
            if len(bare) != 1:
                continue  # need exactly one bare anchor - see module docstring
            keeper = bare[0]
            losers = [d for d, is_bare in entries if not is_bare]
            groups.append((dp, keeper, losers))

    # deepest first, so nested duplicate structures resolve before their
    # ancestors are merged
    groups.sort(key=lambda g: len(g[0].relative_to(root).parts), reverse=True)
    return groups


def merge_loser_into_keeper(root: Path, quarantine_dir: Path, keeper_dir: Path, loser_dir: Path,
                             execute: bool, manifest: list, label: str = "MERGE",
                             delete_duplicates: bool = False, rename_conflicts: bool = False) -> list[dict]:
    """Merge loser_dir's contents into keeper_dir (both real, existing dirs).
    A file not yet present in keeper is moved over; an identical-content
    collision is quarantined (or, with delete_duplicates=True, permanently
    deleted); a differing-content collision is left in place and reported
    as a conflict (or, with rename_conflicts=True, renamed to a unique
    name so both survive). Empty directories left behind by a successful
    merge are removed. Returns a flat list of action dicts:
    {"type": "move"|"quarantine"|"delete"|"rename"|"conflict", "src": relpath,
    "dest": relpath|None, "kept": relpath|None} - "dest" for a quarantine
    action is where it landed in the quarantine folder; "kept" is the
    surviving file it duplicated. Callers needing old-style (moved,
    quarantined, conflicts) counts can derive them by filtering on "type"."""
    print(f"{label}  {loser_dir.relative_to(root)}/  ->  {keeper_dir.relative_to(root)}/")
    actions = []

    for dirpath, dirnames, filenames in os.walk(loser_dir, topdown=False):
        dp = Path(dirpath)
        rel_dir = dp.relative_to(loser_dir)
        for fname in filenames:
            src = dp / fname
            rel_file = (rel_dir / fname) if str(rel_dir) != "." else Path(fname)
            dest = keeper_dir / rel_file

            if not dest.exists():
                print(f"  move  {src.relative_to(root)}  ->  {dest.relative_to(root)}")
                if execute:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(dest))
                    manifest.append({
                        "type": "merge",
                        "hash": None,
                        "original_path": str(src.relative_to(root)),
                        "new_path": str(dest.relative_to(root)),
                        "kept_path": None,
                        "moved_at": now_iso(),
                    })
                actions.append({"type": "move", "src": str(src.relative_to(root)),
                                 "dest": str(dest.relative_to(root)), "kept": None})
                continue

            try:
                same = sha256_of(src) == sha256_of(dest)
            except OSError as e:
                print(f"  ! could not compare {src}: {e}", file=sys.stderr)
                same = False

            if same:
                kept_rel = str(dest.relative_to(root))
                rel = src.relative_to(root)
                if delete_duplicates:
                    print(f"  DELETE  {rel}  (identical to {kept_rel})")
                    if execute:
                        src.unlink()
                        manifest.append({
                            "type": "deleted", "hash": None,
                            "original_path": str(rel), "new_path": None,
                            "kept_path": kept_rel, "moved_at": now_iso(),
                        })
                    actions.append({"type": "delete", "src": str(rel), "dest": None, "kept": kept_rel})
                else:
                    qdest = unique_path(quarantine_dir / rel)
                    print(f"  dup(quarantine)  {rel}  (identical to {kept_rel})")
                    if execute:
                        qdest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(src), str(qdest))
                        manifest.append({
                            "type": "quarantine", "hash": None,
                            "original_path": str(rel), "new_path": str(qdest.relative_to(root)),
                            "kept_path": kept_rel, "moved_at": now_iso(),
                        })
                    actions.append({"type": "quarantine", "src": str(rel),
                                     "dest": str(qdest.relative_to(root)), "kept": kept_rel})
            else:
                actions.append(resolve_conflict(src, dest, root, execute, manifest, rename_conflicts))

        if execute:
            try:
                dp.rmdir()
            except OSError:
                pass  # not empty - conflicts remain, leave it

    return actions


def plan_and_maybe_execute_dir_merge(root: Path, quarantine_dir: Path, execute: bool, manifest: list,
                                      delete_duplicates: bool = False, rename_conflicts: bool = False) -> list[dict]:
    """Returns a flat list of action dicts (see merge_loser_into_keeper),
    each also carrying "merge": "<loser rel>/ -> <keeper rel>/" so callers
    can group by merge pair."""
    groups = find_dir_merge_groups(root, quarantine_dir)
    if not groups:
        print("No duplicate-looking sibling directories found.")
        return []

    all_actions = []

    print()
    print(f"[dirs] {len(groups)} sibling directory group(s) look like duplicates.")
    print()

    for parent, keeper_name, losers in groups:
        keeper_dir = parent / keeper_name
        for loser_name in losers:
            loser_dir = parent / loser_name
            pair = f"{loser_dir.relative_to(root)}/ -> {keeper_dir.relative_to(root)}/"
            actions = merge_loser_into_keeper(root, quarantine_dir, keeper_dir, loser_dir, execute, manifest,
                                               delete_duplicates=delete_duplicates,
                                               rename_conflicts=rename_conflicts)
            for a in actions:
                a["merge"] = pair
            all_actions.extend(actions)

    return all_actions


# ---------------------------------------------------------------------------
# Lowercase-normalization pass
# ---------------------------------------------------------------------------

def rename_case(path: Path, root: Path, quarantine_dir: Path, execute: bool,
                 manifest: list, is_dir: bool, stats: dict,
                 delete_duplicates: bool = False, rename_conflicts: bool = False):
    """Lowercase path's own name (not recursive). On a collision, never
    overwrites: identical-content files are quarantined (or, with
    delete_duplicates=True, permanently deleted); differing-content
    files/dirs are left alone and reported as a conflict (or, with
    rename_conflicts=True, renamed to a unique name so both survive); a
    directory colliding with an existing lowercase directory is merged
    into it via merge_loser_into_keeper. Returns the final Path to keep
    using (which may be unchanged), or None if the entry itself no longer
    exists (quarantined, deleted, or fully merged away)."""
    name = path.name
    lower = name.lower()
    if lower == name:
        return path
    dest = path.parent / lower

    if not dest.exists():
        print(f"  rename  {path.relative_to(root)}  ->  {dest.relative_to(root)}")
        if execute:
            path.rename(dest)
            manifest.append({
                "type": "rename",
                "hash": None,
                "original_path": str(path.relative_to(root)),
                "new_path": str(dest.relative_to(root)),
                "kept_path": None,
                "moved_at": now_iso(),
            })
        stats["renamed_dirs" if is_dir else "renamed_files"] += 1
        stats["actions"].append({"type": "rename", "src": str(path.relative_to(root)),
                                  "dest": str(dest.relative_to(root)), "kept": None})
        # in dry-run mode dest doesn't actually exist yet - recurse into the
        # real (still original-case) path so nested renames aren't silently
        # missed; only in execute mode does dest now exist to recurse into
        return dest if execute else path

    if is_dir:
        if not dest.is_dir():
            action = resolve_conflict(path, dest, root, execute, manifest, rename_conflicts)
            stats["actions"].append(action)
            if action["type"] == "conflict":
                stats["conflicts"].append((path, dest))
                return path
            stats["renamed_dirs"] += 1
            return (root / action["dest"]) if execute else path
        print(f"  case-collision  {path.relative_to(root)}/  has lowercase sibling "
              f"{dest.relative_to(root)}/ - merging")
        actions = merge_loser_into_keeper(
            root, quarantine_dir, dest, path, execute, manifest, label="    MERGE",
            delete_duplicates=delete_duplicates, rename_conflicts=rename_conflicts,
        )
        stats["actions"].extend(actions)
        stats["merged_files"] += sum(1 for a in actions if a["type"] == "move")
        stats["quarantined"] += sum(1 for a in actions if a["type"] == "quarantine")
        stats["renamed_files"] += sum(1 for a in actions if a["type"] == "rename")
        stats["conflicts"].extend(
            (root / a["src"], root / a["dest"]) for a in actions if a["type"] == "conflict"
        )
        if execute and not path.exists():
            return dest  # loser fully drained and removed - continue as the keeper
        return path  # unresolved conflicts remain under the original-case dir; keep normalizing its contents in place

    if not dest.is_file():
        action = resolve_conflict(path, dest, root, execute, manifest, rename_conflicts)
        stats["actions"].append(action)
        if action["type"] == "conflict":
            stats["conflicts"].append((path, dest))
        else:
            stats["renamed_files"] += 1
        return path

    try:
        same = sha256_of(path) == sha256_of(dest)
    except OSError as e:
        print(f"  ! could not compare {path}: {e}", file=sys.stderr)
        same = False

    if same:
        kept_rel = str(dest.relative_to(root))
        rel = path.relative_to(root)
        if delete_duplicates:
            print(f"  DELETE  {rel}  (identical to {kept_rel})")
            if execute:
                path.unlink()
                manifest.append({
                    "type": "deleted", "hash": None,
                    "original_path": str(rel), "new_path": None,
                    "kept_path": kept_rel, "moved_at": now_iso(),
                })
            stats["actions"].append({"type": "delete", "src": str(rel), "dest": None, "kept": kept_rel})
        else:
            qdest = unique_path(quarantine_dir / rel)
            print(f"  case-dup(quarantine)  {rel}  (identical to {kept_rel})")
            if execute:
                qdest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(qdest))
                manifest.append({
                    "type": "quarantine", "hash": None,
                    "original_path": str(rel), "new_path": str(qdest.relative_to(root)),
                    "kept_path": kept_rel, "moved_at": now_iso(),
                })
            stats["actions"].append({"type": "quarantine", "src": str(rel),
                                      "dest": str(qdest.relative_to(root)), "kept": kept_rel})
        stats["quarantined"] += 1
        return None

    action = resolve_conflict(path, dest, root, execute, manifest, rename_conflicts)
    stats["actions"].append(action)
    if action["type"] == "conflict":
        stats["conflicts"].append((path, dest))
    else:
        stats["renamed_files"] += 1
    return path


def lowercase_tree(dir_path: Path, root: Path, quarantine_dir: Path, execute: bool,
                    manifest: list, stats: dict, delete_duplicates: bool = False,
                    rename_conflicts: bool = False):
    if dir_path.resolve() == quarantine_dir.resolve():
        return
    try:
        entries = list(os.scandir(dir_path))
    except OSError as e:
        print(f"  ! could not list {dir_path}: {e}", file=sys.stderr)
        return

    # process files first, then directories (own name), top-down, so a
    # renamed directory's final path is stable before we recurse into it -
    # this avoids a rename recorded for a child becoming stale if its
    # parent gets renamed afterwards
    for name in sorted(e.name for e in entries if e.is_file(follow_symlinks=False)):
        path = dir_path / name
        if path.exists():
            rename_case(path, root, quarantine_dir, execute, manifest, is_dir=False, stats=stats,
                        delete_duplicates=delete_duplicates, rename_conflicts=rename_conflicts)

    for name in sorted(e.name for e in entries if e.is_dir(follow_symlinks=False)):
        path = dir_path / name
        if not path.exists():
            continue
        final = rename_case(path, root, quarantine_dir, execute, manifest, is_dir=True, stats=stats,
                             delete_duplicates=delete_duplicates, rename_conflicts=rename_conflicts)
        if final is not None:
            lowercase_tree(final, root, quarantine_dir, execute, manifest, stats,
                            delete_duplicates=delete_duplicates, rename_conflicts=rename_conflicts)


def plan_and_maybe_execute_lowercase(root: Path, quarantine_dir: Path, execute: bool, manifest: list,
                                      delete_duplicates: bool = False, rename_conflicts: bool = False):
    print()
    print("[case] normalizing file and directory names to lowercase ...")
    stats = {"renamed_files": 0, "renamed_dirs": 0, "merged_files": 0, "quarantined": 0,
             "conflicts": [], "actions": []}
    lowercase_tree(root, root, quarantine_dir, execute, manifest, stats,
                    delete_duplicates=delete_duplicates, rename_conflicts=rename_conflicts)
    if not (stats["renamed_files"] or stats["renamed_dirs"] or stats["merged_files"]
            or stats["quarantined"] or stats["conflicts"]):
        print("Everything is already lowercase.")
    return stats


# ---------------------------------------------------------------------------
# Empty-directory cleanup
# ---------------------------------------------------------------------------

def prune_empty_dirs(root: Path, quarantine_dir: Path, execute: bool) -> list[Path]:
    """Remove (or report) any directory under root that ends up empty -
    whether it was already empty, or was hollowed out by dedupe/merge/
    lowercase removing every file it contained. Never touches root itself
    or anything under quarantine_dir (that has its own separate handling
    during --restore). Bottom-up, so a directory that becomes empty only
    once its children are removed is caught in the same pass. Not tracked
    in the manifest and not undone by --restore - an empty directory holds
    no data, so there's nothing to lose or restore."""
    qres = quarantine_dir.resolve()
    removed = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        dp = Path(dirpath)
        if dp == root:
            continue
        dpres = dp.resolve()
        if dpres == qres or qres in dpres.parents:
            continue
        try:
            if any(dp.iterdir()):
                continue
        except OSError:
            continue
        print(f"  rmdir  {dp.relative_to(root)}/")
        if execute:
            try:
                dp.rmdir()
            except OSError as e:
                print(f"  ! could not remove {dp}: {e}", file=sys.stderr)
                continue
        removed.append(dp)
    return removed


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_run(args):
    root = Path(args.root).resolve()
    quarantine_dir = root / QUARANTINE_DIRNAME
    extensions = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in args.ext}

    manifest_path = quarantine_dir / MANIFEST_NAME
    manifest = load_manifest(manifest_path) if args.execute else []

    file_plan = plan_file_dedupe(root, extensions, quarantine_dir, args.prefer)
    report_file_dedupe(file_plan, root, delete_duplicates=args.delete)

    if not args.execute:
        dir_conflicts = []
        if args.merge_dirs:
            # dry run: safe to call directly, it only prints - the tree
            # hasn't changed since file_plan was computed above
            dir_actions = plan_and_maybe_execute_dir_merge(root, quarantine_dir, False, manifest,
                                                             delete_duplicates=args.delete,
                                                             rename_conflicts=args.rename_conflicts)
            dir_conflicts = [a for a in dir_actions if a["type"] == "conflict"]
        lc_conflicts = []
        if args.lowercase:
            lc_stats = plan_and_maybe_execute_lowercase(root, quarantine_dir, False, manifest,
                                                          delete_duplicates=args.delete,
                                                          rename_conflicts=args.rename_conflicts)
            lc_conflicts = lc_stats["conflicts"]
        print()
        print("[cleanup] directories that are empty right now (note: this doesn't "
              "account for directories the passes above would hollow out once "
              "actually applied - re-run with --execute to catch those too):")
        empty_now = prune_empty_dirs(root, quarantine_dir, False)
        if not empty_now:
            print("None found.")
        print()
        print("Dry run only - no files were touched. Re-run with --execute to apply "
              f"(file duplicates go to {quarantine_dir.relative_to(root)}/, directory "
              "merges move files directly, and only identical-content collisions get "
              "quarantined - nothing is ever deleted).")
        if dir_conflicts:
            print(f"{len(dir_conflicts)} directory-merge conflict(s) would need manual "
                  "review even after --execute (see CONFLICT lines above).")
        if lc_conflicts:
            print(f"{len(lc_conflicts)} lowercase-collision conflict(s) would need manual "
                  "review even after --execute (see CONFLICT lines above).")
        return

    # IMPORTANT: file-level dedupe must execute its plan immediately, before
    # any other pass touches the tree - plan_file_dedupe took a snapshot of
    # paths above, and a later pass (dir merge) moving/removing those same
    # files out from under it would make execute_file_dedupe crash trying to
    # move a path that no longer exists. Dir-merge is safe to run after: it
    # re-scans the live tree itself rather than working off a stale plan.
    # The whole block is wrapped so a manifest is always written for
    # whatever succeeded so far, even if something later raises.
    file_moved = 0
    dir_moved = dir_quarantined = dir_renamed = 0
    dir_conflicts = []
    lc_stats = None
    emptied = []
    try:
        file_moved = execute_file_dedupe(file_plan, root, quarantine_dir, manifest,
                                          delete_duplicates=args.delete)
        if args.merge_dirs:
            dir_actions = plan_and_maybe_execute_dir_merge(root, quarantine_dir, True, manifest,
                                                             delete_duplicates=args.delete,
                                                             rename_conflicts=args.rename_conflicts)
            dir_moved = sum(1 for a in dir_actions if a["type"] == "move")
            dir_quarantined = sum(1 for a in dir_actions if a["type"] == "quarantine")
            dir_renamed = sum(1 for a in dir_actions if a["type"] == "rename")
            dir_conflicts = [a for a in dir_actions if a["type"] == "conflict"]
        if args.lowercase:
            lc_stats = plan_and_maybe_execute_lowercase(root, quarantine_dir, True, manifest,
                                                          delete_duplicates=args.delete,
                                                          rename_conflicts=args.rename_conflicts)
        print()
        print("[cleanup] removing leftover empty directories ...")
        emptied = prune_empty_dirs(root, quarantine_dir, True)
    finally:
        if manifest:
            quarantine_dir.mkdir(exist_ok=True)
            manifest_path.write_text(json.dumps(manifest, indent=2))
            print(f"\nManifest written to {manifest_path} ({len(manifest)} entrie(s)).")

    verb = "Permanently deleted" if args.delete else "Quarantined"
    print()
    print(f"{verb} {file_moved} exact-duplicate file(s).")
    if args.merge_dirs:
        print(f"Merged {dir_moved} file(s) into keeper directories, "
              f"{verb.lower()} {dir_quarantined} more identical file(s) found during merge"
              + (f", renamed {dir_renamed} conflicting file(s) to keep both." if dir_renamed else "."))
        if dir_conflicts:
            print(f"{len(dir_conflicts)} conflict(s) left in place for manual review:")
            for a in dir_conflicts:
                print(f"  {a['src']}  vs  {a['dest']}")
    if lc_stats is not None:
        print(f"Lowercased {lc_stats['renamed_files']} file name(s) and "
              f"{lc_stats['renamed_dirs']} directory name(s), merged {lc_stats['merged_files']} "
              f"file(s) via case-collision, {verb.lower()} {lc_stats['quarantined']} case-duplicate(s).")
        if lc_stats["conflicts"]:
            print(f"{len(lc_stats['conflicts'])} case-collision conflict(s) left in place for manual review:")
            for src, dest in lc_stats["conflicts"]:
                print(f"  {src.relative_to(root)}  vs  {dest.relative_to(root)}")
    print(f"Removed {len(emptied)} leftover empty director(y/ies).")
    print("Review the results, then delete the quarantine folder yourself once "
          "you're happy - or run this script with --restore to undo.")


def prune_empty_quarantine(quarantine_dir: Path):
    if not quarantine_dir.exists():
        return
    pruned = 0
    for dirpath, dirnames, filenames in os.walk(quarantine_dir, topdown=False):
        dp = Path(dirpath)
        if dp == quarantine_dir:
            continue
        try:
            dp.rmdir()
            pruned += 1
        except OSError:
            pass  # not empty - leave it
    if pruned:
        print(f"Removed {pruned} now-empty leftover director(y/ies) from quarantine.")
    try:
        quarantine_dir.rmdir()
        print(f"Quarantine folder {quarantine_dir} is empty and was removed.")
    except OSError:
        pass  # still has content (unrestored entries or the manifest file)


def cmd_restore(args):
    root = Path(args.root).resolve()
    quarantine_dir = root / QUARANTINE_DIRNAME
    manifest_path = quarantine_dir / MANIFEST_NAME

    if not manifest_path.exists():
        print(f"No manifest found at {manifest_path}; nothing to restore.")
        prune_empty_quarantine(quarantine_dir)
        return

    manifest = normalize_manifest(json.loads(manifest_path.read_text()))
    restored = 0
    already_deleted = 0
    # Process in reverse order: a nested rename's original_path assumes its
    # parent directory already has its NEW (lowercased) name, since renames
    # are applied top-down (parent before child) during --execute. Undoing
    # in forward order would rename the parent back first and break the
    # path for every descendant rename recorded after it. Reverse order
    # undoes children before parents, which is correct regardless of entry
    # type since quarantine/merge entries have no such ordering dependency.
    restored_flags = [False] * len(manifest)
    for i in reversed(range(len(manifest))):
        entry = manifest[i]
        if entry.get("type") == "deleted" or entry.get("new_path") is None:
            # permanently deleted (--delete / delete_duplicates), not
            # quarantined - nothing to move back. Left in the manifest as
            # a permanent record; not counted as "remaining to restore".
            already_deleted += 1
            continue
        src = root / entry["new_path"]
        dest = root / entry["original_path"]
        if not src.exists():
            print(f"  ! missing file, skipping: {src}")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            print(f"  ! restore target already exists, skipping: {dest}")
            continue
        shutil.move(str(src), str(dest))
        restored_flags[i] = True
        restored += 1

    remaining = [entry for i, entry in enumerate(manifest) if not restored_flags[i]]
    if remaining:
        manifest_path.write_text(json.dumps(remaining, indent=2))
    else:
        manifest_path.unlink(missing_ok=True)

    if already_deleted:
        print(f"{already_deleted} entr(y/ies) were permanently deleted (not quarantined) "
              "and cannot be restored - left as a record in the manifest.")

    print(f"Restored {restored} file(s) back to their original locations.")
    if remaining:
        print(f"{len(remaining)} entr(y/ies) could not be restored automatically; "
              "see printed warnings above.")

    prune_empty_quarantine(quarantine_dir)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", help="Root directory to scan (recurses into all subdirectories)")
    ap.add_argument("--execute", action="store_true",
                     help="Actually apply the plan. Without this flag, the script only "
                          "reports what it would do.")
    ap.add_argument("--restore", action="store_true",
                     help="Undo a previous --execute run using the manifest.")
    ap.add_argument("--merge-dirs", action="store_true",
                     help="Also detect and merge duplicate-looking sibling directories "
                          '(e.g. "Foo" + "Foo_1") - see module docstring for the exact rules.')
    ap.add_argument("--lowercase", action="store_true",
                     help="Also normalize every file and directory name to lowercase. "
                          "Runs last (after file dedupe and dir merge). A name collision "
                          "this creates is handled the same safe way as everywhere else: "
                          "identical content is quarantined, differing content is left in "
                          "place and reported as a conflict, never overwritten.")
    ap.add_argument("--prefer", choices=["oldest", "newest", "shortest-path", "longest-path"],
                     default="oldest",
                     help="Which copy of a duplicate FILE to keep (default: oldest mtime).")
    ap.add_argument("--ext", nargs="+", default=sorted(DEFAULT_EXTENSIONS),
                     help=f"File extensions to consider (default: {sorted(DEFAULT_EXTENSIONS)})")
    ap.add_argument("--rename-conflicts", action="store_true",
                     help="When --merge-dirs or --lowercase finds a name collision with "
                          "DIFFERENT content, rename the incoming file to a unique name "
                          "instead of leaving it as an unresolved conflict. Default: leave "
                          "conflicts alone for manual review.")
    ap.add_argument("--delete", action="store_true",
                     help="DANGEROUS, IRREVERSIBLE: permanently delete verified duplicates "
                          "instead of moving them to _duplicates_quarantine/. Skips the "
                          "safety net entirely - --restore cannot bring these back. Still "
                          "logged in the manifest for an audit trail, but only as a record, "
                          "not something that can be undone. Default: quarantine (safe).")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        sys.exit(1)

    if args.restore:
        cmd_restore(args)
    else:
        cmd_run(args)


if __name__ == "__main__":
    main()
