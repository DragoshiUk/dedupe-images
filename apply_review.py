#!/usr/bin/env python3
"""
Apply decisions recorded by review_gui.py.

Reads <root>/_near_duplicate_review/decisions.json and, for every
decided (non-skipped) group, quarantines the "discard" files - using the
exact same mechanism as dedupe_images.py: move into
_duplicates_quarantine/ preserving relative path, record in the same
dedupe_manifest.json used by every other tool in this project, never
delete. That means `dedupe_images.py --restore` undoes this too.

A decision entry looks like {"keep": [relpath, ...], "discard": [relpath,
...], "skipped": bool}. "keep" can hold more than one path - a group
isn't always a strict "pick one winner", sometimes more than two images
in a near-duplicate group are worth keeping (e.g. legitimately different
crops/edits that just happened to hash close together).

build_apply_plan()/apply_plan() are also imported directly by
review_gui.py so the web UI can apply decisions in-process without
shelling out to this script.

Dry-run by default. Nothing is touched until you pass --execute.

Usage:
    python3 apply_review.py /mnt/dragonhoard/tuqiri/commissions
    python3 apply_review.py /mnt/dragonhoard/tuqiri/commissions --execute
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

from dedupe_images import (
    MANIFEST_NAME, QUARANTINE_DIRNAME, human, load_manifest, now_iso,
    prune_empty_dirs, sha256_of, unique_path,
)

REVIEW_DIRNAME = "_near_duplicate_review"
DECISIONS_NAME = "decisions.json"


def build_apply_plan(root: Path, decisions: dict):
    """Returns (plan, skipped_groups). plan is a list of
    (keep_list, discard_rel, discard_path) - one entry per file that would
    be quarantined. Silently skips groups with nothing to discard, and
    individual discard paths that no longer exist (tree may have changed
    since the decision was made)."""
    plan = []
    skipped_groups = 0
    for gid, d in decisions.items():
        if d.get("skipped"):
            skipped_groups += 1
            continue
        keep_list = d.get("keep", [])
        discard_list = [p for p in d.get("discard", []) if p not in keep_list]
        if not discard_list:
            continue
        for discard_rel in discard_list:
            dpath = root / discard_rel
            if not dpath.exists():
                print(f"  ! already gone, skipping: {discard_rel}", file=sys.stderr)
                continue
            plan.append((keep_list, discard_rel, dpath))
    return plan, skipped_groups


def apply_plan(root: Path, plan: list, quarantine_dir: Path, manifest: list,
               delete_duplicates: bool = False) -> int:
    """Quarantines every (keep_list, discard_rel, discard_path) in plan -
    or, with delete_duplicates=True, permanently deletes them instead (no
    "new_path" to restore from; still logged as type "deleted" for an
    audit trail). Mutates manifest in place (appends one entry per file)
    so a caller doing this inside a try/finally still gets a manifest for
    whatever succeeded if something raises partway through."""
    moved = 0
    for keep_list, discard_rel, dpath in plan:
        if not dpath.exists():
            continue  # defensive: tree changed between preview and apply
        try:
            digest = sha256_of(dpath)
        except OSError:
            digest = None
        if delete_duplicates:
            dpath.unlink()
            manifest.append({
                "type": "deleted", "hash": digest,
                "original_path": discard_rel, "new_path": None,
                "kept_path": keep_list, "moved_at": now_iso(),
            })
        else:
            dest = unique_path(quarantine_dir / discard_rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dpath), str(dest))
            manifest.append({
                "type": "quarantine", "hash": digest,
                "original_path": discard_rel, "new_path": str(dest.relative_to(root)),
                "kept_path": keep_list, "moved_at": now_iso(),
            })
        moved += 1
    return moved


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", help="Root directory (must match what review_gui.py was run against)")
    ap.add_argument("--decisions", default=None,
                     help="Path to decisions JSON (default: <root>/_near_duplicate_review/decisions.json)")
    ap.add_argument("--execute", action="store_true",
                     help="Actually quarantine the discarded files. Without this, only reports the plan.")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        sys.exit(1)
    decisions_path = Path(args.decisions) if args.decisions else root / REVIEW_DIRNAME / DECISIONS_NAME
    if not decisions_path.exists():
        print(f"No decisions file at {decisions_path} - run review_gui.py first.", file=sys.stderr)
        sys.exit(1)

    quarantine_dir = root / QUARANTINE_DIRNAME
    manifest_path = quarantine_dir / MANIFEST_NAME
    manifest = load_manifest(manifest_path) if args.execute else []

    decisions = json.loads(decisions_path.read_text())
    plan, skipped_groups = build_apply_plan(root, decisions)

    print(f"{len(decisions)} decision(s) on record ({skipped_groups} skipped).")
    if not plan:
        print("Nothing to quarantine.")
        return

    total_size = sum(p.stat().st_size for _, _, p in plan)
    print(f"{len(plan)} file(s) to quarantine, reclaiming {human(total_size)}.")
    print()
    for keep_list, discard_rel, _ in plan:
        print(f"  dup  {discard_rel}   (kept: {', '.join(keep_list) or '?'})")

    if not args.execute:
        print()
        print("Dry run only - no files were touched. Re-run with --execute to apply "
              f"(moves into {quarantine_dir.relative_to(root)}/, reversible with "
              "dedupe_images.py --restore).")
        return

    moved = 0
    emptied = []
    try:
        moved = apply_plan(root, plan, quarantine_dir, manifest)
        emptied = prune_empty_dirs(root, quarantine_dir, True)
    finally:
        if manifest:
            quarantine_dir.mkdir(exist_ok=True)
            manifest_path.write_text(json.dumps(manifest, indent=2))
            print(f"Manifest written to {manifest_path} ({len(manifest)} entrie(s)).")

    print(f"Quarantined {moved} file(s). Removed {len(emptied)} leftover empty director(y/ies).")
    print("Review the results, then delete the quarantine folder yourself once you're "
          "happy - or run dedupe_images.py --restore to undo.")


if __name__ == "__main__":
    main()
