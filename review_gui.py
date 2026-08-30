#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pillow"]
# ///
"""
Dragoshi's Super Duper Image De-Duper - local browser GUI wrapping every
operation in this project: exact-hash dedupe ("Identical Files"),
directory merging + lowercase normalization combined ("Normalisation"),
and interactive perceptual near-duplicate review ("Visually Similar").

Menu: Operations (the three tabs above, each read-only/decision-only -
inspect or decide, never runs anything itself) | Jobs (Pending Jobs: pick
any mix of the three operations, review one combined summary, then Start)
| Quarantine (what's parked in _duplicates_quarantine/, with a
permanent-delete option once you're happy).

Nothing is deleted by a dedupe/merge/rename action itself - everything
that would be removed is moved into _duplicates_quarantine/ with an entry
in the shared dedupe_manifest.json, so dedupe_images.py --restore undoes
any of it. Two explicit, separately-warned opt-ins break that safety net
on purpose: the Jobs tab's "skip quarantine" checkbox permanently deletes
duplicates immediately instead of quarantining them (still logged in the
manifest as an audit trail, but --restore can't act on it), and the
Quarantine tab's delete button permanently empties the quarantine folder.
Both require an explicit tick/confirmation and say plainly that they
cannot be undone.

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
    DEFAULT_EXTENSIONS, MANIFEST_NAME, QUARANTINE_DIRNAME, execute_file_dedupe,
    human, load_manifest, plan_and_maybe_execute_dir_merge,
    plan_and_maybe_execute_lowercase, plan_file_dedupe, prune_empty_dirs,
)
from find_near_duplicates import compute_hashes, group_by_hash, group_confidence, iter_images
from apply_review import apply_plan, build_apply_plan

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
    """Adds a "size" (bytes) field to each action dict, best-effort."""
    for a in actions:
        try:
            a["size"] = (root / a["src"]).stat().st_size
        except OSError:
            a["size"] = 0
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
                "result": self.done_result,
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

        hashes = compute_hashes(files, root, on_progress=hash_cb)
        ordered = group_by_hash(hashes, self.threshold)
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
        def cb(phase, i, total):
            idx = 0 if phase == "hashing" else 1
            prog.phase_tick(idx, phase, i, total)
        state.scan(root, on_progress=cb)
        with state.lock:
            return {"groups_count": len(state.groups)}

    return start_job("scan", phase_count=2, work_fn=work, prog=scan_progress, lock=scan_lock)


# ---------------------------------------------------------------------------
# Combined pending-job building + running
# ---------------------------------------------------------------------------

def build_identical_items(root: Path, prefer: str, delete_duplicates: bool) -> list[dict]:
    quarantine_dir = root / QUARANTINE_DIRNAME
    plan = plan_file_dedupe(root, DEFAULT_EXTENSIONS, quarantine_dir, prefer)
    action = "delete" if delete_duplicates else "quarantine"
    items = []
    for entry in plan:
        keep_rel = str(entry["keep"].relative_to(root))
        for m in entry["move"]:
            sz = m.stat().st_size if m.exists() else 0
            items.append({"op": "identical", "action": action, "path": str(m.relative_to(root)),
                          "dest": None, "kept": keep_rel, "size": sz})
    return items


def build_dirmerge_items(root: Path, rename_conflicts: bool, delete_duplicates: bool) -> list[dict]:
    quarantine_dir = root / QUARANTINE_DIRNAME
    actions = plan_and_maybe_execute_dir_merge(root, quarantine_dir, False, [],
                                                delete_duplicates=delete_duplicates,
                                                rename_conflicts=rename_conflicts)
    enrich_actions(root, actions)
    return [{"op": "normalise", "action": a["type"], "path": a["src"],
             "dest": a["dest"], "kept": a["kept"], "size": a["size"]} for a in actions]


def build_lowercase_items(root: Path, rename_conflicts: bool, delete_duplicates: bool) -> list[dict]:
    quarantine_dir = root / QUARANTINE_DIRNAME
    stats = plan_and_maybe_execute_lowercase(root, quarantine_dir, False, [],
                                              delete_duplicates=delete_duplicates,
                                              rename_conflicts=rename_conflicts)
    actions = enrich_actions(root, stats["actions"])
    return [{"op": "normalise", "action": a["type"], "path": a["src"],
             "dest": a["dest"], "kept": a["kept"], "size": a["size"]} for a in actions]


def build_visual_items(root: Path, decisions: dict, delete_duplicates: bool) -> list[dict]:
    plan, _ = build_apply_plan(root, decisions)
    action = "delete" if delete_duplicates else "quarantine"
    items = []
    for keep_list, discard_rel, dpath in plan:
        sz = dpath.stat().st_size if dpath.exists() else 0
        items.append({"op": "visual", "action": action, "path": discard_rel,
                      "dest": None, "kept": ", ".join(keep_list) or None, "size": sz})
    return items


def do_build_review(root: Path, ops: list[str], prefer: str, rename_conflicts: bool,
                     delete_duplicates: bool, state: State, prog: Progress) -> dict:
    ordered_ops = [o for o in OP_ORDER if o in ops]
    phase_count = sum(2 if o == "normalise" else 1 for o in ordered_ops) or 1

    items = []
    idx = 0
    for op in ordered_ops:
        if op == "identical":
            prog.phase_tick(idx, OP_NAMES[op], 0, 1)
            items.extend(build_identical_items(root, prefer, delete_duplicates))
            prog.phase_tick(idx, OP_NAMES[op], 1, 1)
            idx += 1
        elif op == "normalise":
            prog.phase_tick(idx, "Normalisation: directory merge", 0, 1)
            items.extend(build_dirmerge_items(root, rename_conflicts, delete_duplicates))
            prog.phase_tick(idx, "Normalisation: directory merge", 1, 1)
            idx += 1
            prog.phase_tick(idx, "Normalisation: lowercase names", 0, 1)
            items.extend(build_lowercase_items(root, rename_conflicts, delete_duplicates))
            prog.phase_tick(idx, "Normalisation: lowercase names", 1, 1)
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
           state: State, prog: Progress) -> dict:
    ordered_ops = [o for o in OP_ORDER if o in ops]
    phase_count = sum(2 if o == "normalise" else 1 for o in ordered_ops) + 1  # +1 cleanup phase
    result = {"identical": None, "normalise": None, "visual": None}

    def go(manifest):
        idx = 0
        for op in ordered_ops:
            if op == "identical":
                prog.phase_tick(idx, f"Running: {OP_NAMES[op]}", 0, 1)
                quarantine_dir = root / QUARANTINE_DIRNAME
                plan = plan_file_dedupe(root, DEFAULT_EXTENSIONS, quarantine_dir, prefer)
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
                prog.phase_tick(idx, "Running: Normalisation (lowercase names)", 0, 1)
                lc_stats = plan_and_maybe_execute_lowercase(root, quarantine_dir, True, manifest,
                                                             delete_duplicates=delete_duplicates,
                                                             rename_conflicts=rename_conflicts)
                prog.phase_tick(idx, "Running: Normalisation (lowercase names)", 1, 1)
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
  .spinner { padding:24px; text-align:center; color:var(--text-dim); }

  table.plan { width:100%; border-collapse:collapse; font-size:13px; background:var(--surface); border:1px solid var(--border); border-radius:var(--radius); overflow:hidden; }
  table.plan th { text-align:left; color:var(--text-faint); font-weight:700; font-size:11px; letter-spacing:.04em; text-transform:uppercase; padding:9px 12px; border-bottom:1px solid var(--border); }
  table.plan td { padding:8px 12px; border-bottom:1px solid var(--border); vertical-align:top; }
  table.plan tr:last-child td { border-bottom:none; }
  table.plan tr.conflict td { color:var(--danger); }
  table.plan tr.move td { color:#7fa8f5; }
  table.plan tr.delete td { color:#ff8f93; }
  table.plan tr.rename td { color:var(--success); }
  .keep-badge { color:var(--success); font-weight:700; font-size:11px; text-transform:uppercase; }
  .dup-count { color:var(--text-faint); font-size:12px; }

  .group-block { margin-bottom:16px; border:1px solid var(--border); border-radius:var(--radius); overflow:hidden; }
  .group-block .gh { background:var(--surface-hover); padding:9px 14px; font-size:12px; color:var(--text-dim); font-weight:700; }

  /* ---------- pending toggle (on Operations tabs) ---------- */
  .pending-toggle { display:flex; align-items:center; gap:10px; padding:12px 14px; background:var(--accent-bg); border:1px solid var(--accent-border); border-radius:var(--radius); margin-bottom:18px; }
  .pending-toggle.disabled { background:var(--surface); border-color:var(--border); opacity:.7; }
  .pending-toggle label { font-weight:700; font-size:13.5px; cursor:pointer; }
  .pending-toggle.disabled label { cursor:not-allowed; color:var(--text-dim); }
  .pending-toggle .pending-note { color:var(--text-dim); font-size:12.5px; margin-left:auto; }

  /* ---------- visually similar review ---------- */
  #progress-track { flex:1; min-width:160px; }
  .bar { height:8px; background:var(--border); border-radius:4px; overflow:hidden; }
  .bar-fill { height:100%; background:var(--success); transition:width .2s; }
  #progress-label { font-size:12px; color:var(--text-dim); margin-top:5px; }
  #groupmeta { color:var(--text-dim); font-size:13px; margin:16px 0 14px; }
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
  #picker-path { padding:16px 18px; border-bottom:1px solid var(--border); font-size:13px; color:var(--text-dim); word-break:break-all; }
  #picker-list { overflow-y:auto; flex:1; padding:6px; }
  #picker-list button { display:block; width:100%; text-align:left; background:none; border:none; color:var(--text); padding:9px 11px; border-radius:var(--radius-sm); cursor:pointer; font-size:13px; }
  #picker-list button:hover { background:var(--surface-hover); }
  #picker-list .up { color:var(--accent); font-weight:600; }
  #picker-error { color:var(--danger); font-size:12px; padding:0 18px 10px; }
  #progress-overlay .modal { width:min(480px, 90vw); }
  #progress-overlay .modal-body { text-align:center; padding:28px 18px; }
  #progress-overlay .big-pct { font-size:34px; font-weight:800; margin-bottom:12px; color:var(--accent); }

  /* scan overlay: lighter/blurred backdrop than the standard modals so the
     webui reads as "about to be ready" behind it, not fully hidden */
  #scan-overlay { background:rgba(8,9,11,.45); backdrop-filter:blur(2px); -webkit-backdrop-filter:blur(2px); }
  #scan-overlay .modal { width:min(420px, 90vw); }
  #scan-overlay .modal-body { text-align:center; padding:34px 26px; }
  .scan-spinner {
    width:54px; height:54px; margin:0 auto 22px; border-radius:50%;
    background:conic-gradient(from 0deg, var(--accent) 0deg, transparent 300deg);
    -webkit-mask:radial-gradient(farthest-side, transparent calc(100% - 5px), #000 calc(100% - 5px));
    mask:radial-gradient(farthest-side, transparent calc(100% - 5px), #000 calc(100% - 5px));
    animation:scan-spin .85s linear infinite;
  }
  @keyframes scan-spin { to { transform:rotate(360deg); } }
  #scan-status-text { font-size:14.5px; font-weight:600; color:var(--text); margin-bottom:4px; }
  #scan-pct { font-size:12px; color:var(--text-dim); margin-top:8px; }

  /* ---------- bottom status bar (background scan/scoring) ---------- */
  #status-bar {
    position:fixed; left:0; right:0; bottom:0; z-index:15;
    background:var(--bg-elevated); border-top:1px solid var(--border-strong);
    padding:10px 20px; transform:translateY(100%); transition:transform .25s ease;
  }
  #status-bar.open { transform:translateY(0); }
  #status-bar-inner { display:flex; align-items:center; gap:14px; font-size:13px; color:var(--text-dim); max-width:1400px; margin:0 auto; }
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
  .thumb-row .thumb { width:52px; height:52px; object-fit:cover; border-radius:6px; background:var(--bg); flex-shrink:0; }
  .thumb-row .thumb.placeholder { display:flex; align-items:center; justify-content:center; color:var(--text-faint); font-size:10px; }
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
  .warn-box { background:var(--danger-bg); border:1px solid var(--danger-border); border-radius:var(--radius); padding:14px 16px; color:#f2b6b9; font-size:13px; margin-bottom:12px; }
  .warn-box b { color:#ffcdcf; }

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
  <button data-tab="jobs">Jobs</button>
  <button data-tab="quarantine">Quarantine</button>
</div>
<div id="subtabs">
  <button data-subtab="identical" class="active">Identical Files</button>
  <button data-subtab="visual">Visually Similar</button>
  <button data-subtab="normalise">Normalisation</button>
</div>
<main>
  <div id="tab-operations" class="tabpanel active">
    <div id="sub-identical" class="subpanel"></div>
    <div id="sub-visual" class="subpanel" style="display:none"></div>
    <div id="sub-normalise" class="subpanel" style="display:none"></div>
  </div>
  <div id="tab-jobs" class="tabpanel"></div>
  <div id="tab-quarantine" class="tabpanel"></div>
</main>

<div id="picker-overlay" class="overlay">
  <div class="modal">
    <div id="picker-path"></div>
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
    </div>
  </div>
</div>

<div id="scan-overlay" class="overlay">
  <div class="modal">
    <div class="modal-body">
      <div class="scan-spinner"></div>
      <div id="scan-status-text">Comparing and grouping hashes&hellip;</div>
      <div class="bar"><div class="bar-fill" id="scan-fill" style="width:0%"></div></div>
      <div id="scan-pct">0%</div>
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

const OP_LABELS = {identical: 'Identical Files', normalise: 'Normalisation', visual: 'Visually Similar'};

// ---------- shared: progress polling ----------

function showProgressOverlay(title) {
  document.getElementById('progress-title').textContent = title;
  document.getElementById('progress-pct').textContent = '0%';
  document.getElementById('progress-fill').style.width = '0%';
  document.getElementById('progress-label').textContent = '';
  document.getElementById('progress-overlay').classList.add('open');
}
function hideProgressOverlay() {
  document.getElementById('progress-overlay').classList.remove('open');
}

async function pollUntilDone() {
  while (true) {
    const r = await fetch('/api/progress');
    const p = await r.json();
    document.getElementById('progress-pct').textContent = p.pct + '%';
    document.getElementById('progress-fill').style.width = p.pct + '%';
    document.getElementById('progress-label').textContent =
      p.phase ? `${p.phase} (${p.current}/${p.total})` : '';
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
    alert('Error: ' + final.error);
    return null;
  }
  return final.result;
}

// ---------- background directory scan (hash+group, then incremental scoring) ----------
//
// Hashing+grouping (phase_index 0) has nothing to show yet - Hamming-
// distance grouping needs every hash before it can form any cluster - so
// that part still gets a blocking modal (a light/blurred backdrop rather
// than the usual opaque one, so the webui reads as "about to be ready").
// The moment scoring starts (phase_index 1) the modal closes and a
// persistent bottom status bar takes over instead, because scoring is
// genuinely incremental: state.groups grows one group at a time on the
// server, so results can - and do - load in as they're ready.

let scanActive = false;   // read by renderVisual() to word its empty state
let scanPolling = false;  // re-entrancy guard - only one poll loop at a time

function showScanOverlay() {
  document.getElementById('scan-status-text').textContent = 'Comparing and grouping hashes…';
  document.getElementById('scan-fill').style.width = '0%';
  document.getElementById('scan-pct').textContent = '0%';
  document.getElementById('scan-overlay').classList.add('open');
}
function hideScanOverlay() {
  document.getElementById('scan-overlay').classList.remove('open');
}
function showStatusBar(text, pct) {
  document.getElementById('status-bar-text').textContent = text;
  document.getElementById('status-bar-fill').style.width = pct + '%';
  document.getElementById('status-bar').classList.add('open');
}
function hideStatusBar() {
  document.getElementById('status-bar').classList.remove('open');
}

// Re-fetches the current group list/decisions and, if new groups have
// arrived, re-renders - but only when that's actually safe: skip it while
// the user is mid-way through toggling the group they're currently looking
// at (ndTouched), so a background poll can never wipe an unsaved toggle.
async function refreshVisualGroupsQuietly() {
  const r = await fetch('/api/nd/groups');
  const data = await r.json();
  // was, and still is, showing the "nothing yet" screen - re-render even
  // without new groups so its "still scanning" wording stays current
  const stillEmpty = ndGroups.length === 0 && data.groups.length === 0;
  const grew = data.groups.length !== ndGroups.length;
  ndGroups = data.groups;
  ndDecisions = data.decisions;
  if ((grew || stillEmpty) && activeTab === 'operations' && activeSubtab === 'visual' && !ndTouched) {
    renderVisual();
  }
}

async function pollScan() {
  if (scanPolling) return;
  scanPolling = true;
  try {
    while (true) {
      const r = await fetch('/api/scan-progress');
      const p = await r.json();
      scanActive = p.active;
      if (!p.active && !p.done) break;  // no scan has ever run this session
      if (p.active && p.phase_index === 0) {
        hideStatusBar();
        showScanOverlay();
        document.getElementById('scan-fill').style.width = p.pct + '%';
        document.getElementById('scan-pct').textContent = p.pct + '%';
      } else {
        hideScanOverlay();
        if (p.active) {
          showStatusBar(`Scoring images for review: ${plural(p.current, 'group')} ready`, p.pct);
        } else {
          showStatusBar('Scan complete', 100);
        }
        await refreshVisualGroupsQuietly();
      }
      if (p.done) {
        hideScanOverlay();
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

function openPicker() {
  document.getElementById('picker-overlay').classList.add('open');
  browseTo(currentRoot || null);
}
function closePicker() {
  document.getElementById('picker-overlay').classList.remove('open');
}

document.getElementById('change-dir').onclick = openPicker;
document.getElementById('picker-cancel').onclick = () => { if (currentRoot) closePicker(); };
document.getElementById('picker-use').onclick = async () => {
  if (!browsePath) return;
  const chosen = browsePath;
  closePicker();
  const r = await fetch('/api/set-root', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({path: chosen})
  });
  const started = await r.json();
  if (!started.ok) { alert(started.error || 'could not start scan'); return; }
  pollScan();  // not awaited - runs in the background (scan overlay, then
               // the status bar) while the rest of the UI becomes usable
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
};

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
    return;
  }
  if (activeTab === 'operations') {
    if (activeSubtab === 'identical') loadIdentical();
    if (activeSubtab === 'visual') loadVisual();
    if (activeSubtab === 'normalise') loadNormalise();
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
    if (activeTab === 'jobs') loadJobs();
  };
}

// ---------- Visually Similar sub-tab ----------

let ndGroups = [], ndDecisions = {}, ndIdx = 0;
let ndPending = {};
let ndTouched = false;  // true once the current group's keep/discard has been changed since it was shown

async function loadVisual() {
  const panel = document.getElementById('sub-visual');
  panel.innerHTML = '<div class="spinner">Loading&hellip;</div>';
  const r = await fetch('/api/nd/groups');
  const data = await r.json();
  ndGroups = data.groups;
  ndDecisions = data.decisions;
  ndIdx = ndGroups.findIndex(g => !ndDecisions[g.id]);
  if (ndIdx === -1) ndIdx = 0;
  renderVisual();
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

// Saves the current group's keep/discard split (same save this group would
// get from pressing Enter) if it's been changed since it was shown - used
// so navigating away with the arrow keys/prev/next doesn't silently
// discard toggles you made but didn't explicitly confirm.
async function ndSaveIfTouched() {
  if (!ndTouched) return;
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
        loadVisual();
      },
      { confirmLabel: 'Reset' });
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

  if (ndGroups.length === 0) {
    const emptyMsg = scanActive
      ? 'Still scanning for candidate groups&hellip; hang tight, groups will appear here as they\'re scored.'
      : 'No candidate groups found in this directory.';
    panel.innerHTML = `<h2 class="page-title">Visually Similar</h2>${toggle}<div class="empty">${emptyMsg}</div>`;
    wirePendingToggle('visual');
    return;
  }
  const decidedCount = Object.keys(ndDecisions).length;
  const resetRow = ndResetRowHtml(decidedCount);
  if (decidedCount === ndGroups.length) {
    panel.innerHTML = `<h2 class="page-title">Visually Similar</h2>${toggle}${resetRow}<div class="empty">All groups reviewed! ${plural(discardCount, 'file')} staged to discard.<br><span class="empty-hint">Add to Pending Jobs above, then go to the Jobs tab to run it.</span></div>`;
    wirePendingToggle('visual');
    wireNdReset();
    return;
  }
  const g = ndGroups[ndIdx];
  if (!g) return;
  ndStartPending(g);

  const existing = ndDecisions[g.id];
  let html = `<h2 class="page-title">Visually Similar</h2>${toggle}${resetRow}
    <div id="progress-track"><div class="bar"><div class="bar-fill" style="width:${100*decidedCount/ndGroups.length}%"></div></div>
    <div id="progress-label">${decidedCount} / ${ndGroups.length} decided &mdash; viewing group ${ndIdx+1}</div></div>
    <div id="groupmeta">Group ${ndIdx+1} of ${ndGroups.length} &middot; avg. hash distance ${g.avg_distance}` +
    (existing ? existing.skipped ? ' &middot; <b>skipped</b>' : ` &middot; kept <b>${existing.keep.length}</b> / discarded <b>${existing.discard.length}</b>` : '') +
    `</div><div id="cards-wrap">
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
      <button class="btn btn-primary" id="nd-confirm">Confirm &amp; next (Enter)</button>
      <button class="btn" id="nd-skip">Skip (S)</button>
      <button class="btn" id="nd-prev">&larr; prev</button>
      <button class="btn" id="nd-next">next &rarr;</button>
      <span class="hint">Click an image, or press its number, to toggle keep/discard. More than one can be kept.</span>
    </footer>`;
  panel.innerHTML = html;
  wirePendingToggle('visual');
  wireNdReset();

  panel.querySelectorAll('.card').forEach(card => {
    // suppressed after a click-and-drag carousel scroll (see
    // wireCardsDrag) so releasing a drag over a card doesn't also toggle it
    card.addEventListener('click', () => { if (!cardsDragMoved) ndToggle(card.dataset.path); });
  });
  document.getElementById('nd-confirm').onclick = ndConfirm;
  document.getElementById('nd-skip').onclick = ndSkip;
  document.getElementById('nd-prev').onclick = async () => { await ndSaveIfTouched(); ndIdx = Math.max(0, ndIdx - 1); renderVisual(); };
  document.getElementById('nd-next').onclick = async () => { await ndSaveIfTouched(); ndIdx = Math.min(ndGroups.length - 1, ndIdx + 1); renderVisual(); };
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

function wireCardsDrag(track) {
  const DRAG_THRESHOLD = 6;  // px of movement before a press counts as a drag, not a click
  let dragging = false;
  let startX = 0, startScrollLeft = 0;
  let samples = [];  // trailing {x, t} window, for velocity at release

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

  function endDrag(e) {
    if (!dragging) return;
    dragging = false;
    track.classList.remove('dragging');
    try { track.releasePointerCapture(e.pointerId); } catch (_) { /* already released */ }
    if (!cardsDragMoved) { track.style.scrollBehavior = ''; return; }
    const first = samples[0], last = samples[samples.length - 1];
    const dt = last.t - first.t;
    let velocity = dt > 0 ? (last.x - first.x) / dt : 0;  // px/ms, same sign convention as dx above
    const MAX_VELOCITY = 3;  // sanity clamp against a noisy single-frame spike
    velocity = Math.max(-MAX_VELOCITY, Math.min(MAX_VELOCITY, velocity));
    startMomentum(track, velocity);
  }
  track.addEventListener('pointerup', endDrag);
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
  // number-key toggles can target a card scrolled out of the carousel's
  // view - bring it into view so the state change is actually visible.
  // A no-op (no scrolling) if it's already fully in view.
  card.scrollIntoView({ behavior: 'smooth', inline: 'nearest', block: 'nearest' });
}

async function ndSaveDecision(gid, keep, discard, skipped) {
  ndDecisions[gid] = {keep, discard, skipped, decided_at: new Date().toISOString()};
  await fetch('/api/nd/decide', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({group_id: gid, keep, discard, skip: skipped})
  });
}

async function ndConfirm() {
  const g = ndGroups[ndIdx];
  const keep = g.images.map(im => im.path).filter(p => ndPending[p] !== 'discard');
  const discard = g.images.map(im => im.path).filter(p => ndPending[p] === 'discard');
  await ndSaveDecision(g.id, keep, discard, false);
  ndIdx = ndFirstUndecidedFrom(ndIdx + 1);
  if (ndIdx === -1) ndIdx = ndGroups.length;
  renderVisual();
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
  if (e.key === 'Enter') { ndConfirm(); return; }
  if (e.key === 's' || e.key === 'S') { ndSkip(); return; }
  // Left/Right just browse by default, but if you've toggled anything for
  // this group without pressing Enter, save it first - same save Enter
  // would do - so navigating away with the arrow keys doesn't silently
  // lose those toggles.
  if (e.key === 'ArrowLeft') { await ndSaveIfTouched(); ndIdx = Math.max(0, ndIdx - 1); renderVisual(); return; }
  if (e.key === 'ArrowRight') { await ndSaveIfTouched(); ndIdx = Math.min(ndGroups.length - 1, ndIdx + 1); renderVisual(); return; }
  const n = parseInt(e.key, 10);
  if (!isNaN(n) && n >= 1 && g.images[n-1]) ndToggle(g.images[n-1].path);
});

// ---------- Identical Files sub-tab (preview only) ----------

let identicalPrefer = 'oldest';

async function loadIdentical() {
  const panel = document.getElementById('sub-identical');
  panel.innerHTML = '<div class="spinner">Scanning&hellip;</div>';
  const r = await fetch(`/api/identical/preview?prefer=${identicalPrefer}`);
  const data = await r.json();
  renderIdentical(data);
}

function renderIdentical(data) {
  const panel = document.getElementById('sub-identical');
  const toggle = pendingToggleHtml('identical', data.total_files === 0,
    data.total_files === 0 ? 'Nothing found' : `${plural(data.total_files, 'file')} would be quarantined`);
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
  html += '<table class="plan"><tr><th>Action</th><th>Path</th><th>Size</th></tr>';
  data.groups.forEach(g => {
    html += `<tr><td class="keep-badge">KEEP</td><td>${esc(g.keep)} <span class="dup-count">(${plural(g.move.length, 'duplicate')})</span></td><td>${g.keep_size_human}</td></tr>`;
  });
  html += '</table>';
  panel.innerHTML = html;
  document.getElementById('identical-prefer').value = identicalPrefer;
  document.getElementById('identical-prefer').onchange = (e) => { identicalPrefer = e.target.value; loadIdentical(); };
  document.getElementById('identical-rescan').onclick = loadIdentical;
  wirePendingToggle('identical');
}

// ---------- Normalisation sub-tab (directory merge + lowercase, preview only) ----------

let normaliseRenameConflicts = false;

async function loadNormalise() {
  const panel = document.getElementById('sub-normalise');
  panel.innerHTML = '<div class="spinner">Scanning&hellip;</div>';
  const r = await fetch(`/api/normalise/preview?rename_conflicts=${normaliseRenameConflicts}`);
  const data = await r.json();
  renderNormalise(data);
}

function renderNormalise(data) {
  const panel = document.getElementById('sub-normalise');
  const total = data.actions.length;
  const toggle = pendingToggleHtml('normalise', total === 0,
    total === 0 ? 'Nothing to do' : `${plural(total, 'action')} planned`);
  let html = `<h2 class="page-title">Normalisation</h2>
    <p class="page-sub">Merges duplicate-looking sibling directories (e.g. "Foo" + "Foo_1") and lowercases every file/directory name.</p>${toggle}
    <div class="checkbox-row"><input type="checkbox" id="normalise-rename-conflicts" ${normaliseRenameConflicts ? 'checked' : ''}>
      <label for="normalise-rename-conflicts">Rename conflicting file names instead of leaving them as unresolved conflicts</label></div>
    <div class="toolbar"><button class="btn" id="normalise-rescan">Rescan</button></div>`;
  if (data.actions.length === 0) {
    html += '<div class="empty">Nothing to normalise - no sibling directory duplicates, and everything is already lowercase.</div>';
    panel.innerHTML = html;
    wireNormaliseToolbar();
    return;
  }
  const c = data.counts;
  html += `<div class="summary">${plural(c.move||0,'file')} to move &middot; ${plural((c.quarantine||0)+(c.delete||0),'file')} to quarantine as duplicates &middot; ${plural(c.rename||0,'file')} renamed &middot; ${plural(c.conflict||0,'conflict')} left alone.</div>`;
  html += '<table class="plan"><tr><th>Action</th><th>Path</th><th>Size</th></tr>';
  data.actions.forEach(a => {
    const label = {move:'move', quarantine:'dup', delete:'delete', rename:'rename', conflict:'CONFLICT'}[a.type] || a.type;
    let path = esc(a.src);
    if (a.dest) path += ' &rarr; ' + esc(a.dest);
    if (a.kept) path += ` (kept: ${esc(a.kept)})`;
    html += `<tr class="${a.type}"><td>${label}</td><td>${path}</td><td>${human(a.size)}</td></tr>`;
  });
  html += '</table>';
  panel.innerHTML = html;
  wireNormaliseToolbar();
}

function wireNormaliseToolbar() {
  document.getElementById('normalise-rescan').onclick = loadNormalise;
  document.getElementById('normalise-rename-conflicts').onchange = (e) => {
    normaliseRenameConflicts = e.target.checked;
    loadNormalise();
  };
  wirePendingToggle('normalise');
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
    {prefer: identicalPrefer, rename_conflicts: normaliseRenameConflicts, delete_duplicates: skipQuarantine});
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
    const thumb = isImageExt(it.path)
      ? `<img class="thumb" src="/img/${encodeURIComponent(it.path)}" loading="lazy">`
      : `<div class="thumb placeholder">file</div>`;
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
    btn.onclick = () => { pendingOps[btn.dataset.cancelOp] = false; loadJobs(); };
  });
  const sq = document.getElementById('skip-quarantine');
  if (sq) sq.onchange = (e) => { skipQuarantine = e.target.checked; loadJobs(); };
}

function onStart() {
  const data = lastBuiltReview;
  if (!data) return;
  const actionable = data.items.filter(it => it.action !== 'conflict');
  let body;
  let confirmLabel = 'Start';
  let requireCheckbox = null;
  if (data.delete_duplicates) {
    body = `<div class="warn-box"><b>${plural(actionable.length, 'file')} will be PERMANENTLY DELETED</b>, not quarantined. ` +
      `This cannot be undone - there is no way to get these files back afterward.</div>` +
      `<p>${plural(data.ops.length, 'operation')} will run: ${data.ops.map(o => OP_LABELS[o]).join(', ')}.</p>` +
      `<label style="display:flex;gap:8px;align-items:center;margin-top:10px"><input type="checkbox" id="start-confirm-check"> I understand this permanently deletes files with no way to undo it.</label>`;
    confirmLabel = 'Delete permanently';
    requireCheckbox = 'start-confirm-check';
  } else {
    body = `<p>${plural(actionable.length, 'file')} across ${plural(data.ops.length, 'operation')} will be moved into <code>_duplicates_quarantine/</code> or renamed. ` +
      `Nothing is deleted - reversible with <code>dedupe_images.py --restore</code> until you delete the quarantine folder.</p>` +
      `<p><b>Note:</b> Identical Files / Normalisation plans are recomputed fresh at the moment this runs (not replayed from this summary), ` +
      `in case the folder changed since it was built - Visually Similar decisions are always applied exactly as you decided them.</p>`;
  }
  confirmAction('Start pending jobs', body, async () => {
    const result = await runJob('Running operations', '/api/review/run',
      {ops: data.ops, prefer: identicalPrefer, rename_conflicts: normaliseRenameConflicts, delete_duplicates: skipQuarantine});
    if (result === null) return;
    showRunResult(result);
    pendingOps = {identical: false, normalise: false, visual: false};
    skipQuarantine = false;
    lastBuiltReview = null;
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
  panel.innerHTML = '<div class="spinner">Checking&hellip;</div>';
  const r = await fetch('/api/quarantine/status');
  const data = await r.json();
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
  html += `<div class="warn-box">Deleting the quarantine folder is <b>permanent</b> - it cannot be undone, and <code>dedupe_images.py --restore</code> will no longer be able to bring ${data.file_count === 1 ? 'this file' : 'these files'} back. Only do this once you've confirmed everything looks right.</div>`;
  html += `<div class="toolbar"><button class="btn btn-danger" id="qt-delete">Delete quarantine folder permanently&hellip;</button></div>`;
  panel.innerHTML = html;

  document.getElementById('qt-delete').onclick = () => {
    const fileWord = data.file_count === 1 ? 'the file' : 'everything';
    confirmAction('Permanently delete quarantine folder',
      `<div class="warn-box">This will permanently delete ${plural(data.file_count, 'file')} (${data.total_size_human}) with no way to undo it. ` +
      `<code>dedupe_images.py --restore</code> will stop working for ${fileWord} currently in quarantine.</div>` +
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
  // picks up a scan already in progress - e.g. review_gui.py was launched
  // with a root argument, or this tab was reloaded mid-scan
  pollScan();
  await reloadActive();
  if (!currentRoot) openPicker();
})();
</script>
</body></html>
"""


def _normalise_preview_json(root: Path, rename_conflicts: bool) -> dict:
    quarantine_dir = root / QUARANTINE_DIRNAME
    dm_actions = plan_and_maybe_execute_dir_merge(root, quarantine_dir, False, [],
                                                    rename_conflicts=rename_conflicts)
    for a in dm_actions:
        a.pop("merge", None)
    lc_stats = plan_and_maybe_execute_lowercase(root, quarantine_dir, False, [],
                                                 rename_conflicts=rename_conflicts)
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
                plan = plan_file_dedupe(root, DEFAULT_EXTENSIONS, quarantine_dir, prefer)
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
                self._json(_normalise_preview_json(root, rename_conflicts))
                return

            if parsed.path == "/api/quarantine/status":
                root = self._require_root()
                if root is None:
                    return
                quarantine_dir = root / QUARANTINE_DIRNAME
                if not quarantine_dir.exists():
                    self._json({"exists": False, "file_count": 0, "total_size_human": "0.0B"})
                    return
                count = 0
                total = 0
                for dirpath, dirnames, filenames in os.walk(quarantine_dir):
                    for fn in filenames:
                        if fn == MANIFEST_NAME:
                            continue
                        try:
                            total += (Path(dirpath) / fn).stat().st_size
                        except OSError:
                            continue
                        count += 1
                self._json({"exists": True, "file_count": count, "total_size_human": human(total),
                             "path": str(quarantine_dir)})
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

            if path == "/api/review/build":
                root = self._require_root()
                if root is None:
                    return
                ops = [o for o in body.get("ops", []) if o in OP_NAMES]
                prefer = body.get("prefer", "oldest")
                rename_conflicts = bool(body.get("rename_conflicts"))
                delete_duplicates = bool(body.get("delete_duplicates"))
                if not ops:
                    self._json({"ok": False, "error": "no operations selected"}, status=400)
                    return
                phase_count = sum(2 if o == "normalise" else 1 for o in ops) or 1
                started = start_job("build", phase_count,
                                     lambda prog: do_build_review(root, ops, prefer, rename_conflicts,
                                                                   delete_duplicates, state, prog))
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
                if not ops:
                    self._json({"ok": False, "error": "no operations selected"}, status=400)
                    return
                phase_count = sum(2 if o == "normalise" else 1 for o in ops) + 1
                started = start_job("run", phase_count,
                                     lambda prog: do_run(root, ops, prefer, rename_conflicts,
                                                          delete_duplicates, state, prog))
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
