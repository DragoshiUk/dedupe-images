#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pillow"]
# ///
"""
Dragoshi's Super Duper Image De-Duper - local browser GUI wrapping every
operation in this project: exact-hash dedupe ("Identical Files"),
directory merging + lowercase normalization combined ("Normalisation"),
interactive perceptual near-duplicate review ("Visually Similar"), and
AI resolution upscaling via Real-ESRGAN ("Upscale").

Menu: Operations (four tabs - Identical Files/Normalisation/Visually
Similar are read-only/decision-only, inspect or decide, never run
anything themselves; Upscale is additive and self-contained instead, see
below) | Jobs (Pending Jobs: pick any mix of Identical Files/
Normalisation/Visually Similar, review one combined summary, then Start)
| Quarantine (what's parked in _duplicates_quarantine/, listed file by
file with a per-file or restore-all option, plus a permanent-delete
option once you're happy).

Upscale doesn't go through Pending Jobs/Quarantine at all - it never
moves or deletes anything, it just writes a new file for any image whose
longest side is under the slider's target resolution (up to 8192px/8K):
beside the original by default, or into a chosen output directory (source
sub-folders recreated), with a configurable prefix/suffix on the name. A
file whose output path already exists is skipped and reported unless the
"Overwrite existing upscaled files" box is ticked. So there's no
manifest/restore story for it and it has its own direct Start button.
Needs realesrgan-ncnn-vulkan; if it's missing the command line says so at
startup and the Upscale tab offers a one-click download of the portable
build.

Nothing is deleted by a dedupe/merge/rename action itself - everything
that would be removed is moved into _duplicates_quarantine/ with an entry
in the shared dedupe_manifest.json, so the Quarantine tab's Restore
buttons (or `dedupe_images.py --restore` from the command line, same
underlying logic) undo any of it. Two explicit, separately-warned opt-ins
break that safety net on purpose: the Jobs tab's "skip quarantine"
checkbox permanently deletes duplicates immediately instead of
quarantining them (still logged in the manifest as an audit trail, but
restore can't act on it), and the Quarantine tab's delete button
permanently empties the quarantine folder. Both require an explicit
tick/confirmation and say plainly that they cannot be undone.

Normalisation also offers "rename conflicting files": by default, a name
collision with genuinely different content (not a verified duplicate) is
left alone and flagged as a conflict for manual review; with this option,
the incoming file is instead renamed to a unique name so both survive.

Pending Jobs' Start always recomputes the Identical Files / Normalisation
plans fresh immediately before applying them, rather than replaying
whatever was shown when the summary was built - the folder can change
during a review session. Visually Similar decisions are always applied
exactly as decided, since there's no "fresh" version of a human judgement
call to recompute. Every individual move stays collision-safe regardless
of which plan produced it.

Binds to 127.0.0.1 only - never reachable from the network.

Usage:
    uv run review_gui.py                                   # picker starts at $HOME
    uv run review_gui.py /mnt/dragonhoard/tuqiri/commissions
    uv run review_gui.py ROOT --threshold 6 --port 8765
"""

import argparse
import io
import json
import mimetypes
import os
import shutil
import sys
import threading
import traceback
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

try:
    from PIL import Image, ImageFilter, ImageStat
except ModuleNotFoundError:
    print(
        "error: this script needs Pillow, which the inline script metadata at the "
        "top of this file declares as a dependency - but that only gets installed "
        "automatically when run via uv, not plain python3.\n\n"
        "Run it like this instead:\n"
        "    uv run review_gui.py ...\n"
        "or, since it's executable:\n"
        "    ./review_gui.py ...\n",
        file=sys.stderr,
    )
    sys.exit(1)

from dedupe_images import (
    CASE_STYLES, DEFAULT_EXTENSIONS, MANIFEST_NAME, QUARANTINE_DIRNAME, SEPARATOR_STYLES,
    execute_file_dedupe, human, load_manifest, plan_and_maybe_execute_dir_merge,
    plan_and_maybe_execute_normalize, plan_file_dedupe, prune_empty_dirs, restore_manifest_entries,
)
from find_near_duplicates import compute_hashes, group_by_hash, group_confidence, iter_images
from apply_review import apply_plan, build_apply_plan
import upscale

REVIEW_DIRNAME = "_near_duplicate_review"
DECISIONS_NAME = "decisions.json"

OP_NAMES = {"identical": "Identical Files", "normalise": "Normalisation", "visual": "Visually Similar"}
OP_ORDER = ["identical", "normalise", "visual"]  # safe execution order


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_with_manifest(root: Path, fn):
    """Loads the existing manifest, calls fn(manifest_list), always
    persists whatever ends up in the manifest afterward (even if fn
    raises) - same crash-safety guarantee as the CLI's --execute."""
    quarantine_dir = root / QUARANTINE_DIRNAME
    manifest_path = quarantine_dir / MANIFEST_NAME
    manifest = load_manifest(manifest_path)
    try:
        return fn(manifest)
    finally:
        if manifest:
            quarantine_dir.mkdir(exist_ok=True)
            manifest_path.write_text(json.dumps(manifest, indent=2))


def enrich_actions(root: Path, actions: list[dict]) -> list[dict]:
    """Adds "size" (bytes) and "is_dir" fields to each action dict,
    best-effort. Only ever runs on preview (non-executed) plans, so the
    source path is still there to stat."""
    for a in actions:
        src = root / a["src"]
        try:
            a["size"] = src.stat().st_size
        except OSError:
            a["size"] = 0
        a["is_dir"] = src.is_dir()
    return actions


def is_image_path(rel: str) -> bool:
    return Path(rel).suffix.lower() in DEFAULT_EXTENSIONS


# ---------------------------------------------------------------------------
# Background job / progress tracking
# ---------------------------------------------------------------------------

class Progress:
    """One shared tracker for whatever long-running job is currently
    active (directory scan, review build, or run) - only one runs at a
    time (see job_lock), so one tracker is enough. phase_tick collapses
    "enter a new phase" and "update progress within the current phase"
    into a single call: pass the same phase name again to just update
    current/total, or a new name to move to a new phase."""

    def __init__(self):
        self.lock = threading.Lock()
        self.active = False
        self.kind = None
        self.phase = ""
        self.phase_index = 0
        self.phase_count = 1
        self.current = 0
        self.total = 1
        self.error = None
        self.done_result = None
        self.log = []  # bounded human-readable lines, for jobs that stream output

    def begin(self, kind: str, phase_count: int):
        with self.lock:
            self.active = True
            self.kind = kind
            self.phase = ""
            self.phase_index = 0
            self.phase_count = max(phase_count, 1)
            self.current = 0
            self.total = 1
            self.error = None
            self.done_result = None
            self.log = []

    def log_line(self, text: str):
        """Append a line to the job's rolling output log (last 200 kept)."""
        with self.lock:
            self.log.append(text)
            del self.log[:-200]

    def phase_tick(self, index: int, name: str, current: int, total: int = 1):
        with self.lock:
            if self.phase != name:
                self.phase_index = index
                self.phase = name
                self.total = max(total, 1)
            self.current = current
            if total:
                self.total = max(total, 1)

    def finish(self, result=None, error=None):
        with self.lock:
            self.active = False
            self.done_result = result
            self.error = error

    def snapshot(self) -> dict:
        with self.lock:
            within = self.current / self.total if self.total else 1
            pct = round(100 * (self.phase_index + within) / self.phase_count) if self.phase_count else 100
            return {
                "active": self.active, "kind": self.kind, "phase": self.phase,
                "phase_index": self.phase_index, "phase_count": self.phase_count,
                "current": self.current, "total": self.total, "pct": max(0, min(pct, 100)),
                "error": self.error, "done": (not self.active) and (self.done_result is not None or self.error is not None),
                "result": self.done_result, "log": list(self.log),
            }


progress = Progress()
job_lock = threading.Lock()  # only one build/run job at a time

# The directory scan (hash -> group -> score) gets its own tracker/lock,
# separate from job_lock. It's the one background job that's safe to let
# run alongside build/run (both of those only ever read state.decisions;
# the scan is the only thing that grows state.groups), and giving it a
# dedicated tracker means its progress can drive its own UI - the initial
# hash/group modal, then the bottom status bar - without fighting build/run
# for the same numbers.
scan_progress = Progress()
scan_lock = threading.Lock()


def start_job(kind: str, phase_count: int, work_fn, prog: Progress = None, lock: threading.Lock = None) -> bool:
    """work_fn(prog) -> result dict, run in a background thread.
    Returns False (does nothing) if another job on the same lock is
    already running. Defaults to the shared build/run tracker+lock;
    pass prog=scan_progress, lock=scan_lock for the directory scan."""
    prog = prog if prog is not None else progress
    lock = lock if lock is not None else job_lock
    if not lock.acquire(blocking=False):
        return False

    def runner():
        prog.begin(kind, phase_count)
        try:
            result = work_fn(prog)
            prog.finish(result=result if result is not None else {})
        except Exception as e:
            traceback.print_exc()
            prog.finish(error=str(e))
        finally:
            lock.release()

    threading.Thread(target=runner, daemon=True).start()
    return True


# ---------------------------------------------------------------------------
# Near-duplicate ("Visually Similar") scoring - see find_near_duplicates.py
# ---------------------------------------------------------------------------

def sharpness_score(path: Path, max_dim: int = 512) -> float:
    """Edge-variance blur proxy: downsample, find edges, take the variance
    of the result. Blurrier images have less high-frequency edge content
    and score lower. A standard, cheap stand-in for variance-of-Laplacian."""
    with Image.open(path) as img:
        img = img.convert("L")
        if max(img.size) > max_dim:
            scale = max_dim / max(img.size)
            img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.LANCZOS)
        edges = img.filter(ImageFilter.FIND_EDGES)
        return ImageStat.Stat(edges).var[0]


def group_id(root: Path, members: list[Path]) -> str:
    return "|".join(sorted(str(p.relative_to(root)) for p in members))


def score_one_group(root: Path, members: list[Path], hashes: dict[Path, int]) -> dict:
    """Scores a single candidate group (dimensions/size/sharpness per
    image). Split out from what used to be build_group_data() so State.scan
    can score groups one at a time and publish each as it's ready, instead
    of computing the whole batch before anything is visible."""
    images = []
    for p in members:
        rel = str(p.relative_to(root))
        try:
            with Image.open(p) as img:
                w, h = img.size
            size = p.stat().st_size
            sharp = sharpness_score(p)
        except Exception as e:
            print(f"  ! could not score {rel}: {e}", file=sys.stderr)
            w = h = size = 0
            sharp = 0.0
        images.append({
            "path": rel, "width": w, "height": h, "size": size,
            "size_human": human(size), "sharpness": round(sharp, 1),
        })
    if images:
        best = max(images, key=lambda im: (im["width"] * im["height"], im["sharpness"], im["size"]))
        best["suggested"] = True
    return {
        "id": group_id(root, members),
        "avg_distance": round(group_confidence(hashes, members), 2),
        "images": images,
    }


def normalize_decision(d: dict, all_paths: list[str]) -> dict:
    """Backward compat: an old-format entry (single string "keep", or
    missing "keep" entirely) is upgraded to the current schema
    ({"keep": [...], "discard": [...], "skipped": bool})."""
    keep = d.get("keep")
    if isinstance(keep, str):
        keep = [keep]
    elif keep is None:
        keep = [p for p in all_paths if p not in d.get("discard", [])]
    discard = d.get("discard", [p for p in all_paths if p not in keep])
    return {"keep": keep, "discard": discard, "skipped": bool(d.get("skipped")),
            "decided_at": d.get("decided_at", now_iso())}


def save_decisions(path: Path, decisions: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(decisions, indent=2))


def list_dir(path: Path):
    """Subdirectories of path for the directory picker (dotdirs hidden,
    unreadable entries skipped). Returns (dirnames, error)."""
    try:
        names = []
        with os.scandir(path) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                try:
                    if e.is_dir(follow_symlinks=False):
                        names.append(e.name)
                except OSError:
                    continue
    except OSError as e:
        return None, str(e)
    names.sort(key=str.lower)
    return names, None


class State:
    """Mutable server state for the Visually Similar tab, which needs
    stable group identity across requests (interactive decisions). The
    other two operations are stateless - every preview call recomputes
    fresh against state.root, which is the only thing they read from
    here."""

    def __init__(self, threshold: int, extensions: set[str]):
        self.threshold = threshold
        self.extensions = extensions
        self.lock = threading.Lock()
        self.root: Path | None = None
        self.groups: list[dict] = []
        self.group_paths: set[str] = set()
        self.decisions: dict = {}
        self.decisions_path: Path | None = None

    def scan(self, root: Path, on_progress=None):
        """Hashing must finish for every candidate image before grouping
        can happen at all (Hamming-distance clustering is all-pairs - a
        later image can always turn out to belong to an earlier group), so
        nothing is visible during that part. Scoring each already-formed
        group, though, doesn't have that constraint - it's independent
        per-group work - so each group is appended to self.groups (and
        published under the lock) the moment it's scored, instead of
        waiting for the whole batch. Callers (the web UI) can show
        whatever's in state.groups at any point during this call."""
        quarantine_dir = root / QUARANTINE_DIRNAME
        review_dir = root / REVIEW_DIRNAME
        decisions_path = review_dir / DECISIONS_NAME
        # Snapshot of decisions saved before *this* scan started, so any
        # group that matches one gets its prior decision restored as soon
        # as it's (re)discovered. Read once up front, not re-read per
        # group: cheap either way, but a stable snapshot means restoring
        # old decisions doesn't itself depend on scan progress.
        raw_decisions = json.loads(decisions_path.read_text()) if decisions_path.exists() else {}

        with self.lock:
            self.root = root
            self.groups = []
            self.group_paths = set()
            self.decisions = {}
            self.decisions_path = decisions_path

        files = list(iter_images(root, self.extensions, {quarantine_dir, review_dir}))
        print(f"Scanning {len(files)} image(s) under {root} ...")

        def hash_cb(i, total):
            if on_progress:
                on_progress("hashing", i, total)

        def group_cb(i, total):
            if on_progress:
                on_progress("grouping", i, total)

        hashes = compute_hashes(files, root, on_progress=hash_cb)
        ordered = group_by_hash(hashes, self.threshold, on_progress=group_cb)
        total_groups = len(ordered)
        print(f"Scoring {total_groups} group(s) ...")

        for i, members in enumerate(ordered, 1):
            group = score_one_group(root, members, hashes)
            with self.lock:
                self.groups.append(group)
                self.group_paths.update(im["path"] for im in group["images"])
                if group["id"] in raw_decisions:
                    paths = [im["path"] for im in group["images"]]
                    self.decisions[group["id"]] = normalize_decision(raw_decisions[group["id"]], paths)
            if on_progress:
                on_progress("scoring", i, total_groups)

        print(f"{len(self.groups)} group(s) ready under {root}.")


def start_scan(state: State, root: Path) -> bool:
    """Kicks off a directory scan as a background job on its own
    tracker/lock (see scan_progress/scan_lock above). Shared by the
    /api/set-root handler and the CLI --root startup path, so both get the
    same "server usable immediately, groups fill in as they're scored"
    behavior."""
    def work(prog):
        phase_idx = {"hashing": 0, "grouping": 1, "scoring": 2}
        def cb(phase, i, total):
            prog.phase_tick(phase_idx[phase], phase, i, total)
        state.scan(root, on_progress=cb)
        with state.lock:
            return {"groups_count": len(state.groups)}

    return start_job("scan", phase_count=3, work_fn=work, prog=scan_progress, lock=scan_lock)


# ---------------------------------------------------------------------------
# Combined pending-job building + running
# ---------------------------------------------------------------------------

def build_identical_items(root: Path, prefer: str, delete_duplicates: bool,
                           extensions: set[str]) -> list[dict]:
    quarantine_dir = root / QUARANTINE_DIRNAME
    plan = plan_file_dedupe(root, extensions, quarantine_dir, prefer)
    action = "delete" if delete_duplicates else "quarantine"
    items = []
    for entry in plan:
        keep_rel = str(entry["keep"].relative_to(root))
        for m in entry["move"]:
            sz = m.stat().st_size if m.exists() else 0
            items.append({"op": "identical", "action": action, "path": str(m.relative_to(root)),
                          "dest": None, "kept": keep_rel, "size": sz, "is_dir": False})
    return items


def _normalise_items(actions: list[dict]) -> list[dict]:
    """Maps enriched dir-merge / name-normalize actions to the wire shape
    the Jobs summary expects. Shared by both Normalisation sub-passes."""
    return [{"op": "normalise", "action": a["type"], "path": a["src"],
             "dest": a["dest"], "kept": a["kept"], "size": a["size"],
             "is_dir": a["is_dir"], "was_conflict": a.get("was_conflict", False)} for a in actions]


def build_dirmerge_items(root: Path, rename_conflicts: bool, delete_duplicates: bool) -> list[dict]:
    quarantine_dir = root / QUARANTINE_DIRNAME
    actions = plan_and_maybe_execute_dir_merge(root, quarantine_dir, False, [],
                                                delete_duplicates=delete_duplicates,
                                                rename_conflicts=rename_conflicts)
    return _normalise_items(enrich_actions(root, actions))


def build_namestyle_items(root: Path, rename_conflicts: bool, delete_duplicates: bool,
                           case_style: str, sep_style: str) -> list[dict]:
    quarantine_dir = root / QUARANTINE_DIRNAME
    stats = plan_and_maybe_execute_normalize(root, quarantine_dir, False, [],
                                              delete_duplicates=delete_duplicates,
                                              rename_conflicts=rename_conflicts,
                                              case_style=case_style, sep_style=sep_style)
    return _normalise_items(enrich_actions(root, stats["actions"]))


def build_visual_items(root: Path, decisions: dict, delete_duplicates: bool) -> list[dict]:
    plan, _ = build_apply_plan(root, decisions)
    action = "delete" if delete_duplicates else "quarantine"
    items = []
    for keep_list, discard_rel, dpath in plan:
        sz = dpath.stat().st_size if dpath.exists() else 0
        items.append({"op": "visual", "action": action, "path": discard_rel,
                      "dest": None, "kept": ", ".join(keep_list) or None, "size": sz, "is_dir": False})
    return items


def do_build_review(root: Path, ops: list[str], prefer: str, rename_conflicts: bool,
                     delete_duplicates: bool, state: State, prog: Progress,
                     case_style: str = "lower", sep_style: str = "none") -> dict:
    ordered_ops = [o for o in OP_ORDER if o in ops]
    phase_count = sum(2 if o == "normalise" else 1 for o in ordered_ops) or 1

    items = []
    idx = 0
    for op in ordered_ops:
        if op == "identical":
            prog.phase_tick(idx, OP_NAMES[op], 0, 1)
            items.extend(build_identical_items(root, prefer, delete_duplicates, state.extensions))
            prog.phase_tick(idx, OP_NAMES[op], 1, 1)
            idx += 1
        elif op == "normalise":
            prog.phase_tick(idx, "Normalisation: directory merge", 0, 1)
            items.extend(build_dirmerge_items(root, rename_conflicts, delete_duplicates))
            prog.phase_tick(idx, "Normalisation: directory merge", 1, 1)
            idx += 1
            prog.phase_tick(idx, "Normalisation: name styles", 0, 1)
            items.extend(build_namestyle_items(root, rename_conflicts, delete_duplicates, case_style, sep_style))
            prog.phase_tick(idx, "Normalisation: name styles", 1, 1)
            idx += 1
        elif op == "visual":
            prog.phase_tick(idx, OP_NAMES[op], 0, 1)
            with state.lock:
                decisions = dict(state.decisions)
            items.extend(build_visual_items(root, decisions, delete_duplicates))
            prog.phase_tick(idx, OP_NAMES[op], 1, 1)
            idx += 1

    counts = {}
    total_size = 0
    for it in items:
        counts[it["action"]] = counts.get(it["action"], 0) + 1
        total_size += it["size"]

    return {"items": items, "counts": counts, "total_size_human": human(total_size), "ops": ordered_ops,
            "delete_duplicates": delete_duplicates}


def do_run(root: Path, ops: list[str], prefer: str, rename_conflicts: bool, delete_duplicates: bool,
           state: State, prog: Progress, case_style: str = "lower", sep_style: str = "none") -> dict:
    ordered_ops = [o for o in OP_ORDER if o in ops]
    phase_count = sum(2 if o == "normalise" else 1 for o in ordered_ops) + 1  # +1 cleanup phase
    result = {"identical": None, "normalise": None, "visual": None}

    def go(manifest):
        idx = 0
        for op in ordered_ops:
            if op == "identical":
                prog.phase_tick(idx, f"Running: {OP_NAMES[op]}", 0, 1)
                quarantine_dir = root / QUARANTINE_DIRNAME
                plan = plan_file_dedupe(root, state.extensions, quarantine_dir, prefer)
                moved = execute_file_dedupe(plan, root, quarantine_dir, manifest,
                                             delete_duplicates=delete_duplicates)
                result["identical"] = {"processed": moved}
                prog.phase_tick(idx, f"Running: {OP_NAMES[op]}", 1, 1)
                idx += 1
            elif op == "normalise":
                quarantine_dir = root / QUARANTINE_DIRNAME
                prog.phase_tick(idx, "Running: Normalisation (directory merge)", 0, 1)
                dm_actions = plan_and_maybe_execute_dir_merge(root, quarantine_dir, True, manifest,
                                                                delete_duplicates=delete_duplicates,
                                                                rename_conflicts=rename_conflicts)
                prog.phase_tick(idx, "Running: Normalisation (directory merge)", 1, 1)
                idx += 1
                prog.phase_tick(idx, "Running: Normalisation (name styles)", 0, 1)
                lc_stats = plan_and_maybe_execute_normalize(root, quarantine_dir, True, manifest,
                                                             delete_duplicates=delete_duplicates,
                                                             rename_conflicts=rename_conflicts,
                                                             case_style=case_style, sep_style=sep_style)
                prog.phase_tick(idx, "Running: Normalisation (name styles)", 1, 1)
                idx += 1
                dm_counts = {"move": 0, "quarantine": 0, "delete": 0, "rename": 0, "conflict": 0}
                for a in dm_actions:
                    dm_counts[a["type"]] = dm_counts.get(a["type"], 0) + 1
                result["normalise"] = {
                    "dir_move": dm_counts["move"], "dir_processed": dm_counts["quarantine"] + dm_counts["delete"],
                    "dir_renamed": dm_counts["rename"], "dir_conflicts": dm_counts["conflict"],
                    "renamed_files": lc_stats["renamed_files"], "renamed_dirs": lc_stats["renamed_dirs"],
                    "merged_files": lc_stats["merged_files"], "processed": lc_stats["quarantined"],
                    "conflicts": len(lc_stats["conflicts"]),
                }
            elif op == "visual":
                prog.phase_tick(idx, f"Running: {OP_NAMES[op]}", 0, 1)
                with state.lock:
                    decisions = dict(state.decisions)
                quarantine_dir = root / QUARANTINE_DIRNAME
                plan, _ = build_apply_plan(root, decisions)
                moved = apply_plan(root, plan, quarantine_dir, manifest, delete_duplicates=delete_duplicates)
                result["visual"] = {"processed": moved}
                prog.phase_tick(idx, f"Running: {OP_NAMES[op]}", 1, 1)
                idx += 1

        prog.phase_tick(idx, "Cleaning up empty directories", 0, 1)
        quarantine_dir = root / QUARANTINE_DIRNAME
        emptied = prune_empty_dirs(root, quarantine_dir, True)
        result["emptied_dirs"] = len(emptied)
        result["delete_duplicates"] = delete_duplicates
        prog.phase_tick(idx, "Cleaning up empty directories", 1, 1)

    run_with_manifest(root, go)
    return result


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Dragoshi's Super Duper Image De-Duper</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #14161a;
    --bg-elevated: #191c21;
    --surface: #1e2127;
    --surface-hover: #262a31;
    --border: #2b3037;
    --border-strong: #3a4049;
    --text: #edeef1;
    --text-dim: #9aa1ac;
    --text-faint: #6b7280;
    --accent: #4169e1;
    --accent-hover: #5578e8;
    --accent-bg: #182238;
    --accent-border: #2f4577;
    --success: #3ea86f;
    --success-hover: #46bb7c;
    --success-bg: #142a1e;
    --success-border: #245c3c;
    --danger: #e5484d;
    --danger-hover: #ef5b60;
    --danger-bg: #2e1618;
    --danger-border: #5c2529;
    --warn: #e8a23e;
    --warn-bg: #2e2410;
    --warn-border: #5c4a1f;
    --radius: 10px;
    --radius-sm: 7px;
  }
  * { box-sizing: border-box; }
  body { margin:0; font-family: -apple-system, "Segoe UI", system-ui, sans-serif; background:var(--bg); color:var(--text); font-size:14px; line-height:1.5; }
  code { background:var(--surface); border:1px solid var(--border); border-radius:4px; padding:1px 5px; font-size:12px; }

  /* ---------- header ---------- */
  header { background:var(--bg-elevated); border-bottom:1px solid var(--border); }
  .header-title { display:flex; align-items:center; gap:9px; padding:16px 24px 4px; }
  .header-title .mark { color:var(--accent); font-size:20px; line-height:1; }
  .header-title h1 { font-size:17px; font-weight:700; margin:0; letter-spacing:-.01em; }
  .header-dir { display:flex; align-items:center; gap:14px; margin:12px 24px 16px; padding:12px 16px; background:var(--surface); border:1px solid var(--border); border-radius:var(--radius); }
  .dir-info { display:flex; flex-direction:column; gap:3px; min-width:0; flex:1; }
  .dir-label { font-size:10px; font-weight:700; letter-spacing:.09em; color:var(--text-faint); text-transform:uppercase; }
  .dir-value { font-size:15px; font-weight:600; color:var(--text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .dir-value.empty-value { color:var(--text-faint); font-weight:500; font-style:italic; }

  /* ---------- buttons ---------- */
  .btn { border:1px solid var(--border-strong); border-radius:var(--radius-sm); padding:8px 16px; font-size:13px; font-weight:600; cursor:pointer; background:var(--surface); color:var(--text); transition: background .12s, border-color .12s, transform .05s; white-space:nowrap; }
  .btn:hover { background:var(--surface-hover); }
  .btn:active { transform: scale(.97); }
  .btn:disabled { opacity:.4; cursor:not-allowed; transform:none; }
  .btn-sm { padding:5px 11px; font-size:12px; }
  .btn-lg { padding:11px 22px; font-size:14px; }
  .btn-accent { background:var(--accent); border-color:var(--accent); color:#fff; }
  .btn-accent:hover { background:var(--accent-hover); border-color:var(--accent-hover); }
  .btn-primary { background:var(--success); border-color:var(--success); color:#fff; }
  .btn-primary:hover { background:var(--success-hover); border-color:var(--success-hover); }
  .btn-danger { background:var(--danger); border-color:var(--danger); color:#fff; }
  .btn-danger:hover { background:var(--danger-hover); border-color:var(--danger-hover); }

  /* ---------- nav ---------- */
  #tabs { display:flex; gap:2px; padding:0 24px; background:var(--bg); border-bottom:1px solid var(--border); }
  #tabs button { background:none; border:none; color:var(--text-dim); padding:13px 20px; cursor:pointer; font-size:14px; font-weight:700; border-bottom:3px solid transparent; transition: color .12s, border-color .12s; }
  #tabs button:hover { color:var(--text); }
  #tabs button.active { color:var(--accent); border-bottom-color:var(--accent); }
  #subtabs { display:flex; gap:2px; padding:0 24px; background:var(--bg-elevated); border-bottom:1px solid var(--border); }
  #subtabs button { background:none; border:none; color:var(--text-dim); padding:10px 18px; cursor:pointer; font-size:13px; font-weight:600; border-bottom:2px solid transparent; transition: color .12s, border-color .12s; }
  #subtabs button:hover { color:var(--text); }
  #subtabs button.active { color:var(--text); border-bottom-color:var(--accent); }
  .tab-badge { display:inline-block; margin-left:7px; background:var(--accent); color:#fff; font-size:11px; font-weight:800; border-radius:10px; padding:1px 7px; vertical-align:2px; }
  .subtab-spinner { display:inline-block; width:11px; height:11px; margin-left:7px; vertical-align:-1px; border-radius:50%; border:2px solid var(--border-strong); border-top-color:var(--accent); animation:scan-spin .7s linear infinite; }

  main { padding:24px; padding-bottom:64px; max-width:1400px; margin:0 auto; }
  .tabpanel { display:none; }
  .tabpanel.active { display:block; }
  h2.page-title { font-size:19px; font-weight:700; margin:0 0 4px; color:var(--text); }
  p.page-sub { color:var(--text-dim); font-size:13px; margin:0 0 18px; }

  .toolbar { display:flex; gap:10px; align-items:center; margin-bottom:16px; flex-wrap:wrap; }
  .toolbar select { background:var(--surface); color:var(--text); border:1px solid var(--border-strong); border-radius:var(--radius-sm); padding:7px 10px; font-size:13px; }
  .toolbar label { font-size:13px; color:var(--text-dim); display:flex; align-items:center; gap:6px; }
  .toolbar .spacer-note { color:var(--text-faint); font-size:12px; margin-left:auto; }

  .summary { color:var(--text-dim); font-size:13px; margin-bottom:14px; }
  .empty { padding:48px 20px; text-align:center; color:var(--text-dim); background:var(--surface); border:1px dashed var(--border-strong); border-radius:var(--radius); }
  .empty-hint { color:var(--text-faint); font-size:12.5px; }
  .spinner { padding:52px 24px; text-align:center; color:var(--text-dim); }
  .spinner .ring { width:32px; height:32px; margin:0 auto 16px; border-radius:50%; border:3px solid var(--border); border-top-color:var(--accent); animation:scan-spin .8s linear infinite; }

  table.plan { width:100%; border-collapse:collapse; font-size:13px; background:var(--surface); border:1px solid var(--border); border-radius:var(--radius); overflow:hidden; }
  table.plan th { text-align:left; color:var(--text-faint); font-weight:700; font-size:11px; letter-spacing:.04em; text-transform:uppercase; padding:9px 12px; border-bottom:1px solid var(--border); }
  table.plan td { padding:8px 12px; border-bottom:1px solid var(--border); vertical-align:top; }
  table.plan tr:last-child td { border-bottom:none; }
  table.plan tr.conflict td { color:var(--danger); }
  table.plan tr.move td { color:#7fa8f5; }
  table.plan tr.delete td { color:#ff8f93; }
  table.plan tr.rename td { color:var(--success); }
  table.plan tr.quarantine td { color:#f0a0a3; }
  /* more specific than tr.rename above, so it wins: a rename that only
     exists because "rename conflicting file names" resolved a naming
     collision, not an ordinary case/separator-style rename */
  table.plan tr.rename.conflict-resolved td { color:#e6c34d; }
  table.plan th.sortable { cursor:pointer; user-select:none; }
  table.plan th.sortable:hover { color:var(--text); }
  .dup-count { color:var(--text-faint); font-size:12px; }

  /* ---------- Identical Files: expandable "Kept Files" rows ---------- */
  /* Duplicates column widened to fit its own header label ("Duplicates" is
     wider than a bare digit) - too narrow and text-align:center centers
     each of the header/value within a box too small for the header text,
     which then overflows and throws off the alignment between them. */
  .kept-files-header, .kept-row { display:grid; grid-template-columns:92px 52px 1fr 18px; gap:12px; align-items:center; }
  .kept-files-header { padding:0 12px 8px; font-size:11px; color:var(--text-faint); font-weight:700; text-transform:uppercase; letter-spacing:.04em; }
  .kept-files-header span.sortable { cursor:pointer; user-select:none; }
  .kept-files-header span.sortable:hover { color:var(--text); }
  .kept-files-header .dup-header-cell { text-align:center; }
  .kept-files-list { display:flex; flex-direction:column; gap:8px; }
  .kept-row-wrap { border:1px solid var(--border); border-radius:var(--radius-sm); background:var(--surface); overflow:hidden; }
  .kept-row { padding:9px 12px; cursor:pointer; transition:background .12s; }
  .kept-row:hover { background:var(--surface-hover); }
  .kept-row .dup-count-cell { font-weight:700; color:var(--text); font-size:15px; text-align:center; }
  .kept-row .info { min-width:0; }
  .kept-row .info .path { font-size:14.5px; word-break:break-all; }
  .kept-row .info .meta { font-size:11.5px; color:var(--text-dim); margin-top:2px; }
  .kept-row .expand-arrow { color:var(--text-faint); font-size:11px; transition:transform .15s; justify-self:end; }
  .kept-row-wrap.expanded .expand-arrow { transform:rotate(90deg); }
  /* max-height (not height:auto) so this can transition at all - CSS
     can't animate to/from "auto". 2000px is just a safe ceiling, not a
     real cap: as long as actual content stays under it (any realistic
     duplicate-count list will), the visible expand still tracks the
     transition duration correctly. */
  .dup-detail { max-height:0; overflow:hidden; transition:max-height .25s ease; }
  .kept-row-wrap.expanded .dup-detail { max-height:2000px; }
  .dup-detail-inner { border-top:1px solid var(--border); }
  /* Duplicates: a plain (unboxed) list, not individual thumb-row boxes -
     a shared red-tinted background reading as "these are the ones going
     away", starting where the kept row's own thumbnail column starts
     (12px .kept-row left padding + 92px duplicates column + 12px grid
     gap = 116px - keep in sync with .kept-row's grid-template-columns
     above if those ever change) and running flush to the right edge.
     The left border sits on the box itself, so it's one continuous line
     down the whole list rather than being redrawn per row. */
  .dup-plain-list { background:var(--danger-bg); border-radius:0; margin-left:116px; border-left:1px solid var(--border-strong); }
  /* Row's own column is just the thumb + info - the duplicates-count
     column doesn't exist here, since .dup-plain-list itself already
     starts at that offset. Thumb column is the same 52px width as the
     kept row's thumb column, with the (smaller) 36px thumb centered
     within it, so it lines up horizontally with the kept image above. */
  .dup-plain-row { display:grid; grid-template-columns:52px 1fr; gap:12px; align-items:center; padding:8px 12px 8px 0; }
  .dup-plain-row + .dup-plain-row { border-top:1px solid rgba(255,255,255,.06); }
  .dup-plain-row .thumb { width:36px; height:36px; justify-self:center; }
  .dup-plain-row .info { min-width:0; }
  .dup-plain-row .info .path { font-size:12.5px; color:#f2b6b9; word-break:break-all; }
  .dup-plain-row .info .meta { font-size:11px; color:#d99a9c; margin-top:2px; }

  .group-block { margin-bottom:16px; border:1px solid var(--border); border-radius:var(--radius); overflow:hidden; }
  .group-block .gh { background:var(--surface-hover); padding:9px 14px; font-size:12px; color:var(--text-dim); font-weight:700; }

  /* ---------- pending toggle (on Operations tabs) ---------- */
  /* inline-flex (not flex): shrinks to fit its content instead of
     stretching the full row width. That puts the checkbox/label and the
     note right next to each other now (no more wide auto-margin gap
     between them), so the note gets its own left border as a clear
     divider instead, rather than relying on distance to separate them. */
  .pending-toggle { display:inline-flex; align-items:center; gap:10px; padding:10px 14px; background:var(--accent-bg); border:1px solid var(--accent-border); border-radius:var(--radius); margin-bottom:18px; }
  .pending-toggle.disabled { background:var(--surface); border-color:var(--border); opacity:.7; }
  .pending-toggle label { font-weight:700; font-size:13.5px; cursor:pointer; }
  .pending-toggle.disabled label { cursor:not-allowed; color:var(--text-dim); }
  .pending-toggle .pending-note { color:var(--text-dim); font-size:12.5px; padding-left:12px; margin-left:2px; border-left:1px solid var(--accent-border); white-space:nowrap; }
  .pending-toggle.disabled .pending-note { border-left-color:var(--border-strong); }

  /* ---------- visually similar review ---------- */
  #progress-track { flex:1; min-width:160px; }
  .bar { height:8px; background:var(--border); border-radius:4px; overflow:hidden; }
  .bar-fill { height:100%; background:var(--success); transition:width .2s; }
  #progress-label { font-size:12px; color:var(--text-dim); margin-top:5px; }
  /* Prominent stat bar - three segments (image count / group position /
     live keep-discard split) divided by vertical rules, matching the
     divider treatment already used in .pending-toggle .pending-note. */
  .nd-stats { display:flex; align-items:center; margin:16px 0 14px; padding:14px 20px; background:var(--surface); border:1px solid var(--border-strong); border-radius:var(--radius); font-size:16px; color:var(--text-dim); }
  .nd-stats .nd-stat { padding:0 22px; }
  .nd-stats .nd-stat:first-child { padding-left:0; }
  .nd-stats .nd-stat + .nd-stat { border-left:1px solid var(--border-strong); }
  .nd-stats .nd-stat b { color:var(--text); font-weight:700; }
  .nd-stats #nd-keep-count { color:var(--success); }
  .nd-stats #nd-discard-count { color:var(--danger); }
  #cards-wrap { display:flex; align-items:center; gap:10px; }
  #cards { display:flex; flex-wrap:nowrap; gap:16px; overflow-x:auto; scroll-behavior:smooth; scrollbar-width:thin; padding-bottom:4px; flex:1; min-width:0; cursor:grab; user-select:none; }
  #cards.dragging { cursor:grabbing; }
  #cards.dragging .card { transition:none; }
  .carousel-arrow { flex:0 0 auto; width:38px; height:38px; border-radius:50%; border:1px solid var(--border-strong); background:var(--surface); color:var(--text); font-size:19px; line-height:1; cursor:pointer; display:flex; align-items:center; justify-content:center; transition: background .15s, transform .1s, opacity .15s; }
  .carousel-arrow:hover:not(:disabled) { background:var(--surface-hover); border-color:var(--accent); }
  .carousel-arrow:active:not(:disabled) { transform:scale(.9); }
  .carousel-arrow:disabled { opacity:.25; cursor:default; }
  .carousel-arrow.hidden { display:none; }
  .card { border:2px solid var(--border); border-radius:var(--radius); padding:10px; cursor:pointer; background:var(--surface); max-width:44vw; flex:0 0 auto; transition: border-color .15s, opacity .15s; }
  .card:hover { border-color:var(--accent); }
  .card.keep { border-color:var(--success); }
  .card.discard { border-color:var(--danger); opacity:.55; }
  .card img { max-width:100%; max-height:58vh; display:block; border-radius:5px; object-fit:contain; -webkit-user-drag:none; user-drag:none; }
  .cap { margin-top:8px; font-size:12px; color:var(--text-dim); line-height:1.6; }
  .cap .key { display:inline-block; background:var(--border); border-radius:4px; padding:0 6px; margin-right:6px; font-weight:700; color:var(--text); }
  .cap .path { color:var(--text); font-weight:600; word-break:break-all; }
  .cap .state { float:right; font-weight:700; }
  .card.keep .cap .state { color:var(--success); }
  .card.discard .cap .state { color:var(--danger); }
  .badge { display:inline-block; background:var(--success); color:#08150e; font-size:10.5px; font-weight:700; border-radius:4px; padding:1px 6px; margin-left:6px; }
  footer.nd-footer { position:sticky; bottom:0; padding:14px 20px; background:var(--bg-elevated); border:1px solid var(--border); border-radius:var(--radius); margin-top:16px; display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
  footer.nd-footer .hint { color:var(--text-faint); font-size:12px; }

  /* ---------- overlays / modals ---------- */
  /* opacity/visibility (not display) so opening/closing can transition
     smoothly - display can't be animated, and visibility:hidden already
     drops the overlay from hit-testing, so closed ones don't eat clicks */
  .overlay { position:fixed; inset:0; background:rgba(8,9,11,.72); display:flex; align-items:center; justify-content:center; z-index:20; opacity:0; visibility:hidden; transition:opacity .18s ease; }
  .overlay.open { opacity:1; visibility:visible; }
  .overlay .modal { transform:scale(.97); transition:transform .18s cubic-bezier(.2,.8,.3,1); }
  .overlay.open .modal { transform:scale(1); }
  .modal { background:var(--bg-elevated); border:1px solid var(--border-strong); border-radius:var(--radius); width:min(680px, 92vw); max-height:82vh; display:flex; flex-direction:column; box-shadow:0 20px 60px rgba(0,0,0,.5); }
  .modal-title { padding:16px 18px; border-bottom:1px solid var(--border); font-size:15px; font-weight:700; }
  .modal-body { overflow-y:auto; padding:14px 18px; font-size:13.5px; color:var(--text-dim); flex:1; }
  .modal-body p { margin:0 0 10px; }
  .modal-body ul { margin:0; padding-left:18px; }
  .modal-body li { padding:2px 0; word-break:break-all; }
  .modal-actions { padding:14px 18px; border-top:1px solid var(--border); display:flex; justify-content:flex-end; gap:10px; }
  /* Fixed height so navigating between directories with different numbers
     of sub-folders doesn't resize (and re-centre) the whole dialog -
     #picker-list flexes to fill and scrolls instead. */
  #picker-overlay .modal { height:min(620px, 82vh); }
  #picker-path { padding:16px 18px; border-bottom:1px solid var(--border); font-size:13px; color:var(--text-dim); white-space:nowrap; overflow-x:auto; }
  #picker-newrow { display:flex; gap:8px; padding:10px 18px; border-bottom:1px solid var(--border); }
  #picker-newrow input { flex:1; background:var(--bg); color:var(--text); border:1px solid var(--border-strong); border-radius:var(--radius-sm); padding:6px 9px; font-size:13px; }
  #picker-list { overflow-y:auto; flex:1; padding:6px; }
  #picker-list button { display:block; width:100%; text-align:left; background:none; border:none; color:var(--text); padding:9px 11px; border-radius:var(--radius-sm); cursor:pointer; font-size:13px; }
  #picker-list button:hover { background:var(--surface-hover); }
  #picker-list .up { color:var(--accent); font-weight:600; }
  #picker-error { color:var(--danger); font-size:12px; padding:0 18px 10px; }
  #progress-overlay .modal { width:min(480px, 90vw); }
  #progress-overlay .modal-body { text-align:center; padding:28px 18px; }
  #progress-overlay .big-pct { font-size:34px; font-weight:800; margin-bottom:12px; color:var(--accent); }

  @keyframes scan-spin { to { transform:rotate(360deg); } }

  /* ---------- bottom status bar (background scan/scoring) ---------- */
  #status-bar {
    position:fixed; left:0; right:0; bottom:0; z-index:15;
    background:var(--bg-elevated); border-top:1px solid var(--border-strong);
    padding:10px 20px; transform:translateY(100%); transition:transform .25s ease;
  }
  #status-bar.open { transform:translateY(0); }
  /* #status-bar-inner fills up to 1400px and is itself page-centered via
     margin:auto, but that alone doesn't center its *content* within that
     (invisible, background-less) box - flex items default to
     justify-content:flex-start, so the text+bar cluster was sitting
     flush against the inner box's left edge: neither flush with the true
     viewport edge nor centered on the page, just an awkward in-between
     depending on viewport width. justify-content:center fixes that. */
  #status-bar-inner { display:flex; align-items:center; justify-content:center; gap:14px; font-size:13px; color:var(--text-dim); max-width:1400px; margin:0 auto; }
  #status-bar-text { flex-shrink:0; white-space:nowrap; }
  #status-bar .bar { flex:1; max-width:280px; }

  /* ---------- jobs / pending ---------- */
  .pending-ops-list { display:flex; flex-direction:column; gap:10px; margin-bottom:20px; }
  .pending-op-row { display:flex; align-items:center; gap:14px; padding:14px 16px; border:1px solid var(--border-strong); border-left:3px solid var(--accent); border-radius:var(--radius); background:var(--surface); }
  .pending-op-row .op-name { font-weight:700; font-size:14.5px; flex:0 0 170px; }
  .pending-op-row .op-note { color:var(--text-dim); font-size:12.5px; flex:1; }
  .checkbox-row { display:flex; align-items:center; gap:9px; font-size:13.5px; color:var(--text); margin-bottom:12px; }
  .checkbox-row label { cursor:pointer; }
  input[type=checkbox] { accent-color: var(--accent); width:17px; height:17px; cursor:pointer; }

  .thumb-grid { display:flex; flex-direction:column; gap:8px; }
  .thumb-row { display:flex; align-items:center; gap:12px; padding:9px 12px; border:1px solid var(--border); border-radius:var(--radius-sm); background:var(--surface); }
  .thumb-row.conflict { border-color:var(--danger-border); background:var(--danger-bg); }
  /* Unscoped (not .thumb-row .thumb): thumbHtml() is shared by the Jobs
     tab's .thumb-row list AND Identical Files' .kept-row grid, which
     isn't a .thumb-row - scoping this to .thumb-row left .kept-row's
     thumbnail with no size constraint at all, rendering at full native
     resolution and blowing out the page. */
  .thumb { width:52px; height:52px; object-fit:cover; border-radius:6px; background:var(--bg); flex-shrink:0; }
  .thumb.placeholder { display:flex; align-items:center; justify-content:center; color:var(--text-faint); font-size:10px; }
  .thumb-row .info { flex:1; min-width:0; }
  .thumb-row .info .path { font-weight:600; font-size:13px; word-break:break-all; }
  .thumb-row .info .meta { font-size:11.5px; color:var(--text-dim); margin-top:2px; }
  .action-tag { font-size:10.5px; font-weight:700; border-radius:4px; padding:2px 8px; text-transform:uppercase; flex-shrink:0; letter-spacing:.03em; }
  .action-tag.quarantine { background:var(--danger-bg); color:#f0a0a3; }
  .action-tag.delete { background:var(--danger-bg); color:#ff8f93; }
  .action-tag.move { background:var(--accent-bg); color:#93b4f5; }
  .action-tag.rename { background:var(--success-bg); color:#7ad19a; }
  .action-tag.conflict { background:var(--danger); color:#fff; }
  .op-tag { font-size:10px; color:var(--text-faint); border:1px solid var(--border-strong); border-radius:4px; padding:2px 7px; flex-shrink:0; }

  .quarantine-status { padding:22px; border:1px solid var(--border); border-radius:var(--radius); background:var(--surface); margin-bottom:16px; }
  .quarantine-status .big { font-size:24px; font-weight:800; margin-bottom:6px; }

  /* ---------- upscale ---------- */
  .upscale-slider-row { display:flex; align-items:center; gap:14px; padding:16px 18px; background:var(--surface); border:1px solid var(--border); border-radius:var(--radius); margin-bottom:12px; flex-wrap:wrap; }
  .upscale-slider-row label { font-weight:600; font-size:13.5px; white-space:nowrap; }
  .upscale-slider-row input[type=range] { flex:1; min-width:220px; accent-color:var(--accent); height:6px; cursor:pointer; }
  .upscale-target-value { color:var(--accent); font-weight:700; }
  .upscale-summary { color:var(--text-dim); font-size:13px; margin-bottom:14px; }
  /* Start button sits top-right on its own row, just above the scroll box */
  .upscale-run-row { display:flex; align-items:center; gap:12px; margin-bottom:12px; }
  .upscale-run-row .upscale-summary { flex:1; min-width:0; margin-bottom:0; }
  .upscale-run-row .spacer-note { color:var(--text-faint); font-size:12px; }
  .upscale-run-row .btn { flex-shrink:0; }
  /* Eligible-image list capped at ~5 rows (~72px each + 8px gap), then scrolls */
  .upscale-thumb-scroll { max-height:400px; overflow-y:auto; padding:2px; }
  .upscale-opt-row { display:flex; align-items:center; gap:10px; padding:12px 18px; background:var(--surface); border:1px solid var(--border); border-radius:var(--radius); margin-bottom:12px; flex-wrap:wrap; font-size:13px; }
  .upscale-opt-row > label:first-child { font-weight:600; font-size:13.5px; white-space:nowrap; }
  .upscale-opt-row input[type=text] { background:var(--bg); color:var(--text); border:1px solid var(--border-strong); border-radius:var(--radius-sm); padding:6px 9px; font-size:13px; }
  .upscale-opt-row select { background:var(--surface); color:var(--text); border:1px solid var(--border-strong); border-radius:var(--radius-sm); padding:6px 9px; font-size:13px; }
  .upscale-opt-row code { background:var(--bg); padding:2px 6px; border-radius:4px; font-size:12px; word-break:break-all; }
  .upscale-opt-row .dim { color:var(--text-faint); font-size:12px; }
  .job-log { text-align:left; margin:14px 0 0; max-height:190px; overflow-y:auto; background:var(--bg); border:1px solid var(--border); border-radius:var(--radius-sm); padding:10px 12px; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:11.5px; line-height:1.5; color:var(--text-dim); white-space:pre-wrap; word-break:break-all; }
  .warn-box { background:var(--danger-bg); border:1px solid var(--danger-border); border-radius:var(--radius); padding:14px 16px; color:#f2b6b9; font-size:13px; margin-bottom:12px; }
  .warn-box b { color:#ffcdcf; }
  /* solid yellow outline, faded/low-opacity yellow fill - distinct from
     the red .warn-box above, used for "here's a conflict, here's your
     option" rather than "this is destructive/irreversible" */
  .warn-box.warn-yellow { background:rgba(230,180,30,.12); border:2px solid #e6b41e; color:#e8d29a; }
  .warn-box.warn-yellow b { color:#f5da8a; }
  .warn-box.warn-yellow label { cursor:pointer; }

  /* ---------- decorative background easter egg (purely cosmetic) ----------
     Fixed behind everything (negative z-index) and pointer-events:none,
     so it can never intercept a click/scroll or otherwise affect
     usability - it only shows through the page's own background, never
     over cards/tables/text, since those all paint on top of it normally. */
  #kobold-bg { position:fixed; inset:0; z-index:-1; overflow:hidden; pointer-events:none; user-select:none; }
  .kobold-word {
    position:absolute; font-size:13px; font-weight:700; letter-spacing:.04em;
    color:var(--text-faint); opacity:0; white-space:nowrap; will-change:opacity, transform;
    animation-name:kobold-peek; animation-iteration-count:infinite; animation-timing-function:ease-in-out;
  }
  @keyframes kobold-peek {
    0%, 82%   { opacity:0; transform:translateX(-8px); }
    86%       { opacity:.08; transform:translateX(-2px); }
    92%       { opacity:.08; transform:translateX(4px); }
    97%, 100% { opacity:0; transform:translateX(10px); }
  }
</style></head>
<body>
<div id="kobold-bg" aria-hidden="true"></div>
<header>
  <div class="header-title"><span class="mark">&#10687;</span><h1>Dragoshi's Super Duper Image De-Duper</h1></div>
  <div class="header-dir">
    <div class="dir-info">
      <span class="dir-label">Current Image Collection</span>
      <span id="root-label" class="dir-value empty-value">No directory chosen</span>
    </div>
    <button id="change-dir" class="btn btn-accent">Change</button>
  </div>
</header>
<div id="tabs">
  <button data-tab="operations" class="active">Operations</button>
  <button data-tab="jobs">Jobs<span class="tab-badge" id="jobs-badge" style="display:none"></span></button>
  <button data-tab="quarantine">Quarantine<span class="tab-badge" id="quarantine-badge" style="display:none"></span></button>
</div>
<div id="subtabs">
  <button data-subtab="identical" class="active">Identical Files<span class="subtab-spinner" id="spin-identical" style="display:none"></span></button>
  <button data-subtab="visual">Visually Similar<span class="subtab-spinner" id="spin-visual" style="display:none"></span></button>
  <button data-subtab="normalise">Normalisation<span class="subtab-spinner" id="spin-normalise" style="display:none"></span></button>
  <button data-subtab="upscale">Upscale<span class="subtab-spinner" id="spin-upscale" style="display:none"></span></button>
</div>
<main>
  <div id="tab-operations" class="tabpanel active">
    <div id="sub-identical" class="subpanel"></div>
    <div id="sub-visual" class="subpanel" style="display:none"></div>
    <div id="sub-normalise" class="subpanel" style="display:none"></div>
    <div id="sub-upscale" class="subpanel" style="display:none"></div>
  </div>
  <div id="tab-jobs" class="tabpanel"></div>
  <div id="tab-quarantine" class="tabpanel"></div>
</main>

<div id="picker-overlay" class="overlay">
  <div class="modal">
    <div class="modal-title" id="picker-title">Choose a directory</div>
    <div id="picker-path"></div>
    <div id="picker-newrow" hidden>
      <input type="text" id="picker-newname" placeholder="New folder name" spellcheck="false" autocomplete="off">
      <button id="picker-newbtn" class="btn btn-sm">Create folder</button>
    </div>
    <div id="picker-list"></div>
    <div id="picker-error"></div>
    <div class="modal-actions">
      <button id="picker-cancel" class="btn">Cancel</button>
      <button id="picker-use" class="btn btn-accent">Use this directory</button>
    </div>
  </div>
</div>

<div id="confirm-overlay" class="overlay">
  <div class="modal">
    <div class="modal-title" id="confirm-title"></div>
    <div class="modal-body" id="confirm-body"></div>
    <div class="modal-actions">
      <button id="confirm-cancel" class="btn">Cancel</button>
      <button id="confirm-go" class="btn btn-danger">Confirm</button>
    </div>
  </div>
</div>

<div id="progress-overlay" class="overlay">
  <div class="modal">
    <div class="modal-title" id="progress-title">Working&hellip;</div>
    <div class="modal-body">
      <div class="big-pct" id="progress-pct">0%</div>
      <div class="bar"><div class="bar-fill" id="progress-fill" style="width:0%"></div></div>
      <div id="progress-label" style="margin-top:10px"></div>
      <pre id="progress-log" class="job-log" hidden></pre>
    </div>
  </div>
</div>

<div id="status-bar">
  <div id="status-bar-inner">
    <span id="status-bar-text"></span>
    <div class="bar"><div class="bar-fill" id="status-bar-fill" style="width:0%"></div></div>
  </div>
</div>

<script>
let currentRoot = null;
let activeTab = 'operations';
let activeSubtab = 'identical';

// ---------- shared: pluralization / formatting ----------

function plural(n, word) { return `${n} ${word}${n === 1 ? '' : 's'}`; }
function esc(s) { return (s+'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function human(n) {
  const units = ['B','KB','MB','GB'];
  let i = 0;
  n = n || 0;
  while (n >= 1024 && i < units.length-1) { n /= 1024; i++; }
  return n.toFixed(1) + units[i];
}
function isImageExt(p) {
  return /\.(png|jpe?g|gif|bmp|webp|tiff?)$/i.test(p);
}
function formatWhen(iso) {
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString();
}

// Shared thumbnail-or-placeholder markup (Jobs tab, and Identical Files'
// kept-file/duplicate rows). An actual thumbnail for images; otherwise a
// placeholder labeled with the file's extension (SWF, PSD, ...) rather
// than a generic "file", or DIR for directories - much more useful at a
// glance when a plan mixes several different file types.
function extLabel(path) {
  const m = /\.([a-zA-Z0-9]+)$/.exec(path);
  return m ? m[1].toUpperCase() : 'FILE';
}
function thumbHtml(path, isDir) {
  if (isDir) return `<div class="thumb placeholder">DIR</div>`;
  if (isImageExt(path)) return `<img class="thumb" src="/img/${encodeURIComponent(path)}" loading="lazy">`;
  return `<div class="thumb placeholder">${esc(extLabel(path))}</div>`;
}

const OP_LABELS = {identical: 'Identical Files', normalise: 'Normalisation', visual: 'Visually Similar'};

// ---------- shared: progress polling ----------

function showProgressOverlay(title) {
  document.getElementById('progress-title').textContent = title;
  document.getElementById('progress-pct').textContent = '0%';
  document.getElementById('progress-fill').style.width = '0%';
  document.getElementById('progress-label').textContent = '';
  const log = document.getElementById('progress-log');
  log.textContent = '';
  log.hidden = true;
  document.getElementById('progress-overlay').classList.add('open');
}
function hideProgressOverlay() {
  document.getElementById('progress-overlay').classList.remove('open');
}

// Big counts (> 100k) are treated as a byte total and shown in MB - the
// only job that reports one is the realesrgan-ncnn-vulkan download.
function progressLabelText(p) {
  if (!p.phase) return '';
  if (p.total > 100000) return `${p.phase} (${(p.current/1048576).toFixed(1)} / ${(p.total/1048576).toFixed(1)} MB)`;
  return `${p.phase} (${p.current}/${p.total})`;
}

async function pollUntilDone() {
  while (true) {
    const r = await fetch('/api/progress');
    const p = await r.json();
    document.getElementById('progress-pct').textContent = p.pct + '%';
    document.getElementById('progress-fill').style.width = p.pct + '%';
    document.getElementById('progress-label').textContent = progressLabelText(p);
    const log = document.getElementById('progress-log');
    if (p.log && p.log.length) {
      const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
      log.textContent = p.log.join('\n');
      log.hidden = false;
      if (atBottom) log.scrollTop = log.scrollHeight;
    }
    if (p.done) return p;
    await new Promise(res => setTimeout(res, 350));
  }
}

async function runJob(title, startUrl, startBody) {
  showProgressOverlay(title);
  const r = await fetch(startUrl, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(startBody || {})
  });
  const started = await r.json();
  if (!started.ok) {
    hideProgressOverlay();
    alert(started.error || 'could not start');
    return null;
  }
  const final = await pollUntilDone();
  hideProgressOverlay();
  if (final.error) {
    // include the tail of the streamed log so "what actually happened"
    // survives the overlay closing (matters most for the dependency download)
    let m = 'Error: ' + final.error;
    if (final.log && final.log.length) m += '\n\n' + final.log.slice(-8).join('\n');
    alert(m);
    return null;
  }
  return final.result;
}

// ---------- per-tab loading state (big spinner in the panel, small
// spinner next to the sub-tab's name) ----------
//
// Each of the three Operations sub-tabs loads independently and shows its
// own pair of spinners until its own data is ready - no more single
// blocking modal for the whole directory. identical/normalise are simple
// stateless fetches; visual's "loading" instead tracks the scan's
// hash+group phase (see pollScan below), since that's the part with
// nothing to show yet - Hamming-distance grouping needs every hash before
// it can form any cluster at all.

let tabLoading = {identical: false, visual: false, normalise: false, upscale: false};

// pct is optional - identical/normalise don't have a meaningful one (a
// single fetch, not an incremental job), so it's only ever passed for the
// Visually Similar scan. Without it, the spinner used to sit there with no
// number at all through the whole hash+group phase - completely
// indistinguishable from actually being stuck, which is what prompted this.
function bigSpinnerHtml(label, pct) {
  const bar = (typeof pct === 'number')
    ? `<div class="bar" style="max-width:220px;margin:14px auto 0"><div class="bar-fill spinner-bar-fill" style="width:${pct}%"></div></div>` +
      `<div class="spinner-pct-text" style="margin-top:6px;font-size:12px;color:var(--text-dim)">${pct}%</div>`
    : '';
  return `<div class="spinner"><div class="ring"></div><span class="spinner-label-text">${esc(label)}</span>&hellip;${bar}</div>`;
}

// Updates an already-rendered big spinner's label/percentage in place,
// without touching the .ring element. The Visually Similar scan gets a
// fresh percentage roughly every 500ms while hashing/grouping (see
// pollScan), far more often than the ring's own ~0.8s rotation -
// overwriting the whole panel via innerHTML on every tick was recreating
// the ring element itself each time, resetting its CSS animation before a
// single rotation could ever finish. That's what looked like a stuttering
// spin that never completed - it was a brand new element with a brand new
// animation, over and over, never the same one running continuously.
function updateSpinnerInPlace(panel, label, pct) {
  const labelEl = panel.querySelector('.spinner-label-text');
  if (labelEl) labelEl.textContent = label;
  if (typeof pct === 'number') {
    const barFill = panel.querySelector('.spinner-bar-fill');
    const pctText = panel.querySelector('.spinner-pct-text');
    if (barFill) barFill.style.width = pct + '%';
    if (pctText) pctText.textContent = pct + '%';
  }
}

// A fetch that fails (network error, or the server closing the
// connection on an unhandled exception) used to leave tabLoading stuck
// true forever with no feedback at all - the spinner text just sat there
// permanently since nothing after the failed fetch ever ran to clear it.
// Every load path below now wraps its fetch in try/catch and calls this
// on failure, so a real error is at least visible and retryable instead
// of an indefinite, silent hang.
function loadErrorHtml(title, err, retryId) {
  return `<h2 class="page-title">${esc(title)}</h2><div class="empty">Failed to load: ${esc(err && err.message || String(err))}` +
    `<br><span class="empty-hint">Check the terminal running review_gui.py for details.</span><br><br>` +
    `<button class="btn" id="${retryId}">Retry</button></div>`;
}

// Small spinner (next to the sub-tab's name) is tracked separately from
// the big one for 'visual': that tab's underlying scan has two phases,
// and only the first (hashing/grouping - nothing reviewable exists yet)
// should hide real content behind the big spinner. But the small spinner
// is meant to answer "is this operation still working at all", which
// spans the *whole* scan including scoring - clearing it once scoring
// starts (even though groups are still actively being found) read as
// "done" when it very much wasn't, which was confusing. So identical/
// normalise (no phases, one fetch = fully loading or fully done) keep
// using setTabLoading for both spinners together; 'visual' additionally
// drives this one on its own from pollScan(), independent of tabLoading.visual.
function setSmallSpinner(key, visible) {
  const spin = document.getElementById('spin-' + key);
  if (spin) spin.style.display = visible ? 'inline-block' : 'none';
}

// loading=true also immediately paints the big spinner into that tab's
// panel (even if it's not the active tab right now - harmless, and means
// switching to it mid-load shows the spinner instead of stale/blank
// content). loading=false only clears the small spinner; the caller is
// responsible for rendering real content right after.
function setTabLoading(key, loading, label, pct) {
  tabLoading[key] = loading;
  setSmallSpinner(key, loading);
  if (loading) {
    const panel = document.getElementById('sub-' + key);
    if (!panel) return;
    // If a spinner is already showing (e.g. pollScan calling this again
    // with an updated percentage), update its text/bar in place rather
    // than rebuilding the whole panel - see updateSpinnerInPlace's note.
    if (panel.querySelector('.spinner .ring')) {
      updateSpinnerInPlace(panel, label || 'Loading', pct);
    } else {
      panel.innerHTML = bigSpinnerHtml(label || 'Loading', pct);
    }
  }
}

// Kicks off the Identical Files / Normalisation / Visually Similar loads
// - called whenever the chosen directory changes (picker confirm, or boot
// with an already-known root), so each of those sub-tabs' spinners is live
// from the start regardless of which one happens to be active. (Upscale
// loads lazily on first visit - see the note at the end.)
async function startAllTabLoads() {
  identicalData = null;
  normaliseData = null;
  upscaleData = null;
  // Upscale settings are scoped to the collection they were set for - a
  // previous root's output directory / affix / target must not steer the
  // next one. Nulls make loadUpscale() re-pull the server defaults.
  upscaleOutDir = null;
  upscaleAffix = null;
  upscaleAffixPos = 'suffix';
  upscaleOverwrite = false;
  upscaleTarget = null;
  ndGroups = [];
  ndDecisions = {};
  visualLoaded = false;
  updateJobsBadge();
  refreshQuarantineBadge();
  // The near-duplicate scan is already running server-side by this point
  // (kicked off by /api/set-root before this function is even called) -
  // pollScan() just polls its status, which is cheap and doesn't compete
  // for the GIL, so start it (and paint its spinners) immediately.
  // pct:0 (not omitted) so the percentage bar/text exist in the DOM from
  // this very first paint - pollScan()'s first tick (and every one after)
  // then only ever updates that existing bar/text in place, never
  // recreating the spinner, which is what keeps its animation continuous.
  setTabLoading('visual', true, 'Hashing images', 0);  // small spinner stays lit through pollScan()'s whole run
  pollScan();
  // Identical Files and Normalisation both hash their way through every
  // file (SHA-256) - real CPU-bound work that, under Python's GIL,
  // doesn't truly run in parallel with the scan's own per-image hashing
  // happening at the same time. Firing both of these at once on top of
  // that just made everything contend and slow each other down on a
  // large real directory, which is what large-directory "stuck on
  // Scanning" reports turned out to be. Sequencing these two remains
  // just one await apart from the caller's perspective, but halves that
  // particular contention.
  await loadIdentical();
  await loadNormalise();
  // Upscale is deliberately not pre-loaded here: its preview is a whole
  // extra tree walk that opens every image for its dimensions, it feeds
  // no badge, and reloadActive() lazy-loads it on first visit anyway.
}

let scanActive = false;   // read by renderVisual() to word its empty state
let scanPolling = false;  // re-entrancy guard - only one poll loop at a time

function showStatusBar(text, pct) {
  document.getElementById('status-bar-text').textContent = text;
  document.getElementById('status-bar-fill').style.width = pct + '%';
  document.getElementById('status-bar').classList.add('open');
}
function hideStatusBar() {
  document.getElementById('status-bar').classList.remove('open');
}

// Re-fetches the current group list/decisions and updates them quietly in
// the background - only actually touching the DOM (renderVisual(), which
// rebuilds the whole panel including the carousel) when what's currently
// on screen would otherwise go stale. New groups are always appended
// after existing ones and never mutated, so if ndIdx is still pointing at
// an already-available group, that group's content can't have changed -
// re-rendering on every poll tick while the user is just sitting there
// (or scrolling the carousel) was tearing down and rebuilding the whole
// panel every ~500ms during active scoring, which is what was seen as
// the carousel/scroll-buttons "flashing".
//
// ndIdx >= (old) ndGroups.length covers both cases where the currently
// displayed screen genuinely does depend on whether more groups exist:
// the empty "nothing yet"/"still scanning" state (ndIdx 0 >= length 0),
// and the "all reviewed!" state (ndIdx deliberately pushed to
// ndGroups.length once nothing is left undecided) - newly arrived groups
// should be reflected right away in both.
async function refreshVisualGroupsQuietly() {
  const r = await fetch('/api/nd/groups');
  const data = await r.json();
  const staleOnScreen = ndIdx >= ndGroups.length;
  ndGroups = data.groups;
  ndDecisions = data.decisions;
  if (staleOnScreen && activeTab === 'operations' && activeSubtab === 'visual' && !ndTouched) {
    renderVisual();
  }
}

async function pollScan() {
  if (scanPolling) return;
  scanPolling = true;
  try {
    while (true) {
      let p;
      try {
        const r = await fetch('/api/scan-progress');
        p = await r.json();
      } catch (e) {
        setTabLoading('visual', false);
        setSmallSpinner('visual', false);
        if (activeTab === 'operations' && activeSubtab === 'visual') {
          document.getElementById('sub-visual').innerHTML = loadErrorHtml('Visually Similar', e, 'visual-retry');
          document.getElementById('visual-retry').onclick = () => { scanPolling = false; pollScan(); };
        }
        break;
      }
      scanActive = p.active;
      // phase_index stays under 2 (0=hashing, 1=grouping) both during
      // genuine hashing/grouping AND briefly right after a scan is
      // requested but before its background thread has actually called
      // Progress.begin() yet (every caller of pollScan only does so once
      // it knows a scan should be running, so that race - not "no scan
      // will ever happen" - is the only way to see active:false/done:false
      // here). It also stays under 2 for a scan that finds zero candidate
      // groups, since the scoring phase it would otherwise advance into
      // never runs - hence "&& !p.done" so that case still counts as
      // finished, not stuck loading forever.
      const stillInHashPhase = p.phase_index < 2 && !p.done;
      const justLeftHashPhase = tabLoading.visual && !stillInHashPhase;
      if (stillInHashPhase) {
        // grouping (all-pairs Hamming-distance comparison) is the other
        // half of this - same "nothing to show yet" reasoning as hashing,
        // but a distinct label + its own percentage (it only ticks every
        // 50k pairs server-side - see group_by_hash - not a smooth
        // per-file count like hashing), so it doesn't look like hashing
        // got stuck at 100% for however long grouping takes.
        const within = p.total ? Math.round(100 * p.current / p.total) : 0;
        const label = p.phase_index === 0 ? 'Hashing images' : 'Comparing hashes to find matches';
        setTabLoading('visual', true, label, within);
      } else {
        setTabLoading('visual', false);
      }
      setSmallSpinner('visual', !p.done);  // stays lit through scoring too, only clears once the whole scan is done
      if (!stillInHashPhase) {
        if (p.active) {
          showStatusBar(`Scoring images for review: ${plural(p.current, 'group')} ready`, p.pct);
        } else if (p.done) {
          showStatusBar('Scan complete', 100);
        }
        // The first time content becomes available this scan, do a real
        // load (fetch + position on the first undecided group) rather
        // than the lightweight incremental merge, which intentionally
        // never touches ndIdx so it doesn't yank a reviewer around mid-review.
        if (justLeftHashPhase) await loadVisualData();
        else if (p.active || p.done) await refreshVisualGroupsQuietly();
      }
      if (p.done) {
        await refreshVisualGroupsQuietly();
        setTimeout(hideStatusBar, 1500);
        break;
      }
      await new Promise(res => setTimeout(res, 500));
    }
  } finally {
    scanPolling = false;
  }
}

// runs a build job quietly (no full-screen overlay) - used for the Jobs
// tab's auto-refresh, so toggling a checkbox doesn't flash a modal for
// what's normally a near-instant operation. Serialized via jobsBuildPromise
// so overlapping calls (e.g. rapid checkbox toggles) never race the
// server's single-job lock.
let jobsBuildPromise = null;
async function silentBuild(ops, body) {
  if (jobsBuildPromise) await jobsBuildPromise;
  jobsBuildPromise = (async () => {
    const r = await fetch('/api/review/build', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(Object.assign({ops}, body))
    });
    const started = await r.json();
    if (!started.ok) return null;
    while (true) {
      const pr = await fetch('/api/progress');
      const p = await pr.json();
      if (p.done) return p.error ? null : p.result;
      await new Promise(res => setTimeout(res, 250));
    }
  })();
  const res = await jobsBuildPromise;
  jobsBuildPromise = null;
  return res;
}

// ---------- shared: directory picker ----------

async function apiState() {
  const r = await fetch('/api/state');
  return r.json();
}

async function refreshRootLabel() {
  const s = await apiState();
  currentRoot = s.root;
  const el = document.getElementById('root-label');
  el.textContent = currentRoot || 'No directory chosen';
  el.classList.toggle('empty-value', !currentRoot);
  return s;
}

let browsePath = null;
async function browseTo(path) {
  document.getElementById('picker-error').textContent = '';
  const url = path ? `/api/browse?path=${encodeURIComponent(path)}` : '/api/browse';
  const r = await fetch(url);
  const data = await r.json();
  if (!data.ok) {
    document.getElementById('picker-error').textContent = data.error || 'could not list that directory';
    return;
  }
  browsePath = data.path;
  document.getElementById('picker-path').textContent = browsePath;
  const list = document.getElementById('picker-list');
  list.innerHTML = '';
  if (data.parent) {
    const up = document.createElement('button');
    up.className = 'up';
    up.textContent = '.. (up)';
    up.onclick = () => browseTo(data.parent);
    list.appendChild(up);
  }
  data.dirs.forEach(name => {
    const b = document.createElement('button');
    b.textContent = '📁 ' + name;
    b.onclick = () => browseTo(browsePath + (browsePath.endsWith('/') ? '' : '/') + name);
    list.appendChild(b);
  });
}

// The picker overlay is shared: openPicker() with no args is the "choose
// the scan root" flow (its old behaviour); pass {onChoose} to reuse the
// same overlay to pick any other directory (e.g. the Upscale output dir),
// with the callback getting the chosen absolute path.
let pickerChoose = defaultRootChoose;
let pickerCanCancel = true;

function openPicker(opts) {
  opts = opts || {};
  pickerChoose = opts.onChoose || defaultRootChoose;
  // the root flow can't be cancelled until there's a root to fall back on;
  // any other use is always cancellable
  pickerCanCancel = opts.onChoose ? true : !!currentRoot;
  document.getElementById('picker-title').textContent = opts.title || 'Choose the image directory to scan';
  document.getElementById('picker-use').textContent = opts.useLabel || 'Use this directory';
  // "New folder" only makes sense when picking a destination, not a
  // directory to scan - opt in via allowCreate.
  document.getElementById('picker-newrow').hidden = !opts.allowCreate;
  document.getElementById('picker-newname').value = '';
  document.getElementById('picker-overlay').classList.add('open');
  browseTo(opts.startAt || currentRoot || null);
}
function closePicker() {
  document.getElementById('picker-overlay').classList.remove('open');
}

async function defaultRootChoose(chosen) {
  const r = await fetch('/api/set-root', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path: chosen})
  });
  const started = await r.json();
  if (!started.ok) { alert(started.error || 'could not start scan'); return; }
  startAllTabLoads();  // not awaited - all four Operations tabs load in
                        // the background (each with its own spinner) while
                        // the rest of the UI becomes usable immediately
  // state.root is set essentially the instant the scan job starts, but
  // there's a small window before that background thread actually runs -
  // wait (briefly, bounded) for it to land so tabs don't flash "no
  // directory chosen" before catching up on their own.
  for (let i = 0; i < 20; i++) {
    const s = await apiState();
    if (s.root) break;
    await new Promise(res => setTimeout(res, 50));
  }
  await reloadActive();
}

document.getElementById('change-dir').onclick = () => openPicker();
document.getElementById('picker-cancel').onclick = () => { if (pickerCanCancel) closePicker(); };
document.getElementById('picker-use').onclick = async () => {
  if (!browsePath) return;
  const chosen = browsePath;
  closePicker();
  await pickerChoose(chosen);
};

async function pickerCreateFolder() {
  const name = document.getElementById('picker-newname').value.trim();
  if (!name || !browsePath) return;
  const r = await fetch('/api/mkdir', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({parent: browsePath, name}),
  });
  const data = await r.json();
  if (!data.ok) { document.getElementById('picker-error').textContent = data.error || 'could not create folder'; return; }
  document.getElementById('picker-newname').value = '';
  browseTo(data.path);  // navigate into it - it's now the current directory
}
document.getElementById('picker-newbtn').onclick = pickerCreateFolder;
document.getElementById('picker-newname').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') { e.preventDefault(); pickerCreateFolder(); }
});

// ---------- shared: confirmation modal ----------

function confirmAction(title, bodyHtml, onConfirm, opts) {
  opts = opts || {};
  document.getElementById('confirm-title').textContent = title;
  document.getElementById('confirm-body').innerHTML = bodyHtml;
  document.getElementById('confirm-overlay').classList.add('open');
  const go = document.getElementById('confirm-go');
  const cancel = document.getElementById('confirm-cancel');
  go.textContent = opts.confirmLabel || 'Confirm';
  const cleanup = () => {
    document.getElementById('confirm-overlay').classList.remove('open');
    go.onclick = null; cancel.onclick = null; go.disabled = false; go.textContent = 'Confirm';
  };
  if (opts.requireCheckbox) {
    go.disabled = true;
    const cb = document.getElementById(opts.requireCheckbox);
    if (cb) cb.onchange = (e) => { go.disabled = !e.target.checked; };
  }
  go.onclick = async () => { cleanup(); await onConfirm(); };
  cancel.onclick = cleanup;
}

// ---------- top-level tabs ----------

// Leaving the Visually Similar sub-tab while it's enabled for the next
// run but nothing has actually been reviewed yet (nothing staged to
// discard) is very likely a mistake - it would run as a no-op operation.
// Intercept that specific case with a choice: go back and review, or
// explicitly ignore (which un-enables it, since there's genuinely
// nothing for it to do). Any other navigation is unaffected.
function isLeavingVisualEmptyHanded(newTab, newSubtab) {
  const onVisualNow = activeTab === 'operations' && activeSubtab === 'visual';
  if (!onVisualNow) return false;
  const stayingOnVisual = newTab === 'operations' && (newSubtab === undefined || newSubtab === 'visual');
  if (stayingOnVisual) return false;
  return pendingOps.visual && ndDiscardTotal() === 0;
}

function showLeaveVisualWarning(onIgnore) {
  document.getElementById('confirm-title').textContent = 'No images have been chosen for de-duping!';
  document.getElementById('confirm-body').innerHTML =
    `<p>Visually Similar is enabled for the next run, but nothing has been reviewed yet - there's nothing for it to de-duplicate.</p>`;
  const go = document.getElementById('confirm-go');
  const cancel = document.getElementById('confirm-cancel');
  go.className = 'btn btn-accent';
  go.textContent = 'Review';
  go.disabled = false;
  cancel.className = 'btn';
  cancel.textContent = 'Ignore';
  document.getElementById('confirm-overlay').classList.add('open');
  const cleanup = () => {
    document.getElementById('confirm-overlay').classList.remove('open');
    go.onclick = null; cancel.onclick = null;
    // restore confirmAction()'s defaults for its own later use
    go.className = 'btn btn-danger'; go.textContent = 'Confirm';
    cancel.className = 'btn'; cancel.textContent = 'Cancel';
  };
  go.onclick = () => { cleanup(); };  // "Review" just stays put - the navigation never happened
  cancel.onclick = () => {
    cleanup();
    pendingOps.visual = false;
    onIgnore();
  };
}

document.querySelectorAll('#tabs button').forEach(btn => {
  btn.onclick = () => {
    const doNav = () => {
      document.querySelectorAll('#tabs button').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tabpanel').forEach(p => p.classList.remove('active'));
      btn.classList.add('active');
      activeTab = btn.dataset.tab;
      document.getElementById('tab-' + activeTab).classList.add('active');
      document.getElementById('subtabs').style.display = activeTab === 'operations' ? 'flex' : 'none';
      reloadActive();
    };
    if (isLeavingVisualEmptyHanded(btn.dataset.tab, undefined)) showLeaveVisualWarning(doNav);
    else doNav();
  };
});
document.querySelectorAll('#subtabs button').forEach(btn => {
  btn.onclick = () => {
    const doNav = () => {
      document.querySelectorAll('#subtabs button').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.subpanel').forEach(p => p.style.display = 'none');
      btn.classList.add('active');
      activeSubtab = btn.dataset.subtab;
      document.getElementById('sub-' + activeSubtab).style.display = 'block';
      reloadActive();
    };
    if (isLeavingVisualEmptyHanded('operations', btn.dataset.subtab)) showLeaveVisualWarning(doNav);
    else doNav();
  };
});

// Always re-verifies against the live server before deciding whether a
// directory is chosen, rather than trusting a client-side flag that could
// go stale - if the header ever disagrees with what a tab shows, this is
// the fix: every tab switch re-derives state from /api/state fresh.
async function reloadActive() {
  await refreshRootLabel();
  if (!currentRoot) {
    // NOTE: #tab-operations is a .tabpanel that is also the *parent* of the
    // three .subpanel divs - overwriting its innerHTML would destroy those
    // children permanently (any later getElementById('sub-identical') etc.
    // would return null forever after). So this only ever touches the leaf
    // containers, never the parent wrapper.
    const msg = '<div class="empty">No directory chosen — click "Change" above.</div>';
    document.querySelectorAll('.subpanel').forEach(p => p.innerHTML = msg);
    document.getElementById('tab-jobs').innerHTML = msg;
    document.getElementById('tab-quarantine').innerHTML = msg;
    document.getElementById('jobs-badge').style.display = 'none';
    document.getElementById('quarantine-badge').style.display = 'none';
    return;
  }
  if (activeTab === 'operations') {
    // Re-render from what's already cached (from startAllTabLoads's
    // proactive background load, or an earlier visit) instead of always
    // re-fetching - identical/normalise recompute their whole plan on
    // every fetch (real filesystem work, with server-side console output
    // for a large tree), so re-fetching on every plain tab click meant
    // real rescanning and the whole table flashing/repopulating each
    // time you switched back to it. Explicit refresh triggers (Rescan,
    // the prefer/rename-conflicts options, a completed Run) call
    // loadIdentical()/loadNormalise() directly and still always fetch.
    if (activeSubtab === 'identical') { identicalData ? renderIdentical(identicalData) : loadIdentical(); }
    if (activeSubtab === 'visual') { if (!tabLoading.visual) { visualLoaded ? renderVisual() : loadVisual(); } }
    if (activeSubtab === 'normalise') { normaliseData ? renderNormalise(normaliseData) : loadNormalise(); }
    if (activeSubtab === 'upscale') { upscaleData ? renderUpscale(upscaleData) : loadUpscale(); }
  }
  if (activeTab === 'jobs') loadJobs();
  if (activeTab === 'quarantine') loadQuarantine();
}

// ---------- pending-jobs toggle shared by all three Operations tabs ----------

let pendingOps = {identical: false, normalise: false, visual: false};

function pendingToggleHtml(key, disabled, note) {
  const checked = pendingOps[key] && !disabled;
  return `<div class="pending-toggle ${disabled ? 'disabled' : ''}">
    <input type="checkbox" id="pending-${key}" ${checked ? 'checked' : ''} ${disabled ? 'disabled' : ''}>
    <label for="pending-${key}">Add to Pending Jobs</label>
    <span class="pending-note">${note}</span>
  </div>`;
}
function wirePendingToggle(key) {
  const cb = document.getElementById('pending-' + key);
  if (!cb) return;
  cb.onchange = () => {
    pendingOps[key] = cb.checked;
    updateJobsBadge();
    if (activeTab === 'jobs') loadJobs();
  };
}

// Jobs badge count is derived entirely client-side from each tab's
// already-fetched preview data (identicalData/normaliseData/ndDecisions) -
// no extra server round-trip, so it can update instantly on every toggle
// or decision. It's an estimate (matches what a real build would count,
// assuming the filesystem hasn't changed since each tab last loaded) -
// visiting the Jobs tab still runs the authoritative build.
function updateJobsBadge() {
  let n = 0;
  if (pendingOps.identical && identicalData) n += identicalData.total_files;
  if (pendingOps.normalise && normaliseData) n += normaliseData.actions.length;
  if (pendingOps.visual) n += ndDiscardTotal();
  const badge = document.getElementById('jobs-badge');
  if (n > 0) { badge.textContent = n; badge.style.display = 'inline-block'; }
  else { badge.style.display = 'none'; }
}

async function refreshQuarantineBadge() {
  try {
    const r = await fetch('/api/quarantine/status');
    const data = await r.json();
    const badge = document.getElementById('quarantine-badge');
    if (data.exists && data.file_count > 0) { badge.textContent = data.file_count; badge.style.display = 'inline-block'; }
    else { badge.style.display = 'none'; }
    return data;
  } catch (e) {
    return null;  // called both fire-and-forget (startAllTabLoads) and for its
                  // return value (loadQuarantine) - the latter handles null itself
  }
}

// ---------- Visually Similar sub-tab ----------

let ndGroups = [], ndDecisions = {}, ndIdx = 0;
let ndPending = {};
let ndTouched = false;  // true once the current group's keep/discard has been changed since it was shown
let visualLoaded = false;  // true once loadVisualData() has fetched at least once this scan - lets reloadActive() re-render from memory on a plain revisit instead of re-fetching

function ndPositionToFirstUndecided() {
  ndIdx = ndGroups.findIndex(g => !ndDecisions[g.id]);
  if (ndIdx === -1) ndIdx = 0;
}

// Fetches the current groups/decisions and positions on the first
// undecided one - shared by loadVisual() (a manual tab visit) and
// pollScan() (the moment the scan's hash/group phase ends and there's
// real content to show for the first time).
async function loadVisualData() {
  try {
    const r = await fetch('/api/nd/groups');
    const data = await r.json();
    ndGroups = data.groups;
    ndDecisions = data.decisions;
    ndPositionToFirstUndecided();
    visualLoaded = true;
    if (activeTab === 'operations' && activeSubtab === 'visual') renderVisual();
  } catch (e) {
    // Don't rethrow: this is also called from pollScan()'s loop, which
    // should keep polling scan status even if this one fetch failed - a
    // later tick calling refreshVisualGroupsQuietly() can still recover.
    if (activeTab === 'operations' && activeSubtab === 'visual') {
      document.getElementById('sub-visual').innerHTML = loadErrorHtml('Visually Similar', e, 'visual-retry');
      document.getElementById('visual-retry').onclick = loadVisualData;
    }
  }
}

async function loadVisual() {
  if (tabLoading.visual) return;  // still in the scan's hash/group phase - pollScan will render once ready
  const panel = document.getElementById('sub-visual');
  panel.innerHTML = bigSpinnerHtml('Loading');
  await loadVisualData();
}

function ndFirstUndecidedFrom(start) {
  for (let i = start; i < ndGroups.length; i++) if (!ndDecisions[ndGroups[i].id]) return i;
  for (let i = 0; i < ndGroups.length; i++) if (!ndDecisions[ndGroups[i].id]) return i;
  return -1;
}

function ndStartPending(g) {
  const existing = ndDecisions[g.id];
  ndPending = {};
  ndTouched = false;
  g.images.forEach(im => {
    ndPending[im.path] = existing ? existing.discard.includes(im.path) ? 'discard' : 'keep' : 'keep';
  });
}

// Saves the current group's keep/discard split - called on every
// prev/next navigation (buttons and arrow keys alike), unconditionally,
// not just when something was toggled. There used to be a separate
// "Confirm & next" button and this only fired on an actual toggle
// (ndTouched), so simply browsing to a group and moving on without
// touching a card left it permanently undecided - excluded from
// ndDecisions and therefore never applied, even though nothing looked
// wrong in the UI. Now prev/next always commit the current keep/discard
// split (even an all-kept "nothing to discard here" one), exactly like
// the old Confirm button did, so every group you pass through ends up
// recorded.
async function ndSaveCurrent() {
  const g = ndGroups[ndIdx];
  if (!g) return;
  const keep = g.images.map(im => im.path).filter(p => ndPending[p] !== 'discard');
  const discard = g.images.map(im => im.path).filter(p => ndPending[p] === 'discard');
  await ndSaveDecision(g.id, keep, discard, false);
  ndTouched = false;
}

function ndDiscardTotal() {
  return Object.values(ndDecisions).reduce((n,d) => n + (d.skipped?0:d.discard.length), 0);
}

function ndResetRowHtml(decidedCount) {
  if (decidedCount === 0) return '';
  return `<div class="toolbar"><button class="btn btn-danger btn-sm" id="nd-reset">Reset all decisions</button>
    <span class="spacer-note">${plural(decidedCount, 'group')} decided so far</span></div>`;
}
function wireNdReset() {
  const btn = document.getElementById('nd-reset');
  if (!btn) return;
  btn.onclick = () => {
    const decidedCount = Object.keys(ndDecisions).length;
    confirmAction('Reset all decisions',
      `<p>This discards all ${plural(decidedCount, 'decision')} you've made in Visually Similar and starts review from the beginning. ` +
      `Only your review progress is affected - no files are touched.</p>`,
      async () => {
        const r = await fetch('/api/nd/reset', { method: 'POST' });
        const result = await r.json();
        if (!result.ok) { alert(result.error || 'reset failed'); return; }
        ndDecisions = {};
        ndIdx = 0;
        pendingOps.visual = false;
        updateJobsBadge();
        loadVisual();
      },
      { confirmLabel: 'Reset' });
  };
}

// Re-walks the tree from scratch - picks up anything quarantined, deleted
// or added since the last scan. Disabled while a scan (this one or the
// initial one) is already running, since the backend only allows one at a
// time anyway; disabling avoids a pointless "already running" error alert.
function visualRescanRowHtml() {
  return `<div class="toolbar"><button class="btn" id="visual-rescan" ${scanActive ? 'disabled' : ''}>Rescan</button></div>`;
}
function wireVisualRescan() {
  document.getElementById('visual-rescan').onclick = async () => {
    const r = await fetch('/api/nd/rescan', {method: 'POST'});
    const result = await r.json();
    if (!result.ok) { alert(result.error || 'rescan failed'); return; }
    setTabLoading('visual', true, 'Hashing images', 0);
    pollScan();
  };
}

function renderVisual() {
  const panel = document.getElementById('sub-visual');
  const discardCount = ndDiscardTotal();
  const noGroups = ndGroups.length === 0;
  // enabling doesn't require having reviewed anything yet - navigating
  // away while enabled with nothing staged is caught separately, by
  // isLeavingVisualEmptyHanded()/showLeaveVisualWarning()
  const toggle = pendingToggleHtml('visual', noGroups,
    noGroups ? 'Nothing to review' : discardCount === 0 ? 'No decisions yet' : `${plural(discardCount, 'file')} staged to discard`);
  const rescanRow = visualRescanRowHtml();

  if (ndGroups.length === 0) {
    const emptyMsg = scanActive
      ? 'Still scanning for candidate groups&hellip; hang tight, groups will appear here as they\'re scored.'
      : 'No candidate groups found in this directory.';
    panel.innerHTML = `<h2 class="page-title">Visually Similar</h2>${toggle}${rescanRow}<div class="empty">${emptyMsg}</div>`;
    wirePendingToggle('visual');
    wireVisualRescan();
    return;
  }
  const decidedCount = Object.keys(ndDecisions).length;
  const resetRow = ndResetRowHtml(decidedCount);
  if (decidedCount === ndGroups.length) {
    panel.innerHTML = `<h2 class="page-title">Visually Similar</h2>${toggle}${rescanRow}${resetRow}<div class="empty">All groups reviewed! ${plural(discardCount, 'file')} staged to discard.<br><span class="empty-hint">Add to Pending Jobs above, then go to the Jobs tab to run it.</span></div>`;
    wirePendingToggle('visual');
    wireVisualRescan();
    wireNdReset();
    return;
  }
  const g = ndGroups[ndIdx];
  if (!g) return;
  ndStartPending(g);

  const groupKeepCount = g.images.filter(im => ndPending[im.path] !== 'discard').length;
  const groupDiscardCount = g.images.length - groupKeepCount;
  let html = `<h2 class="page-title">Visually Similar</h2>${toggle}${rescanRow}${resetRow}
    <div id="progress-track"><div class="bar"><div class="bar-fill" style="width:${100*decidedCount/ndGroups.length}%"></div></div>
    <div id="progress-label">${decidedCount} / ${ndGroups.length} decided &mdash; viewing group ${ndIdx+1}</div></div>
    <div class="nd-stats">
      <span class="nd-stat"><b>${g.images.length}</b> Images</span>
      <span class="nd-stat">Image Group <b>${ndIdx+1}</b>/<b>${ndGroups.length}</b></span>
      <span class="nd-stat">Keeping <b id="nd-keep-count">${groupKeepCount}</b> / Discarding <b id="nd-discard-count">${groupDiscardCount}</b></span>
    </div>
    <div id="cards-wrap">
      <button class="carousel-arrow" id="cards-prev" aria-label="Scroll left">&lsaquo;</button>
      <div id="cards">`;
  g.images.forEach((im, i) => {
    // reflect ndPending (already reconstructed from any saved decision by
    // ndStartPending above) rather than assuming every card starts as
    // "keep" - otherwise returning to a group you'd already toggled shows
    // everything as kept again even though the saved decision is correct
    const isKeep = ndPending[im.path] !== 'discard';
    html += `<div class="card ${isKeep ? 'keep' : 'discard'}" data-path="${esc(im.path)}">
      <img src="/img/${encodeURIComponent(im.path)}" loading="lazy" draggable="false">
      <div class="cap">
        <span class="key">${i+1}</span>
        <span class="path">${esc(im.path)}</span>${im.suggested ? '<span class="badge">suggested</span>' : ''}
        <span class="state">${isKeep ? 'KEEP' : 'DISCARD'}</span><br>
        ${im.width}&times;${im.height} &middot; sharpness ${im.sharpness} &middot; ${im.size_human}
      </div>
    </div>`;
  });
  html += `</div>
      <button class="carousel-arrow" id="cards-next" aria-label="Scroll right">&rsaquo;</button>
    </div>
    <footer class="nd-footer">
      <button class="btn" id="nd-prev">&larr; prev</button>
      <button class="btn btn-primary" id="nd-next">next &rarr; (Enter)</button>
      <button class="btn" id="nd-skip">Skip (S)</button>
      <span class="hint">Click an image, or press its number, to toggle keep/discard. More than one can be kept.</span>
    </footer>`;
  panel.innerHTML = html;
  wirePendingToggle('visual');
  wireVisualRescan();
  wireNdReset();

  panel.querySelectorAll('.card').forEach(card => {
    // For mouse, wireCardsDrag's pointerup handler is what actually
    // toggles a plain click (see the note there) - this listener mainly
    // covers touch/pen taps, which never go through that mouse-only
    // pointer handling at all. Both guard flags are consumed (reset)
    // immediately on use, suppressing exactly the one click they're
    // about, rather than left set until the next *mouse* pointerdown -
    // on a touchscreen device mixing mouse and touch, a stale flag left
    // over from an earlier mouse drag/click would otherwise go on wrongly
    // suppressing an unrelated later touch tap indefinitely.
    card.addEventListener('click', () => {
      if (cardsJustToggledViaPointer) { cardsJustToggledViaPointer = false; return; }
      if (cardsDragMoved) { cardsDragMoved = false; return; }
      ndToggle(card.dataset.path);
    });
  });
  document.getElementById('nd-skip').onclick = ndSkip;
  document.getElementById('nd-prev').onclick = async () => { await ndSaveCurrent(); ndIdx = Math.max(0, ndIdx - 1); renderVisual(); };
  document.getElementById('nd-next').onclick = async () => { await ndSaveCurrent(); ndIdx = Math.min(ndGroups.length - 1, ndIdx + 1); renderVisual(); };
  wireCardsCarousel();
}

// ---------- horizontal carousel for a group's images (#cards) ----------
//
// Images used to wrap onto new rows once a group had more than fit
// side-by-side, forcing a vertical scroll to see the rest. Instead #cards
// is a single non-wrapping row that scrolls horizontally (native smooth
// scrolling + snap points), with flanking arrow buttons that page through
// it - hidden/disabled once there's nothing left in that direction.
function scrollCarousel(dir) {
  const track = document.getElementById('cards');
  if (!track) return;
  stopMomentum(track);  // an in-flight momentum scroll would otherwise keep
                         // fighting this smooth scrollBy over the same scrollLeft
  const card = track.querySelector('.card');
  const step = card ? card.getBoundingClientRect().width + 16 : track.clientWidth * 0.8;
  track.scrollBy({ left: dir * step, behavior: 'smooth' });
}

function updateCarouselArrows() {
  const track = document.getElementById('cards');
  const prev = document.getElementById('cards-prev');
  const next = document.getElementById('cards-next');
  if (!track || !prev || !next) return;
  const maxScroll = track.scrollWidth - track.clientWidth;
  const hasOverflow = maxScroll > 4;
  prev.classList.toggle('hidden', !hasOverflow);
  next.classList.toggle('hidden', !hasOverflow);
  if (!hasOverflow) return;
  prev.disabled = track.scrollLeft <= 4;
  next.disabled = track.scrollLeft >= maxScroll - 4;
}

function wireCardsCarousel() {
  const track = document.getElementById('cards');
  const prev = document.getElementById('cards-prev');
  const next = document.getElementById('cards-next');
  if (!track || !prev || !next) return;
  prev.onclick = () => scrollCarousel(-1);
  next.onclick = () => scrollCarousel(1);
  let ticking = false;
  track.addEventListener('scroll', () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => { updateCarouselArrows(); ticking = false; });
  });
  // images are loading="lazy" and have no explicit width/height, so
  // scrollWidth can change (and arrows need re-checking) as each one loads
  track.querySelectorAll('img').forEach(img => img.addEventListener('load', updateCarouselArrows));
  requestAnimationFrame(updateCarouselArrows);
  wireCardsDrag(track);
}

// Click-and-drag scrolling for #cards (mouse only - touch already gets
// this for free from native overflow-x:auto). cardsDragMoved is read by
// the .card click handlers above to swallow the click a drag ends on, so
// releasing a drag over a card never also toggles it.
let cardsDragMoved = false;
// set true right after a plain (non-drag) mouse press-release toggles its
// card directly via pointerup - tells the .card click listener to skip
// its own toggle if 'click' also fires for that same press, so it can
// never double-toggle. Reset on the next pointerdown either way.
let cardsJustToggledViaPointer = false;

function wireCardsDrag(track) {
  const DRAG_THRESHOLD = 6;  // px of movement before a press counts as a drag, not a click
  let dragging = false;
  let startX = 0, startScrollLeft = 0;
  let samples = [];  // trailing {x, t} window, for velocity at release
  let pressedCard = null;  // the .card actually pressed on, captured at pointerdown - see the note in pointerup below

  // Images (and any future draggable descendant) must not start a native
  // HTML5 drag - that's what was actually breaking this: <img> is
  // draggable by default in Chrome/Firefox (the -webkit-user-drag CSS
  // property that was here before is Safari-only and does nothing in
  // either of those), so pressing down on an image and moving handed the
  // gesture to the browser's own drag-and-drop instead of our pointermove
  // tracking below - two separate things fighting over scrollLeft at
  // once, which is exactly the "dragging both directions" glitch. Belt
  // and suspenders alongside draggable="false" on the <img> tag itself.
  track.addEventListener('dragstart', (e) => e.preventDefault());

  track.addEventListener('pointerdown', (e) => {
    if (e.pointerType !== 'mouse' || e.button !== 0) return;
    stopMomentum(track);
    // NOTE: deliberately no e.preventDefault() here. It looks like the
    // obvious way to also stop native text selection from starting, but
    // in real Chrome (not reproducible in jsdom, which is how this got
    // missed) calling preventDefault() on pointerdown suppresses the
    // browser's *click* event entirely for that press - it broke
    // selecting images outright, not just post-drag clicks. #cards has
    // user-select:none in CSS already, which is what actually stops text
    // selection, with no such side effect.
    // #cards has scroll-behavior:smooth in CSS (for arrow-button paging),
    // but setting scrollLeft directly still respects that - every
    // pointermove below would otherwise animate/lag instead of tracking
    // the cursor 1:1. Override to instant for the duration of the
    // drag+momentum, then hand control back to the CSS value afterward.
    track.style.scrollBehavior = 'auto';
    dragging = true;
    cardsDragMoved = false;
    cardsJustToggledViaPointer = false;
    pressedCard = e.target.closest('.card');
    startX = e.clientX;
    startScrollLeft = track.scrollLeft;
    samples = [{ x: e.clientX, t: performance.now() }];
    track.setPointerCapture(e.pointerId);
  });

  track.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    const dx = e.clientX - startX;
    if (!cardsDragMoved && Math.abs(dx) < DRAG_THRESHOLD) return;
    if (!cardsDragMoved) track.classList.add('dragging');
    cardsDragMoved = true;
    track.scrollLeft = startScrollLeft - dx;
    const now = performance.now();
    samples.push({ x: e.clientX, t: now });
    // Trailing *time* window, not a fixed sample count - a press-then-
    // pause-then-flick gesture would otherwise dilute the release
    // velocity with that dwell time (samples.length rarely gets past a
    // handful of entries anyway, so this was effectively including
    // everything since the initial press), making a real flick look slow
    // and killing momentum. Keeping only the last ~100ms means release
    // velocity reflects how fast the cursor was *actually* moving just
    // before letting go.
    while (samples.length > 1 && now - samples[0].t > 100) samples.shift();
    e.preventDefault();
  });

  // Returns true if this was a real drag, false if it was a plain
  // press-and-release (no drag), or undefined if there was nothing to end.
  function endDrag(e) {
    if (!dragging) return undefined;
    dragging = false;
    track.classList.remove('dragging');
    try { track.releasePointerCapture(e.pointerId); } catch (_) { /* already released */ }
    if (!cardsDragMoved) { track.style.scrollBehavior = ''; return false; }
    const first = samples[0], last = samples[samples.length - 1];
    const dt = last.t - first.t;
    let velocity = dt > 0 ? (last.x - first.x) / dt : 0;  // px/ms, same sign convention as dx above
    const MAX_VELOCITY = 3;  // sanity clamp against a noisy single-frame spike
    velocity = Math.max(-MAX_VELOCITY, Math.min(MAX_VELOCITY, velocity));
    startMomentum(track, velocity);
    return true;
  }
  track.addEventListener('pointerup', (e) => {
    const wasDrag = endDrag(e);
    if (wasDrag === false && pressedCard) {
      // Toggle directly here rather than relying on the native 'click'
      // event that would normally follow: once setPointerCapture has been
      // engaged for a gesture, real browsers don't reliably target the
      // resulting click at the element actually under the pointer (this
      // is browser-specific behavior jsdom doesn't reproduce, which is
      // how the previous fix here looked right but still left clicking
      // broken). Acting on the pointer sequence itself sidesteps that
      // entirely. cardsJustToggledViaPointer tells the .card click
      // listener to skip its own toggle if 'click' does still fire for
      // this press, so it can never double-toggle.
      ndToggle(pressedCard.dataset.path);
      cardsJustToggledViaPointer = true;
    }
  });
  track.addEventListener('pointercancel', endDrag);
}

function stopMomentum(track) {
  if (track.__momentumRAF) {
    cancelAnimationFrame(track.__momentumRAF);
    track.__momentumRAF = null;
  }
}

// Keeps scrolling after release, in the direction and rough speed the
// drag was moving, decaying via friction each frame until it's
// imperceptibly slow or the track runs out of room to scroll further.
function startMomentum(track, velocity) {
  if (Math.abs(velocity) < 0.05) { track.style.scrollBehavior = ''; return; }
  const friction = 0.94;  // per ~16.7ms frame
  let v = velocity;
  let lastT = performance.now();
  function step(now) {
    const dt = Math.min(now - lastT, 48);  // clamp so a stalled tab doesn't jump on return
    lastT = now;
    track.scrollLeft -= v * dt;
    v *= Math.pow(friction, dt / 16.67);
    const maxScroll = track.scrollWidth - track.clientWidth;
    const atBounds = track.scrollLeft <= 0 || track.scrollLeft >= maxScroll;
    if (Math.abs(v) < 0.02 || atBounds) {
      track.__momentumRAF = null;
      track.style.scrollBehavior = '';  // hand back to CSS's smooth, for arrow-button paging
      return;
    }
    track.__momentumRAF = requestAnimationFrame(step);
  }
  track.__momentumRAF = requestAnimationFrame(step);
}
// registered once (not per-render, unlike the listeners above which live
// on elements that get thrown away and re-created every render) - safe to
// call even when #cards doesn't currently exist, it just no-ops
window.addEventListener('resize', updateCarouselArrows);

function ndToggle(path) {
  ndPending[path] = ndPending[path] === 'keep' ? 'discard' : 'keep';
  ndTouched = true;
  const card = document.querySelector(`#sub-visual .card[data-path="${CSS.escape(path)}"]`);
  const isKeep = ndPending[path] === 'keep';
  card.classList.toggle('keep', isKeep);
  card.classList.toggle('discard', !isKeep);
  card.querySelector('.state').textContent = isKeep ? 'KEEP' : 'DISCARD';
  ndUpdateStatsCounts();
  // number-key toggles can target a card scrolled out of the carousel's
  // view - bring it into view so the state change is actually visible.
  // A no-op (no scrolling) if it's already fully in view.
  card.scrollIntoView({ behavior: 'smooth', inline: 'nearest', block: 'nearest' });
}
// Keeps the "Keeping X / Discarding X" stat live as cards are toggled,
// without a full renderVisual() re-render (which would rebuild the whole
// carousel and lose scroll position mid-toggle).
function ndUpdateStatsCounts() {
  const g = ndGroups[ndIdx];
  if (!g) return;
  const keepEl = document.getElementById('nd-keep-count');
  const discardEl = document.getElementById('nd-discard-count');
  if (!keepEl || !discardEl) return;
  const keepCount = g.images.filter(im => ndPending[im.path] !== 'discard').length;
  keepEl.textContent = keepCount;
  discardEl.textContent = g.images.length - keepCount;
}

async function ndSaveDecision(gid, keep, discard, skipped) {
  ndDecisions[gid] = {keep, discard, skipped, decided_at: new Date().toISOString()};
  updateJobsBadge();
  await fetch('/api/nd/decide', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({group_id: gid, keep, discard, skip: skipped})
  });
}

async function ndSkip() {
  const g = ndGroups[ndIdx];
  if (!g) return;
  await ndSaveDecision(g.id, [], [], true);
  ndIdx = ndFirstUndecidedFrom(ndIdx + 1);
  if (ndIdx === -1) ndIdx = ndGroups.length;
  renderVisual();
}

window.addEventListener('keydown', async (e) => {
  if (!(activeTab === 'operations' && activeSubtab === 'visual')) return;
  if (document.getElementById('picker-overlay').classList.contains('open')) return;
  if (document.getElementById('confirm-overlay').classList.contains('open')) return;
  const g = ndGroups[ndIdx];
  if (!g) return;
  if (e.key === 's' || e.key === 'S') { ndSkip(); return; }
  // Enter and ArrowRight both act like the "next" button (save + advance);
  // ArrowLeft acts like "prev". All three always save the current
  // keep/discard split first, same as clicking next/prev - see
  // ndSaveCurrent().
  if (e.key === 'Enter' || e.key === 'ArrowRight') { await ndSaveCurrent(); ndIdx = Math.min(ndGroups.length - 1, ndIdx + 1); renderVisual(); return; }
  if (e.key === 'ArrowLeft') { await ndSaveCurrent(); ndIdx = Math.max(0, ndIdx - 1); renderVisual(); return; }
  const n = parseInt(e.key, 10);
  if (!isNaN(n) && n >= 1 && g.images[n-1]) ndToggle(g.images[n-1].path);
});

// ---------- Identical Files sub-tab (preview only) ----------

let identicalPrefer = 'oldest';
let identicalData = null;  // cached, used for the Jobs badge without a round-trip
let identicalSortCol = null;  // null = natural (server) order; 'duplicates' | 'path'
let identicalSortDir = 'asc';

async function loadIdentical() {
  if (tabLoading.identical) return;  // already in flight (e.g. from startAllTabLoads) - avoid an overlapping duplicate fetch
  setTabLoading('identical', true, 'Scanning');
  try {
    const r = await fetch(`/api/identical/preview?prefer=${identicalPrefer}`);
    const data = await r.json();
    identicalData = data;
    setTabLoading('identical', false);
    updateJobsBadge();
    renderIdentical(data);
  } catch (e) {
    setTabLoading('identical', false);
    document.getElementById('sub-identical').innerHTML = loadErrorHtml('Identical Files', e, 'identical-retry');
    document.getElementById('identical-retry').onclick = loadIdentical;
  }
}

function renderIdentical(data) {
  const panel = document.getElementById('sub-identical');
  const toggle = pendingToggleHtml('identical', data.total_files === 0,
    data.total_files === 0 ? 'Nothing found' : `${plural(data.total_files, 'file')} to be added`);
  let html = `<h2 class="page-title">Identical Files</h2>
    <p class="page-sub">Byte-for-byte duplicates, matched by SHA-256 content hash.</p>${toggle}
    <div class="toolbar">
    <label>Keep: <select id="identical-prefer">
      <option value="oldest">oldest file</option>
      <option value="newest">newest file</option>
      <option value="shortest-path">shortest path</option>
      <option value="longest-path">longest path</option>
    </select></label>
    <button class="btn" id="identical-rescan">Rescan</button>
  </div>`;
  if (data.groups.length === 0) {
    html += '<div class="empty">No identical files found.</div>';
    panel.innerHTML = html;
    document.getElementById('identical-prefer').value = identicalPrefer;
    document.getElementById('identical-prefer').onchange = (e) => { identicalPrefer = e.target.value; loadIdentical(); };
    document.getElementById('identical-rescan').onclick = loadIdentical;
    wirePendingToggle('identical');
    return;
  }
  html += `<div class="summary">${plural(data.groups.length, 'duplicate group')}, ${plural(data.total_files, 'file')} would be quarantined, reclaiming ${data.total_size_human}.</div>`;

  const groups = data.groups.slice();
  if (identicalSortCol) {
    groups.sort((a, b) => {
      const cmp = identicalSortCol === 'duplicates'
        ? a.move.length - b.move.length
        : a.keep.toLowerCase().localeCompare(b.keep.toLowerCase());
      return identicalSortDir === 'asc' ? cmp : -cmp;
    });
  }
  const idArrow = col => identicalSortCol === col ? (identicalSortDir === 'asc' ? ' ▲' : ' ▼') : '';
  html += `<div class="kept-files-header">
      <span class="sortable dup-header-cell" data-sort="duplicates">Duplicates${idArrow('duplicates')}</span>
      <span></span>
      <span class="sortable" data-sort="path">Path${idArrow('path')}</span>
      <span></span>
    </div>
    <div class="kept-files-list">`;
  groups.forEach((g, i) => {
    html += `<div class="kept-row-wrap">
      <div class="kept-row" data-idx="${i}">
        <span class="dup-count-cell">${g.move.length}</span>
        ${thumbHtml(g.keep, false)}
        <div class="info"><div class="path">${esc(g.keep)}</div><div class="meta">${esc(g.keep_size_human)}</div></div>
        <span class="expand-arrow">&#9656;</span>
      </div>
      <div class="dup-detail"><div class="dup-detail-inner">
        <div class="dup-plain-list">
          ${g.move.map(m => `<div class="dup-plain-row">
            ${thumbHtml(m.path, false)}
            <div class="info"><div class="path">${esc(m.path)}</div><div class="meta">${esc(m.size_human)}</div></div>
          </div>`).join('')}
        </div>
      </div></div>
    </div>`;
  });
  html += '</div>';
  panel.innerHTML = html;
  document.getElementById('identical-prefer').value = identicalPrefer;
  document.getElementById('identical-prefer').onchange = (e) => { identicalPrefer = e.target.value; loadIdentical(); };
  document.getElementById('identical-rescan').onclick = loadIdentical;
  wirePendingToggle('identical');

  panel.querySelectorAll('.kept-row').forEach(row => {
    row.onclick = () => row.closest('.kept-row-wrap').classList.toggle('expanded');
  });
  panel.querySelectorAll('.kept-files-header .sortable').forEach(span => {
    span.onclick = () => {
      const col = span.dataset.sort;
      if (identicalSortCol === col) identicalSortDir = identicalSortDir === 'asc' ? 'desc' : 'asc';
      else { identicalSortCol = col; identicalSortDir = 'asc'; }
      renderIdentical(data);
    };
  });
}

// ---------- Normalisation sub-tab (directory merge + lowercase, preview only) ----------

let normaliseRenameConflicts = false;
let normaliseCaseStyle = 'lower';    // 'none' | 'lower' | 'camel'
let normaliseSepStyle = 'none';      // 'none' | 'spaces-dash' | 'spaces-underscore' | 'dash-underscore' | 'underscore-dash'
let normaliseTypeFilter = 'all';     // 'all' | 'file' | 'dir'
let normaliseSortCol = 'type';       // 'type' | 'current' | 'normalised'
let normaliseSortDir = 'asc';        // 'asc' | 'desc' - default (type, asc) puts "Directory" above "File" for free, alphabetically
let normaliseData = null;  // cached, used for the Jobs badge without a round-trip

async function loadNormalise() {
  if (tabLoading.normalise) return;  // already in flight (e.g. from startAllTabLoads) - avoid an overlapping duplicate fetch
  setTabLoading('normalise', true, 'Scanning');
  try {
    const r = await fetch(`/api/normalise/preview?rename_conflicts=${normaliseRenameConflicts}`
      + `&case_style=${normaliseCaseStyle}&sep_style=${normaliseSepStyle}`);
    const data = await r.json();
    if (data.ok === false) throw new Error(data.error || 'request failed');
    normaliseData = data;
    setTabLoading('normalise', false);
    updateJobsBadge();
    renderNormalise(data);
  } catch (e) {
    setTabLoading('normalise', false);
    document.getElementById('sub-normalise').innerHTML = loadErrorHtml('Normalisation', e, 'normalise-retry');
    document.getElementById('normalise-retry').onclick = loadNormalise;
  }
}

// What the "Normalised Path" column shows for an action that isn't a
// straightforward move/rename - there's no "Action" column anymore to
// spell these out, so the outcome has to read clearly from this cell alone.
function normalisedPathText(a) {
  if (a.type === 'conflict') return 'left in place (conflict)';
  if (a.type === 'quarantine') return 'duplicate → quarantined';
  if (a.type === 'delete') return 'duplicate → deleted';
  return a.dest || a.src;  // move / rename
}

function normaliseSortValue(a, col) {
  if (col === 'type') return a.is_dir ? 'Directory' : 'File';
  if (col === 'normalised') return normalisedPathText(a);
  return a.src;  // 'current'
}

function renderNormalise(data) {
  const panel = document.getElementById('sub-normalise');
  const total = data.actions.length;
  const toggle = pendingToggleHtml('normalise', total === 0,
    total === 0 ? 'Nothing to do' : `${plural(total, 'action')} planned`);
  // A conflict can show up either as an actual "conflict" (left alone) or,
  // once "rename conflicting file names" is checked, as a "rename" that's
  // flagged was_conflict - checking both means the warning box (and its
  // own checkbox) stays visible either way, so it's never possible to
  // check the box and have it vanish out from under you with no way back.
  const conflictActions = data.actions.filter(a => a.type === 'conflict' || a.was_conflict);
  let html = `<h2 class="page-title">Normalisation</h2>
    <p class="page-sub">Merges duplicate-looking sibling directories (e.g. "Foo" + "Foo_1") and renames files/directories to match the styles below.</p>${toggle}
    <div class="toolbar">
      <label>Case: <select id="normalise-case-style">
        <option value="none"${normaliseCaseStyle === 'none' ? ' selected' : ''}>unchanged</option>
        <option value="lower"${normaliseCaseStyle === 'lower' ? ' selected' : ''}>lowercase</option>
        <option value="camel"${normaliseCaseStyle === 'camel' ? ' selected' : ''}>camelCase</option>
      </select></label>
      <label>Separators: <select id="normalise-sep-style">
        <option value="none"${normaliseSepStyle === 'none' ? ' selected' : ''}>unchanged</option>
        <option value="spaces-dash"${normaliseSepStyle === 'spaces-dash' ? ' selected' : ''}>spaces &rarr; dashes</option>
        <option value="spaces-underscore"${normaliseSepStyle === 'spaces-underscore' ? ' selected' : ''}>spaces &rarr; underscores</option>
        <option value="dash-underscore"${normaliseSepStyle === 'dash-underscore' ? ' selected' : ''}>dashes &rarr; underscores</option>
        <option value="underscore-dash"${normaliseSepStyle === 'underscore-dash' ? ' selected' : ''}>underscores &rarr; dashes</option>
      </select></label>
      <button class="btn" id="normalise-rescan">Rescan</button>
    </div>`;
  if (conflictActions.length > 0) {
    html += `<div class="warn-box warn-yellow">
      <b>${plural(conflictActions.length, 'naming conflict')} found</b> - ${conflictActions.length === 1 ? 'a file or directory' : 'some files/directories'} would collide with an existing name after normalising.
      <label style="display:flex;gap:8px;align-items:center;margin-top:10px">
        <input type="checkbox" id="normalise-rename-conflicts" ${normaliseRenameConflicts ? 'checked' : ''}>
        Rename conflicting file names instead of leaving them as unresolved conflicts
      </label>
    </div>`;
  }
  if (data.actions.length === 0) {
    html += '<div class="empty">Nothing to normalise - no sibling directory duplicates, and names already match the selected styles.</div>';
    panel.innerHTML = html;
    wireNormaliseToolbar();
    return;
  }
  html += `<div class="toolbar">
      <label>Show: <select id="normalise-type-filter">
        <option value="all"${normaliseTypeFilter === 'all' ? ' selected' : ''}>Files &amp; directories</option>
        <option value="file"${normaliseTypeFilter === 'file' ? ' selected' : ''}>Files only</option>
        <option value="dir"${normaliseTypeFilter === 'dir' ? ' selected' : ''}>Directories only</option>
      </select></label>
      <span class="spacer-note">${plural(data.counts.rename || 0, 'file')} to Rename</span>
    </div>`;

  const filtered = data.actions.filter(a =>
    normaliseTypeFilter === 'all' ? true : normaliseTypeFilter === 'dir' ? a.is_dir : !a.is_dir);
  const sorted = filtered.slice().sort((a, b) => {
    const av = normaliseSortValue(a, normaliseSortCol).toLowerCase();
    const bv = normaliseSortValue(b, normaliseSortCol).toLowerCase();
    const cmp = av < bv ? -1 : av > bv ? 1 : a.src.localeCompare(b.src);  // tie-break: current path, always ascending
    return normaliseSortDir === 'asc' ? cmp : -cmp;
  });

  const arrow = col => normaliseSortCol === col ? (normaliseSortDir === 'asc' ? ' ▲' : ' ▼') : '';
  html += `<table class="plan"><tr>
      <th class="sortable" data-sort="type">Type${arrow('type')}</th>
      <th class="sortable" data-sort="current">Current Path${arrow('current')}</th>
      <th class="sortable" data-sort="normalised">Normalised Path${arrow('normalised')}</th>
    </tr>`;
  if (sorted.length === 0) {
    html += `<tr><td colspan="3" style="text-align:center;color:var(--text-faint);padding:16px">No ${normaliseTypeFilter === 'dir' ? 'directories' : 'files'} among the planned actions.</td></tr>`;
  }
  sorted.forEach(a => {
    const normalisedCell = (a.type === 'move' || a.type === 'rename')
      ? esc(normalisedPathText(a))
      : `<span class="dup-count">${esc(normalisedPathText(a))}</span>`;
    const conflictResolved = a.type === 'rename' && a.was_conflict;
    html += `<tr class="${a.type}${conflictResolved ? ' conflict-resolved' : ''}">
      <td>${a.is_dir ? 'Directory' : 'File'}</td>
      <td>${esc(a.src)}</td>
      <td>${normalisedCell}</td>
    </tr>`;
  });
  html += '</table>';
  panel.innerHTML = html;
  wireNormaliseToolbar();

  panel.querySelectorAll('th.sortable').forEach(th => {
    th.onclick = () => {
      const col = th.dataset.sort;
      if (normaliseSortCol === col) normaliseSortDir = normaliseSortDir === 'asc' ? 'desc' : 'asc';
      else { normaliseSortCol = col; normaliseSortDir = 'asc'; }
      renderNormalise(data);
    };
  });
  const filterSel = document.getElementById('normalise-type-filter');
  if (filterSel) filterSel.onchange = (e) => { normaliseTypeFilter = e.target.value; renderNormalise(data); };
}

function wireNormaliseToolbar() {
  document.getElementById('normalise-rescan').onclick = loadNormalise;
  const renameConflictsCb = document.getElementById('normalise-rename-conflicts');
  if (renameConflictsCb) {
    renameConflictsCb.onchange = (e) => {
      normaliseRenameConflicts = e.target.checked;
      loadNormalise();
    };
  }
  document.getElementById('normalise-case-style').onchange = (e) => {
    normaliseCaseStyle = e.target.value;
    loadNormalise();
  };
  document.getElementById('normalise-sep-style').onchange = (e) => {
    normaliseSepStyle = e.target.value;
    loadNormalise();
  };
  wirePendingToggle('normalise');
}

// ---------- Upscale sub-tab ----------
//
// Not part of the Pending Jobs staging system the other three Operations
// tabs share - AI upscaling is additive (writes a new file, never
// moves/deletes anything), so there's no quarantine/manifest/restore
// story here at all, and no "keep vs discard" decision to stage. It gets
// its own self-contained Start button instead, same shape as the
// Quarantine tab's buttons.

let upscaleData = null;      // raw preview payload from the last scan
let upscaleTarget = null;    // slider value - null until the first load pulls default_target
let upscaleAffix = null;     // filename affix text - null until the first load pulls default_affix
let upscaleAffixPos = 'suffix';  // 'suffix' (append) | 'prefix' (prepend)
let upscaleOutDir = null;    // null = alongside each original; else an absolute directory
let upscaleOverwrite = false; // replace an output file that already exists, instead of skipping it
let upscaleSliderDebounce = null;
const UPSCALE_WARN_THRESHOLD = 15;  // eligible count at/above which the "this'll take a while" box shows

// Matches the server-side guard: an empty affix only overwrites originals
// when the copy also lands in the source location, so a separate output
// directory - one that isn't the scan root itself - makes an empty affix
// safe.
function upscaleOutputOk() {
  const outDirDistinct = upscaleOutDir && upscaleOutDir !== currentRoot;
  return !!(upscaleAffix || outDirDistinct);
}

// "photo.jpg" -> what the affix + position would name it
function upscaleExampleName() {
  const a = upscaleAffix || '';
  return (upscaleAffixPos === 'prefix' ? a + 'photo' : 'photo' + a) + '.jpg';
}

// In alongside mode (no output dir), a file that already carries the affix
// is a previous run's own output - server-side run_upscale skips it too,
// so keep the eligible list in step.
function upscaleNameIsAffixed(path) {
  if (!upscaleAffix || upscaleOutDir) return false;
  const stem = path.split('/').pop().replace(/\.[^./]*$/, '');
  return upscaleAffixPos === 'prefix' ? stem.startsWith(upscaleAffix) : stem.endsWith(upscaleAffix);
}

async function loadUpscale() {
  if (tabLoading.upscale) return;  // already in flight (e.g. from startAllTabLoads) - avoid an overlapping duplicate fetch
  setTabLoading('upscale', true, 'Scanning');
  try {
    const q = upscaleOutDir ? `?out_dir=${encodeURIComponent(upscaleOutDir)}` : '';
    const r = await fetch('/api/upscale/preview' + q);
    const data = await r.json();
    upscaleData = data;
    if (upscaleTarget === null) upscaleTarget = data.default_target;
    if (upscaleAffix === null) upscaleAffix = data.default_affix;
    setTabLoading('upscale', false);
    renderUpscale(data);
  } catch (e) {
    setTabLoading('upscale', false);
    document.getElementById('sub-upscale').innerHTML = loadErrorHtml('Upscale', e, 'upscale-retry');
    document.getElementById('upscale-retry').onclick = loadUpscale;
  }
}

function eligibleUpscaleImages(data, target) {
  return data.images.filter(im => im.longest < target && !upscaleNameIsAffixed(im.path));
}

// The summary text (updated in place), and the warning + scrollable list
// (rebuilt). Deliberately never touches the slider, options rows or Start
// button elements themselves, so re-running this on every slider "input"
// event (i.e. continuously while dragging) can't interrupt an in-progress
// drag by replacing the <input type=range> out from under the browser.
function updateUpscaleEligibleSection(data, target) {
  const section = document.getElementById('upscale-eligible-section');
  if (!section) return;
  const eligible = eligibleUpscaleImages(data, target);
  const startBtn = document.getElementById('upscale-start');
  if (startBtn) startBtn.disabled = !!data.tool_error || eligible.length === 0 || !upscaleOutputOk();
  const hint = document.getElementById('upscale-start-hint');
  if (hint) hint.textContent = (!data.tool_error && !upscaleOutputOk())
    ? 'Set a filename prefix/suffix or a separate output directory first.' : '';

  const hiddenN = data.images.length - eligible.length;
  const summaryEl = document.getElementById('upscale-summary');
  if (summaryEl) summaryEl.innerHTML =
    `${plural(eligible.length, 'image')} below ${target}px on their longest side` +
    (eligible.length ? ' - eligible for upscaling.' : '.') +
    (hiddenN > 0 ? ` (${plural(hiddenN, 'other image')} not shown - already large enough, or an existing ${esc(upscaleAffix || '_upscaled')} output.)` : '');

  let html = '';
  if (eligible.length >= UPSCALE_WARN_THRESHOLD) {
    html += `<div class="warn-box warn-yellow"><b>${plural(eligible.length, 'image')} queued</b> - AI upscaling runs one image at a time on the GPU and can take anywhere from several seconds to over a minute per image depending on how much upscaling it needs. With this many eligible, running this could take a long time.</div>`;
  }

  if (eligible.length === 0) {
    html += `<div class="empty">${data.images.length === 0 ? 'No images found in this directory.' : `Nothing to upscale at ${target}px - every image is already at least that large, or is an existing ${esc(upscaleAffix || '_upscaled')} output.`}</div>`;
    section.innerHTML = html;
    return;
  }

  html += '<div class="thumb-grid upscale-thumb-scroll">';
  eligible.forEach(im => {
    const scale = target / im.longest;
    const newW = Math.max(1, Math.round(im.width * scale));
    const newH = Math.max(1, Math.round(im.height * scale));
    html += `<div class="thumb-row">
      ${thumbHtml(im.path, false)}
      <div class="info"><div class="path">${esc(im.path)}</div>
      <div class="meta">${im.size_human} &middot; ${im.width}&times;${im.height} &rarr; ${newW}&times;${newH}</div></div>
    </div>`;
  });
  html += '</div>';
  section.innerHTML = html;
}

function renderUpscale(data) {
  const panel = document.getElementById('sub-upscale');
  let html = `<h2 class="page-title">Upscale</h2>
    <p class="page-sub">AI-upscales images (Real-ESRGAN, GPU) so their longest side reaches the target below, preserving aspect ratio. Images already at or above the target aren't touched or listed. Each result is written as a new file - originals are never modified or removed.</p>`;

  // Dependency box - only shows here (the command line already noted it at
  // startup), with a one-click download of the self-contained build.
  if (data.tool_error) {
    html += `<div class="warn-box warn-yellow" id="upscale-dep-box">
      <b>realesrgan-ncnn-vulkan isn't installed.</b> It's the GPU tool that does the actual upscaling - every other tab works without it.
      <div style="margin-top:10px">${esc(data.tool_error)}</div>
      <div class="toolbar" style="margin:12px 0 0">
        <button class="btn btn-primary" id="upscale-install">Download realesrgan-ncnn-vulkan</button>
        <span class="dim">~45&nbsp;MB, self-contained, into <code>~/.local/share/dedupe-images/</code> - no sudo.</span>
      </div>
    </div>`;
  }

  html += `<div class="upscale-slider-row">
    <label for="upscale-target">Target longest side: <span class="upscale-target-value" id="upscale-target-value">${upscaleTarget}px</span></label>
    <input type="range" id="upscale-target" min="${data.min_target}" max="${data.max_target}" step="1" value="${upscaleTarget}">
    <button class="btn" id="upscale-rescan">Rescan</button>
  </div>`;

  const outLabel = upscaleOutDir
    ? `<code>${esc(upscaleOutDir)}</code> <span class="dim">(source sub-folders recreated inside)</span>`
    : 'Alongside each original image';
  html += `<div class="upscale-opt-row">
    <label>Save to:</label>
    <span id="upscale-outdir-label">${outLabel}</span>
    <button class="btn btn-sm" id="upscale-outdir-pick">Choose directory&hellip;</button>
    ${upscaleOutDir ? '<button class="btn btn-sm" id="upscale-outdir-reset">Reset to default</button>' : ''}
  </div>
  <div class="upscale-opt-row">
    <label id="upscale-affix-label" for="upscale-affix">Filename ${upscaleAffixPos === 'prefix' ? 'prefix' : 'suffix'}:</label>
    <input type="text" id="upscale-affix" value="${esc(upscaleAffix)}" spellcheck="false" style="width:150px">
    <select id="upscale-affix-pos">
      <option value="suffix"${upscaleAffixPos === 'suffix' ? ' selected' : ''}>Append (after name)</option>
      <option value="prefix"${upscaleAffixPos === 'prefix' ? ' selected' : ''}>Prepend (before name)</option>
    </select>
    <span class="dim">e.g. <code id="upscale-affix-example">photo.jpg &rarr; ${esc(upscaleExampleName())}</code></span>
  </div>
  <div class="upscale-opt-row">
    <label style="font-weight:400;cursor:pointer"><input type="checkbox" id="upscale-overwrite" ${upscaleOverwrite ? 'checked' : ''}> Overwrite existing upscaled files</label>
    <span class="dim">off: a file whose output already exists is skipped and reported, not replaced</span>
  </div>`;

  html += `<div class="upscale-run-row">
    <div class="upscale-summary" id="upscale-summary"></div>
    <span class="spacer-note" id="upscale-start-hint"></span>
    <button class="btn btn-primary btn-lg" id="upscale-start" ${data.tool_error ? 'disabled' : ''}>Start Upscale</button>
  </div>
  <div id="upscale-eligible-section"></div>`;
  panel.innerHTML = html;
  updateUpscaleEligibleSection(data, upscaleTarget);

  const installBtn = document.getElementById('upscale-install');
  if (installBtn) installBtn.onclick = async () => {
    const result = await runJob('Downloading realesrgan-ncnn-vulkan&hellip;', '/api/upscale/install', {});
    if (result === null) return;  // runJob already alerted
    alert('realesrgan-ncnn-vulkan installed. See the log for any warnings (e.g. a missing Vulkan driver).');
    loadUpscale();
  };

  document.getElementById('upscale-rescan').onclick = loadUpscale;
  document.getElementById('upscale-target').oninput = (e) => {
    upscaleTarget = parseInt(e.target.value, 10);
    document.getElementById('upscale-target-value').textContent = upscaleTarget + 'px';
    clearTimeout(upscaleSliderDebounce);
    upscaleSliderDebounce = setTimeout(() => updateUpscaleEligibleSection(upscaleData, upscaleTarget), 80);
  };

  document.getElementById('upscale-outdir-pick').onclick = () => openPicker({
    title: 'Choose a directory for the upscaled images',
    useLabel: 'Save upscaled images here',
    startAt: upscaleOutDir || currentRoot || null,
    allowCreate: true,
    onChoose: (path) => { upscaleOutDir = path; loadUpscale(); },
  });
  const resetBtn = document.getElementById('upscale-outdir-reset');
  if (resetBtn) resetBtn.onclick = () => { upscaleOutDir = null; loadUpscale(); };

  const affixInput = document.getElementById('upscale-affix');
  const affixPos = document.getElementById('upscale-affix-pos');
  function syncAffix() {
    upscaleAffix = affixInput.value;
    upscaleAffixPos = affixPos.value;
    document.getElementById('upscale-affix-label').textContent =
      'Filename ' + (upscaleAffixPos === 'prefix' ? 'prefix' : 'suffix') + ':';
    document.getElementById('upscale-affix-example').innerHTML =
      'photo.jpg &rarr; ' + esc(upscaleExampleName());
    updateUpscaleEligibleSection(upscaleData, upscaleTarget);  // Start disabled/hint depend on the affix
  }
  affixInput.oninput = syncAffix;
  affixPos.onchange = syncAffix;
  document.getElementById('upscale-overwrite').onchange = (e) => { upscaleOverwrite = e.target.checked; };

  document.getElementById('upscale-start').onclick = async () => {
    const eligible = eligibleUpscaleImages(upscaleData, upscaleTarget);
    const where = upscaleOutDir
      ? `into <code>${esc(upscaleOutDir)}</code> (source sub-folders recreated)`
      : 'next to each original';
    const existing = upscaleOverwrite
      ? '<p>An output file that already exists <b>will be overwritten</b>.</p>'
      : '<p>A file whose output already exists is skipped (and reported), not replaced.</p>';
    confirmAction('Start upscaling',
      `<p>${plural(eligible.length, 'image')} below ${upscaleTarget}px will be upscaled to that size on their longest side, saved ${where} with <code>${esc(upscaleExampleName())}</code>-style names. Originals are never touched.</p>
       ${existing}
       <p>This runs on the GPU and can take a while - the page shows progress as it goes.</p>`,
      async () => {
        const result = await runJob('Upscaling images&hellip;', '/api/upscale/run',
          {target: upscaleTarget, out_dir: upscaleOutDir || '', affix: upscaleAffix,
           affix_pos: upscaleAffixPos, overwrite: upscaleOverwrite});
        if (result === null) return;
        let msg = `Upscaled ${plural(result.processed, 'image')}.`;
        if (result.skipped && result.skipped.length)
          msg += ` ${plural(result.skipped.length, 'image')} skipped - output already exists (tick "Overwrite existing upscaled files" to replace them).`;
        if (result.errors.length) msg += ` ${plural(result.errors.length, 'image')} failed - see the server console for details.`;
        alert(msg);
        loadUpscale();
      },
      {confirmLabel: 'Start'});
  };
}

// ---------- Jobs tab (Pending Jobs) ----------

let skipQuarantine = false;
let lastBuiltReview = null;

async function loadJobs() {
  const panel = document.getElementById('tab-jobs');
  const activeOps = Object.keys(pendingOps).filter(k => pendingOps[k]);
  if (activeOps.length === 0) {
    lastBuiltReview = null;
    panel.innerHTML = `<h2 class="page-title">Pending Jobs</h2>
      <div class="empty">No pending jobs.<br><span class="empty-hint">Enable "Add to Pending Jobs" on an Operations tab to add it here.</span></div>`;
    return;
  }
  panel.innerHTML = `<h2 class="page-title">Pending Jobs</h2><div class="spinner">Building summary&hellip;</div>`;
  const result = await silentBuild(activeOps,
    {prefer: identicalPrefer, rename_conflicts: normaliseRenameConflicts, delete_duplicates: skipQuarantine,
     case_style: normaliseCaseStyle, sep_style: normaliseSepStyle});
  if (activeTab !== 'jobs') return;  // user navigated away while this was loading
  if (result === null) {
    panel.innerHTML = `<h2 class="page-title">Pending Jobs</h2><div class="empty">Could not build the summary - try again.</div>`;
    return;
  }
  lastBuiltReview = result;
  renderJobsSummary(result);
}

function renderJobsSummary(data) {
  const panel = document.getElementById('tab-jobs');
  let html = '<h2 class="page-title">Pending Jobs</h2><div class="pending-ops-list">';
  data.ops.forEach(op => {
    const opItems = data.items.filter(it => it.op === op);
    const opConflicts = opItems.filter(it => it.action === 'conflict').length;
    const actionable = opItems.length - opConflicts;
    html += `<div class="pending-op-row">
      <div class="op-name">${OP_LABELS[op]}</div>
      <div class="op-note">${plural(actionable, 'action')}${opConflicts ? ` &middot; ${plural(opConflicts, 'conflict')} left alone` : ''}</div>
      <button class="btn btn-sm" data-cancel-op="${op}">Cancel</button>
    </div>`;
  });
  html += '</div>';

  html += `<div class="checkbox-row"><input type="checkbox" id="skip-quarantine" ${skipQuarantine ? 'checked' : ''}>
    <label for="skip-quarantine">Skip quarantine - permanently delete duplicates immediately instead of moving them to quarantine for later review</label></div>`;
  if (skipQuarantine) {
    html += `<div class="warn-box">This cannot be undone. Files processed this way are deleted immediately, not quarantined - <code>dedupe_images.py --restore</code> will not be able to bring them back.</div>`;
  }

  if (data.items.length === 0) {
    html += '<div class="empty">Nothing to do for the pending operation(s).</div>';
    panel.innerHTML = html;
    wireJobsControls(data);
    return;
  }

  const counts = data.counts;
  const removeWord = data.delete_duplicates ? 'delete' : 'quarantine';
  html += `<div class="summary">
    ${plural(data.items.length, 'planned action')} across ${plural(data.ops.length, 'operation')} &middot;
    ${plural((counts.quarantine||0)+(counts.delete||0), 'file')} to ${removeWord} &middot; ${plural(counts.move||0, 'file')} to move &middot;
    ${plural(counts.rename||0, 'file')} to rename &middot; ${plural(counts.conflict||0, 'conflict')} left alone &middot;
    reclaiming ${data.total_size_human}
  </div>`;
  html += '<div class="thumb-grid">';
  data.items.forEach(it => {
    const thumb = thumbHtml(it.path, it.is_dir);
    let meta = human(it.size);
    if (it.dest) meta += ` &rarr; ${esc(it.dest)}`;
    if (it.kept) meta += ` (kept: ${esc(it.kept)})`;
    html += `<div class="thumb-row ${it.action}">
      ${thumb}
      <div class="info"><div class="path">${esc(it.path)}</div><div class="meta">${meta}</div></div>
      <span class="op-tag">${esc(OP_LABELS[it.op] || it.op)}</span>
      <span class="action-tag ${it.action}">${esc(it.action)}</span>
    </div>`;
  });
  html += '</div>';
  html += `<div class="toolbar" style="margin-top:18px"><button class="btn btn-primary btn-lg" id="start-btn">Start</button></div>`;
  panel.innerHTML = html;
  wireJobsControls(data);
  document.getElementById('start-btn').onclick = onStart;
}

function wireJobsControls(data) {
  document.querySelectorAll('[data-cancel-op]').forEach(btn => {
    btn.onclick = () => { pendingOps[btn.dataset.cancelOp] = false; updateJobsBadge(); loadJobs(); };
  });
  const sq = document.getElementById('skip-quarantine');
  if (sq) sq.onchange = (e) => { skipQuarantine = e.target.checked; loadJobs(); };
}

function onStart() {
  const data = lastBuiltReview;
  if (!data) return;
  // Count by what each action actually does. Only "delete" removes a file
  // for good; "quarantine"/"move"/"rename" are all recorded in the
  // manifest and reversible with --restore. "conflict" does nothing.
  const c = data.counts || {};
  const deleteN = c.delete || 0;
  const reversibleN = (c.quarantine || 0) + (c.move || 0) + (c.rename || 0);
  const opsLine = `<p>${plural(data.ops.length, 'operation')} will run: ${data.ops.map(o => OP_LABELS[o]).join(', ')}.</p>`;
  const recomputeNote = `<p><b>Note:</b> Identical Files / Normalisation plans are recomputed fresh at the moment this runs (not replayed from this summary), ` +
    `in case the folder changed since it was built - Visually Similar decisions are always applied exactly as you decided them.</p>`;
  let body;
  let confirmLabel = 'Start';
  let requireCheckbox = null;
  if (deleteN > 0) {
    // skip-quarantine is on AND this plan has duplicates to delete outright
    const also = reversibleN > 0
      ? ` A further ${plural(reversibleN, 'file')} will be moved or renamed - those are recorded in the manifest and reversible.`
      : '';
    body = `<div class="warn-box"><b>${plural(deleteN, 'file')} will be PERMANENTLY DELETED</b>, not quarantined. ` +
      `This cannot be undone - there is no way to get ${deleteN === 1 ? 'it' : 'them'} back afterward.${also}</div>` +
      opsLine +
      `<label style="display:flex;gap:8px;align-items:center;margin-top:10px"><input type="checkbox" id="start-confirm-check"> I understand this permanently deletes files with no way to undo it.</label>`;
    confirmLabel = 'Delete permanently';
    requireCheckbox = 'start-confirm-check';
  } else {
    body = `<p>${plural(reversibleN, 'file')} across ${plural(data.ops.length, 'operation')} will be moved to <code>_duplicates_quarantine/</code>, merged into a keeper folder, or renamed. ` +
      `Nothing is deleted - all of it is reversible with <code>dedupe_images.py --restore</code> until you delete the quarantine folder.</p>` +
      recomputeNote;
  }
  confirmAction('Start pending jobs', body, async () => {
    const result = await runJob('Running operations', '/api/review/run',
      {ops: data.ops, prefer: identicalPrefer, rename_conflicts: normaliseRenameConflicts, delete_duplicates: skipQuarantine,
       case_style: normaliseCaseStyle, sep_style: normaliseSepStyle});
    if (result === null) return;
    showRunResult(result);
    pendingOps = {identical: false, normalise: false, visual: false};
    skipQuarantine = false;
    lastBuiltReview = null;
    updateJobsBadge();
    refreshQuarantineBadge();
    // Identical Files / Normalisation cache would otherwise show a
    // pre-run plan (files that no longer exist, e.g.) - refresh both now
    // that the tree has actually changed. Visually Similar isn't touched
    // here: it has its own in-progress review state tied to a specific
    // scan, which a silent reset would disrupt.
    identicalData = null;
    normaliseData = null;
    loadIdentical();
    loadNormalise();
    loadJobs();
  }, {confirmLabel, requireCheckbox});
}

function showRunResult(result) {
  let msg = 'Done.\n\n';
  if (result.identical) msg += `Identical Files: ${result.delete_duplicates ? 'deleted' : 'quarantined'} ${result.identical.processed}.\n`;
  if (result.normalise) {
    const n = result.normalise;
    msg += `Normalisation: moved ${n.dir_move}, ${result.delete_duplicates ? 'deleted' : 'quarantined'} ${n.dir_processed} (dir merge), `
         + `renamed ${n.dir_renamed}, ${n.dir_conflicts} conflict(s) left (dir merge); `
         + `renamed ${n.renamed_files} file(s) / ${n.renamed_dirs} dir(s), merged ${n.merged_files}, `
         + `${result.delete_duplicates ? 'deleted' : 'quarantined'} ${n.processed}, ${n.conflicts} conflict(s) left (lowercase).\n`;
  }
  if (result.visual) msg += `Visually Similar: ${result.delete_duplicates ? 'deleted' : 'quarantined'} ${result.visual.processed}.\n`;
  msg += `\n${plural(result.emptied_dirs, 'empty directory').replace(/directorys$/, 'directories')} removed.`;
  if (!result.delete_duplicates) {
    msg += `\n\nFiles were moved to quarantine, not deleted. Go to the Quarantine tab to review and permanently remove them when you're ready.`;
  }
  alert(msg);
}

// ---------- Quarantine tab ----------

async function loadQuarantine() {
  const panel = document.getElementById('tab-quarantine');
  panel.innerHTML = bigSpinnerHtml('Checking');
  const data = await refreshQuarantineBadge();  // one fetch drives both the badge and the panel
  if (data === null) {
    panel.innerHTML = loadErrorHtml('Quarantine', new Error('could not reach the server'), 'quarantine-retry');
    document.getElementById('quarantine-retry').onclick = loadQuarantine;
    return;
  }
  renderQuarantine(data);
}

function renderQuarantine(data) {
  const panel = document.getElementById('tab-quarantine');
  let html = '<h2 class="page-title">Quarantine</h2>';
  if (!data.exists || data.file_count === 0) {
    html += '<div class="empty">No quarantined files right now.</div>';
    panel.innerHTML = html;
    return;
  }
  html += `<div class="quarantine-status">
    <div class="big">${plural(data.file_count, 'file')} quarantined, ${data.total_size_human}</div>
    <div class="summary" style="margin:0">Location: <code>${esc(data.path)}</code></div>
  </div>`;
  html += `<div class="warn-box">Deleting the quarantine folder is <b>permanent</b> - it cannot be undone, and restoring will no longer be able to bring ${data.file_count === 1 ? 'this file' : 'these files'} back. Only do this once you've confirmed everything looks right.</div>`;
  const restorableCount = (data.files || []).filter(f => f.original_path).length;
  html += `<div class="toolbar">
    <button class="btn" id="qt-restore-all" ${restorableCount === 0 ? 'disabled' : ''}>Restore all&hellip;</button>
    <button class="btn btn-danger" id="qt-delete">Delete quarantine folder permanently&hellip;</button>
  </div>`;
  html += '<div class="thumb-grid">';
  (data.files || []).forEach(f => {
    const thumb = thumbHtml(f.path, false);
    // Original path (where it lived before quarantine) is the label a
    // user will actually recognize - fall back to the in-quarantine path
    // only when there's no manifest record to say what it used to be.
    const label = f.original_path || f.path;
    const metaParts = [f.size_human];
    if (f.moved_at) metaParts.push(`quarantined ${formatWhen(f.moved_at)}`);
    if (f.kept_path) metaParts.push(`kept instead: ${esc([].concat(f.kept_path).join(', '))}`);
    if (!f.original_path) metaParts.push('no manifest record for this file');
    // Only a file with a manifest record has a known original_path to
    // restore to - without one there's nowhere to put it back.
    const restoreBtn = f.original_path
      ? `<button class="btn btn-sm qt-restore-one" data-path="${esc(f.path)}">Restore</button>`
      : '';
    html += `<div class="thumb-row">
      ${thumb}
      <div class="info"><div class="path">${esc(label)}</div><div class="meta">${metaParts.join(' &middot; ')}</div></div>
      ${restoreBtn}
    </div>`;
  });
  html += '</div>';
  panel.innerHTML = html;

  async function doRestore(paths) {
    const r = await fetch('/api/quarantine/restore', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(paths === null ? {} : {paths}),
    });
    const result = await r.json();
    if (!result.ok) { alert(result.error || 'restore failed'); return; }
    let msg = `Restored ${plural(result.restored.length, 'file')}.`;
    if (result.conflicts.length) msg += ` ${plural(result.conflicts.length, 'file')} skipped - something already exists at the original location.`;
    if (result.missing.length) msg += ` ${plural(result.missing.length, 'file')} skipped - no longer found in quarantine.`;
    alert(msg);
    loadQuarantine();
  }

  panel.querySelectorAll('.qt-restore-one').forEach(btn => {
    btn.onclick = () => doRestore([btn.dataset.path]);
  });

  document.getElementById('qt-restore-all').onclick = () => {
    confirmAction('Restore all quarantined files',
      `<p>${plural(restorableCount, 'file')} will be moved back to where they originally were.</p>`,
      () => doRestore(null),
      {confirmLabel: 'Restore all'});
  };

  document.getElementById('qt-delete').onclick = () => {
    const fileWord = data.file_count === 1 ? 'the file' : 'everything';
    confirmAction('Permanently delete quarantine folder',
      `<div class="warn-box">This will permanently delete ${plural(data.file_count, 'file')} (${data.total_size_human}) with no way to undo it. ` +
      `Restoring will stop working for ${fileWord} currently in quarantine.</div>` +
      `<label style="display:flex;gap:8px;align-items:center;margin-top:10px"><input type="checkbox" id="qt-confirm-check"> I understand this cannot be undone.</label>`,
      async () => {
        const r = await fetch('/api/quarantine/delete', {method: 'POST'});
        const result = await r.json();
        if (!result.ok) { alert(result.error || 'delete failed'); return; }
        alert(`Deleted ${plural(result.removed_files, 'file')}.`);
        loadQuarantine();
      },
      {confirmLabel: 'Delete permanently', requireCheckbox: 'qt-confirm-check'});
  };
}

// ---------- decorative background easter egg ----------
//
// A scatter of little words behind the page, each idle for most of a
// randomized-per-word cycle and only briefly fading up to ~8% opacity
// while drifting sideways - meant to be almost imperceptible, not a
// feature. #kobold-bg is pointer-events:none with a negative z-index (see
// its CSS), so this can't affect usability no matter what: nothing here
// ever intercepts a click or blocks content. Shorter cycles + more words
// than a first pass at this - now frequent enough that more than one can
// be on screen peeking at once, not just one at a time.
function spawnKoboldWords() {
  const container = document.getElementById('kobold-bg');
  if (!container) return;
  const words = [
    'rawr', 'yip', 'rawr!', 'yip!', 'omg i love ur art',
    'sheaths~', 'paws~', 'maws~', 'dragons~', 'kobolds~', 'yoshis~', 'mlem',
  ];
  for (let i = 0; i < 14; i++) {
    const el = document.createElement('span');
    el.className = 'kobold-word';
    el.textContent = words[Math.floor(Math.random() * words.length)];
    el.style.top = (Math.random() * 92 + 2) + 'vh';
    el.style.left = (Math.random() * 88 + 2) + 'vw';
    const duration = 10 + Math.random() * 14;
    el.style.animationDuration = duration + 's';
    el.style.animationDelay = (-Math.random() * duration) + 's';  // negative: starts mid-cycle, staggers immediately instead of a synchronized first flash
    container.appendChild(el);
  }
}

// ---------- boot ----------

(async () => {
  spawnKoboldWords();
  // picks up a directory already chosen (or a scan already in progress) -
  // e.g. review_gui.py was launched with a root argument, or this tab was
  // reloaded mid-scan. A plain page reload wipes all client-side JS state
  // even though the server-side root/scan didn't change, so this needs to
  // (re)kick off all three tabs' loads same as a fresh "Use this directory".
  const s = await refreshRootLabel();
  if (s.root) startAllTabLoads();
  await reloadActive();
  if (!currentRoot) openPicker();
})();
</script>
</body></html>
"""


def _normalise_preview_json(root: Path, rename_conflicts: bool, case_style: str = "lower",
                             sep_style: str = "none") -> dict:
    quarantine_dir = root / QUARANTINE_DIRNAME
    dm_actions = plan_and_maybe_execute_dir_merge(root, quarantine_dir, False, [],
                                                    rename_conflicts=rename_conflicts)
    for a in dm_actions:
        a.pop("merge", None)
    lc_stats = plan_and_maybe_execute_normalize(root, quarantine_dir, False, [],
                                                 rename_conflicts=rename_conflicts,
                                                 case_style=case_style, sep_style=sep_style)
    actions = dm_actions + lc_stats["actions"]
    enrich_actions(root, actions)
    counts = {}
    for a in actions:
        counts[a["type"]] = counts.get(a["type"], 0) + 1
    return {"actions": actions, "counts": counts}


class QuietHTTPServer(ThreadingHTTPServer):
    """A browser cancelling/resetting an in-flight request (e.g. navigating
    away before an <img> finishes loading) is normal, expected behavior,
    not a server bug - but socketserver's default handle_error() dumps a
    full traceback for any exception raised while handling a request,
    which makes this look alarming. Suppress just that specific, benign
    class of error; anything else still prints normally so real bugs stay
    visible."""

    def handle_error(self, request, client_address):
        exc_type = sys.exc_info()[0]
        if exc_type in (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            return
        super().handle_error(request, client_address)


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # keep stdout quiet; nothing security-sensitive to audit here

        def _json(self, obj, status=200):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _require_root(self):
            with state.lock:
                root = state.root
            if root is None:
                self._json({"ok": False, "error": "no directory chosen"}, status=400)
                return None
            return root

        def _valid_styles(self, case_style, sep_style):
            """True if both normalisation style params are recognised;
            otherwise emits a 400 and returns False so the caller returns."""
            if case_style not in CASE_STYLES or sep_style not in SEPARATOR_STYLES:
                self._json({"ok": False, "error": "invalid case_style/sep_style"}, status=400)
                return False
            return True

        def do_GET(self):
            parsed = urlparse(self.path)

            if parsed.path == "/":
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path == "/api/state":
                with state.lock:
                    self._json({"root": str(state.root) if state.root else None})
                return

            if parsed.path == "/api/progress":
                self._json(progress.snapshot())
                return

            if parsed.path == "/api/scan-progress":
                self._json(scan_progress.snapshot())
                return

            if parsed.path == "/api/browse":
                qs = parse_qs(parsed.query)
                raw = qs.get("path", [None])[0]
                path = Path(raw).expanduser() if raw else Path.home()
                try:
                    path = path.resolve()
                except OSError as e:
                    self._json({"ok": False, "error": str(e)})
                    return
                if not path.is_dir():
                    self._json({"ok": False, "error": f"not a directory: {path}"})
                    return
                dirs, err = list_dir(path)
                if err is not None:
                    self._json({"ok": False, "error": err})
                    return
                parent = str(path.parent) if path.parent != path else None
                self._json({"ok": True, "path": str(path), "parent": parent, "dirs": dirs})
                return

            if parsed.path == "/api/nd/groups":
                with state.lock:
                    self._json({"groups": state.groups, "decisions": state.decisions})
                return

            if parsed.path == "/api/identical/preview":
                root = self._require_root()
                if root is None:
                    return
                qs = parse_qs(parsed.query)
                prefer = qs.get("prefer", ["oldest"])[0]
                quarantine_dir = root / QUARANTINE_DIRNAME
                plan = plan_file_dedupe(root, state.extensions, quarantine_dir, prefer)
                groups = []
                total_files = 0
                total_size = 0
                for entry in plan:
                    move = []
                    for m in entry["move"]:
                        sz = m.stat().st_size if m.exists() else 0
                        move.append({"path": str(m.relative_to(root)), "size": sz, "size_human": human(sz)})
                        total_files += 1
                        total_size += sz
                    groups.append({
                        "keep": str(entry["keep"].relative_to(root)),
                        "keep_size_human": human(entry["size"]),
                        "move": move,
                    })
                self._json({"groups": groups, "total_files": total_files,
                             "total_size_human": human(total_size)})
                return

            if parsed.path == "/api/normalise/preview":
                root = self._require_root()
                if root is None:
                    return
                qs = parse_qs(parsed.query)
                rename_conflicts = qs.get("rename_conflicts", ["false"])[0] == "true"
                case_style = qs.get("case_style", ["lower"])[0]
                sep_style = qs.get("sep_style", ["none"])[0]
                if not self._valid_styles(case_style, sep_style):
                    return
                self._json(_normalise_preview_json(root, rename_conflicts, case_style, sep_style))
                return

            if parsed.path == "/api/upscale/preview":
                root = self._require_root()
                if root is None:
                    return
                # Unfiltered by target size - the web UI filters/re-renders
                # client-side as the resolution slider moves, rather than
                # re-scanning the whole tree on every tick.
                qs = parse_qs(parsed.query)
                excludes = {root / QUARANTINE_DIRNAME, root / REVIEW_DIRNAME}
                # out_dir is only used here to keep a previous run's own
                # output from showing up as fresh candidates; a path that
                # doesn't exist yet simply excludes nothing.
                out_raw = (qs.get("out_dir", [""])[0] or "").strip()
                if out_raw:
                    try:
                        excludes.add(Path(out_raw).expanduser().resolve())
                    except OSError:
                        pass
                images = []
                for p, w, h in upscale.iter_upscale_candidates(root, excludes):
                    size = p.stat().st_size
                    images.append({
                        "path": str(p.relative_to(root)), "width": w, "height": h,
                        "longest": max(w, h), "size": size, "size_human": human(size),
                    })
                self._json({
                    "images": images, "min_target": upscale.MIN_TARGET, "max_target": upscale.MAX_TARGET,
                    "default_target": upscale.DEFAULT_TARGET, "default_affix": upscale.DEFAULT_AFFIX,
                    "tool_error": upscale.tool_status(),
                })
                return

            if parsed.path == "/api/quarantine/status":
                root = self._require_root()
                if root is None:
                    return
                quarantine_dir = root / QUARANTINE_DIRNAME
                if not quarantine_dir.exists():
                    self._json({"exists": False, "file_count": 0, "total_size_human": "0.0B", "files": []})
                    return
                # Only "quarantine"-type entries ever land under
                # quarantine_dir (a "deleted" entry was removed outright,
                # never moved; "rename"/"merge" entries stay under root
                # proper) - keyed by new_path (root-relative) so each file
                # actually still sitting in quarantine_dir can be matched
                # back to its manifest record.
                manifest = load_manifest(quarantine_dir / MANIFEST_NAME)
                by_new_path = {e["new_path"]: e for e in manifest
                               if e.get("type") == "quarantine" and e.get("new_path")}
                files = []
                total = 0
                for dirpath, dirnames, filenames in os.walk(quarantine_dir):
                    for fn in filenames:
                        if fn == MANIFEST_NAME:
                            continue
                        fpath = Path(dirpath) / fn
                        try:
                            size = fpath.stat().st_size
                        except OSError:
                            continue
                        total += size
                        rel = str(fpath.relative_to(root))
                        entry = by_new_path.get(rel)
                        files.append({
                            "path": rel, "size": size, "size_human": human(size),
                            "original_path": entry["original_path"] if entry else None,
                            "kept_path": entry["kept_path"] if entry else None,
                            "moved_at": entry["moved_at"] if entry else None,
                        })
                # newest-quarantined first - entries with no manifest match
                # (moved_at is None) sort last rather than first
                files.sort(key=lambda f: f["moved_at"] or "", reverse=True)
                self._json({"exists": True, "file_count": len(files), "total_size_human": human(total),
                             "path": str(quarantine_dir), "files": files})
                return

            if parsed.path.startswith("/img/"):
                with state.lock:
                    root = state.root
                if root is None:
                    self.send_error(404, "no directory chosen")
                    return
                rel = unquote(parsed.path[len("/img/"):])
                fpath = (root / rel).resolve()
                if not fpath.is_relative_to(root) or not fpath.is_file():
                    self.send_error(404, "not found")
                    return
                if fpath.suffix.lower() in (".tif", ".tiff"):
                    # No browser renders TIFF in an <img> tag - transcode to
                    # PNG on the fly so it actually displays instead of a
                    # broken-image icon. RGBA is a safe universal target
                    # (handles CMYK/grayscale/palette source modes too).
                    with Image.open(fpath) as img:
                        buf = io.BytesIO()
                        img.convert("RGBA").save(buf, format="PNG")
                    data = buf.getvalue()
                    ctype = "image/png"
                else:
                    ctype = mimetypes.guess_type(fpath.name)[0] or "application/octet-stream"
                    data = fpath.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "private, max-age=3600")
                self.end_headers()
                self.wfile.write(data)
                return

            self.send_error(404)

        def do_POST(self):
            path = urlparse(self.path).path
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}

            if path == "/api/mkdir":
                # One new directory inside an existing one, for the picker's
                # "New folder" button (destination pickers only). Name is a
                # single component - no separators, no traversal.
                parent = (body.get("parent") or "").strip()
                name = (body.get("name") or "").strip()
                if not parent or not name:
                    self._json({"ok": False, "error": "parent and name are required"}, status=400)
                    return
                if "/" in name or "\\" in name or "\x00" in name or name in (".", ".."):
                    self._json({"ok": False, "error": "folder name can't contain a path separator"}, status=400)
                    return
                try:
                    parent_p = Path(parent).expanduser().resolve()
                except OSError as e:
                    self._json({"ok": False, "error": str(e)}, status=400)
                    return
                if not parent_p.is_dir():
                    self._json({"ok": False, "error": f"not a directory: {parent_p}"}, status=400)
                    return
                new_p = parent_p / name
                try:
                    new_p.mkdir(exist_ok=True)  # exist_ok: an existing dir just gets navigated into
                except OSError as e:
                    self._json({"ok": False, "error": str(e)}, status=400)
                    return
                self._json({"ok": True, "path": str(new_p)})
                return

            if path == "/api/set-root":
                raw = body.get("path")
                if not raw:
                    self._json({"ok": False, "error": "no path given"}, status=400)
                    return
                new_root = Path(raw).expanduser().resolve()
                if not new_root.is_dir():
                    self._json({"ok": False, "error": f"not a directory: {new_root}"})
                    return

                started = start_scan(state, new_root)
                if not started:
                    self._json({"ok": False, "error": "a job is already running"}, status=409)
                    return
                self._json({"ok": True, "started": True})
                return

            if path == "/api/nd/decide":
                with state.lock:
                    if state.root is None:
                        self._json({"ok": False, "error": "no directory chosen"}, status=400)
                        return
                    gid = body.get("group_id")
                    if body.get("skip"):
                        state.decisions[gid] = {"keep": [], "discard": [], "skipped": True, "decided_at": now_iso()}
                    else:
                        keep = body.get("keep", [])
                        discard = body.get("discard", [])
                        state.decisions[gid] = {"keep": keep, "discard": discard, "skipped": False,
                                                 "decided_at": now_iso()}
                    save_decisions(state.decisions_path, state.decisions)
                self._json({"ok": True})
                return

            if path == "/api/nd/reset":
                with state.lock:
                    if state.root is None:
                        self._json({"ok": False, "error": "no directory chosen"}, status=400)
                        return
                    state.decisions = {}
                    save_decisions(state.decisions_path, state.decisions)
                self._json({"ok": True})
                return

            if path == "/api/nd/rescan":
                root = self._require_root()
                if root is None:
                    return
                # Same background scan used at startup/set-root - re-walks
                # the tree from scratch (picking up anything quarantined,
                # deleted, or newly added since the last scan) and restores
                # decisions for any group that's rediscovered unchanged, via
                # State.scan's own decisions.json snapshot/restore logic.
                started = start_scan(state, root)
                if not started:
                    self._json({"ok": False, "error": "a scan is already running"}, status=409)
                    return
                self._json({"ok": True, "started": True})
                return

            if path == "/api/review/build":
                root = self._require_root()
                if root is None:
                    return
                ops = [o for o in body.get("ops", []) if o in OP_NAMES]
                prefer = body.get("prefer", "oldest")
                rename_conflicts = bool(body.get("rename_conflicts"))
                delete_duplicates = bool(body.get("delete_duplicates"))
                case_style = body.get("case_style", "lower")
                sep_style = body.get("sep_style", "none")
                if not ops:
                    self._json({"ok": False, "error": "no operations selected"}, status=400)
                    return
                if not self._valid_styles(case_style, sep_style):
                    return
                phase_count = sum(2 if o == "normalise" else 1 for o in ops) or 1
                started = start_job("build", phase_count,
                                     lambda prog: do_build_review(root, ops, prefer, rename_conflicts,
                                                                   delete_duplicates, state, prog,
                                                                   case_style, sep_style))
                if not started:
                    self._json({"ok": False, "error": "a job is already running"}, status=409)
                    return
                self._json({"ok": True, "started": True})
                return

            if path == "/api/review/run":
                root = self._require_root()
                if root is None:
                    return
                ops = [o for o in body.get("ops", []) if o in OP_NAMES]
                prefer = body.get("prefer", "oldest")
                rename_conflicts = bool(body.get("rename_conflicts"))
                delete_duplicates = bool(body.get("delete_duplicates"))
                case_style = body.get("case_style", "lower")
                sep_style = body.get("sep_style", "none")
                if not ops:
                    self._json({"ok": False, "error": "no operations selected"}, status=400)
                    return
                if not self._valid_styles(case_style, sep_style):
                    return
                phase_count = sum(2 if o == "normalise" else 1 for o in ops) + 1
                started = start_job("run", phase_count,
                                     lambda prog: do_run(root, ops, prefer, rename_conflicts,
                                                          delete_duplicates, state, prog,
                                                          case_style, sep_style))
                if not started:
                    self._json({"ok": False, "error": "a job is already running"}, status=409)
                    return
                self._json({"ok": True, "started": True})
                return

            if path == "/api/upscale/run":
                root = self._require_root()
                if root is None:
                    return
                tool_error = upscale.tool_status()
                if tool_error:
                    self._json({"ok": False, "error": tool_error}, status=400)
                    return
                try:
                    target = int(body.get("target"))
                except (TypeError, ValueError):
                    self._json({"ok": False, "error": "invalid target resolution"}, status=400)
                    return
                if not (upscale.MIN_TARGET <= target <= upscale.MAX_TARGET):
                    self._json({"ok": False, "error": f"target must be between {upscale.MIN_TARGET} and {upscale.MAX_TARGET}"}, status=400)
                    return

                affix = str(body.get("affix", upscale.DEFAULT_AFFIX))
                affix_pos = str(body.get("affix_pos", "suffix"))
                if affix_pos not in upscale.AFFIX_POSITIONS:
                    self._json({"ok": False, "error": "affix_pos must be 'prefix' or 'suffix'"}, status=400)
                    return
                if "/" in affix or "\\" in affix or "\x00" in affix:
                    self._json({"ok": False, "error": "filename affix can't contain a path separator"}, status=400)
                    return

                overwrite = bool(body.get("overwrite"))

                out_raw = (body.get("out_dir") or "").strip()
                out_dir = None
                if out_raw:
                    try:
                        out_dir = Path(out_raw).expanduser().resolve()
                        out_dir.mkdir(parents=True, exist_ok=True)
                    except OSError as e:
                        self._json({"ok": False, "error": f"can't use that output directory: {e}"}, status=400)
                        return
                # Guard against writing over the originals: only possible
                # when output lands in the source location AND the affix is
                # empty, so nothing distinguishes the copy from its source.
                if not affix and (out_dir is None or out_dir == root):
                    self._json({"ok": False, "error": "set a filename prefix/suffix, or a separate output directory, so upscaled files don't overwrite the originals"}, status=400)
                    return

                def work(prog):
                    excludes = {root / QUARANTINE_DIRNAME, root / REVIEW_DIRNAME}
                    # Fresh recompute of the eligible list at execute time
                    # (same reasoning as Identical Files/Normalisation's
                    # Start) - the tree can change between when the list was
                    # last shown and when Start is actually clicked.
                    def cb(rel, i, total):
                        prog.phase_tick(0, f"Upscaling: {rel}", i, total)
                    return upscale.run_upscale(root, excludes, target, out_dir=out_dir,
                                               affix=affix, affix_pos=affix_pos,
                                               overwrite=overwrite, on_progress=cb)

                started = start_job("upscale", 1, work)
                if not started:
                    self._json({"ok": False, "error": "a job is already running"}, status=409)
                    return
                self._json({"ok": True, "started": True})
                return

            if path == "/api/upscale/install":
                # Downloads the portable realesrgan-ncnn-vulkan build into a
                # user-local dir (no sudo, nothing system-wide). Progress and
                # the live log stream through the shared job tracker, same as
                # any other job - the Upscale tab shows both.
                if upscale.resolve_tool() is not None:
                    self._json({"ok": False, "error": "realesrgan-ncnn-vulkan is already installed"}, status=400)
                    return

                def work(prog):
                    def on_bytes(done, total):
                        prog.phase_tick(0, "Downloading realesrgan-ncnn-vulkan", done, total or 1)
                    prog.log_line("Starting realesrgan-ncnn-vulkan download")
                    upscale.install_tool(on_log=prog.log_line, on_bytes=on_bytes)
                    return {"installed": True}

                started = start_job("upscale-install", 1, work)
                if not started:
                    self._json({"ok": False, "error": "a job is already running"}, status=409)
                    return
                self._json({"ok": True, "started": True})
                return

            if path == "/api/quarantine/delete":
                root = self._require_root()
                if root is None:
                    return
                quarantine_dir = root / QUARANTINE_DIRNAME
                if not quarantine_dir.exists():
                    self._json({"ok": True, "removed_files": 0})
                    return
                count = 0
                for dirpath, dirnames, filenames in os.walk(quarantine_dir):
                    count += sum(1 for fn in filenames if fn != MANIFEST_NAME)
                try:
                    shutil.rmtree(quarantine_dir)
                except OSError as e:
                    self._json({"ok": False, "error": str(e)})
                    return
                self._json({"ok": True, "removed_files": count})
                return

            if path == "/api/quarantine/restore":
                root = self._require_root()
                if root is None:
                    return
                quarantine_dir = root / QUARANTINE_DIRNAME
                # "paths" omitted (key absent) restores everything restorable;
                # an explicit list (possibly empty) restores just those
                # entries - matches new_path as returned by
                # /api/quarantine/status's "path" field for each file.
                paths = body.get("paths")
                only = set(paths) if paths is not None else None
                result = restore_manifest_entries(root, quarantine_dir, only_new_paths=only)
                self._json({
                    "ok": True,
                    "restored": result["restored"],
                    "missing": [m["original_path"] for m in result["missing"]],
                    "conflicts": [c["original_path"] for c in result["conflicts"]],
                })
                return

            self.send_error(404)

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", nargs="?", default=None,
                     help="Directory to scan (recurses into all subdirectories). If omitted, "
                          "the page opens a directory picker seeded at your home directory.")
    ap.add_argument("--threshold", type=int, default=8,
                     help="Max Hamming distance (out of 64) for Visually Similar grouping (default: 8)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--ext", nargs="+", default=sorted(DEFAULT_EXTENSIONS),
                     help=f"Image extensions considered by Visually-Similar/Identical-Files scanning "
                          f"(default: {sorted(DEFAULT_EXTENSIONS)}). Normalisation always considers "
                          f"all files, matching the CLI.")
    ap.add_argument("--no-browser", action="store_true", help="Don't try to auto-open a browser tab")
    args = ap.parse_args()

    extensions = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in args.ext}
    state = State(args.threshold, extensions)

    handler = make_handler(state)
    server = QuietHTTPServer(("127.0.0.1", args.port), handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Serving at {url} (Ctrl-C to stop)")

    upscale_missing = upscale.tool_status()
    if upscale_missing:
        print(f"Note: {upscale_missing}")
        print("      Every tab except Upscale works without it; the Upscale tab "
              "has a one-click Download button.")

    if args.root is not None:
        root = Path(args.root).resolve()
        if not root.is_dir():
            print(f"Not a directory: {root}", file=sys.stderr)
            sys.exit(1)
        # Runs as a background job, same as the picker's "Use this
        # directory" - the server below starts listening immediately
        # instead of blocking until the whole scan finishes.
        start_scan(state, root)
    else:
        print(f"No directory given - the page will open a picker seeded at {Path.home()}")

    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
