#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["pillow"]
# ///
"""
Report-only perceptual near-duplicate finder.

Exact-hash dedupe (dedupe_images.py) only catches byte-identical files -
it can't tell "same image, recompressed/resized/re-saved" from "unrelated
image", so a visually-identical pair that differs even by one byte sails
right through it. This script fills that gap using a perceptual hash
(dHash: shrink to grayscale, compare adjacent pixel brightness), which is
tolerant of recompression, resizing, and minor color/format changes.

This is deliberately NEVER destructive. Perceptual matching is fuzzy by
nature - two different pieces of art with a similar pose or palette can
hash close together, and this script has no way to tell "recompressed
copy" from "coincidentally similar". So it only ever REPORTS candidate
groups and builds a side-by-side comparison montage image for each one -
it does not move, rename, or delete anything. Deciding which groups are
genuine duplicates, and what to do about them, is left entirely to you;
once you've picked specific files, use dedupe_images.py's existing safe
quarantine mechanism (or just delete them yourself) to act on it.

Usage:
    uv run find_near_duplicates.py /mnt/dragonhoard/tuqiri/commissions
    uv run find_near_duplicates.py ROOT --threshold 6 --montage-dir OUT_DIR

--threshold is a Hamming distance out of 64 bits. Lower = fewer, more
confident matches. 0 means "perceptually identical after downsampling";
the default (8) allows for light recompression/resizing noise while
staying fairly conservative. Raise it to catch more candidates at the
cost of more false positives; you're the one reviewing the montages
either way, so it's safe to over-include and then discard by eye.
"""

import argparse
import itertools
import os
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ModuleNotFoundError:
    print(
        "error: this script needs Pillow, which the inline script metadata at the "
        "top of this file declares as a dependency - but that only gets installed "
        "automatically when run via uv, not plain python3.\n\n"
        "Run it like this instead:\n"
        "    uv run find_near_duplicates.py ...\n"
        "or, since it's executable:\n"
        "    ./find_near_duplicates.py ...\n",
        file=sys.stderr,
    )
    sys.exit(1)

DEFAULT_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff",
}
QUARANTINE_DIRNAME = "_duplicates_quarantine"


def dhash(path: Path, hash_size: int = 8) -> int:
    with Image.open(path) as img:
        img = img.convert("L").resize((hash_size + 1, hash_size), Image.LANCZOS)
        pixels = img.tobytes()  # mode "L" = 1 byte/pixel, row-major
    bits = 0
    for row in range(hash_size):
        offset = row * (hash_size + 1)
        for col in range(hash_size):
            bits = (bits << 1) | (1 if pixels[offset + col] > pixels[offset + col + 1] else 0)
    return bits


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def iter_images(root: Path, extensions: set[str], exclude_dirs: set[Path]):
    exclude_dirs = {d.resolve() for d in exclude_dirs}
    for dirpath, dirnames, filenames in os.walk(root):
        dp = Path(dirpath)
        if dp.resolve() in exclude_dirs:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if (dp / d).resolve() not in exclude_dirs]
        for name in filenames:
            p = dp / name
            if p.suffix.lower() in extensions:
                yield p


class UnionFind:
    def __init__(self, items):
        self.parent = {i: i for i in items}

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def compute_hashes(files: list[Path], root: Path, progress: bool = True, on_progress=None) -> dict[Path, int]:
    """on_progress(current, total), if given, is called after every file -
    lets a caller (e.g. a web UI) drive a real progress bar."""
    hashes: dict[Path, int] = {}
    for i, p in enumerate(files, 1):
        try:
            hashes[p] = dhash(p)
        except Exception as e:
            print(f"  ! could not hash {p.relative_to(root)}: {e}", file=sys.stderr)
        if progress and i % 100 == 0:
            print(f"  ...{i}/{len(files)}")
        if on_progress:
            on_progress(i, len(files))
    return hashes


def group_confidence(hashes: dict[Path, int], members: list[Path]) -> float:
    dists = [hamming(hashes[a], hashes[b]) for a, b in itertools.combinations(members, 2)]
    return sum(dists) / len(dists)


def group_by_hash(hashes: dict[Path, int], threshold: int) -> list[list[Path]]:
    """Union-find clustering: any pair within `threshold` Hamming distance
    joins the same group. Returns groups of size > 1, each sorted by path,
    ordered by ascending avg. pairwise distance (most confident match first)."""
    paths = list(hashes.keys())
    uf = UnionFind(paths)
    for a, b in itertools.combinations(paths, 2):
        if hamming(hashes[a], hashes[b]) <= threshold:
            uf.union(a, b)

    clusters: dict[Path, list[Path]] = {}
    for p in paths:
        clusters.setdefault(uf.find(p), []).append(p)
    groups = [sorted(members, key=str) for members in clusters.values() if len(members) > 1]
    groups.sort(key=lambda members: group_confidence(hashes, members))
    return groups


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def build_montage(paths: list[Path], root: Path, out_path: Path, thumb_h: int = 220):
    font = ImageFont.load_default()
    thumbs = []
    for p in paths:
        try:
            with Image.open(p) as img:
                img = img.convert("RGB")
                w = int(img.width * thumb_h / img.height)
                thumbs.append((p, img.resize((max(w, 1), thumb_h), Image.LANCZOS)))
        except Exception as e:
            print(f"  ! could not thumbnail {p}: {e}", file=sys.stderr)

    if not thumbs:
        return

    label_h = 34
    pad = 8
    total_w = sum(t.width for _, t in thumbs) + pad * (len(thumbs) + 1)
    total_h = thumb_h + label_h + pad * 2
    canvas = Image.new("RGB", (total_w, total_h), (30, 30, 30))
    draw = ImageDraw.Draw(canvas)

    x = pad
    for p, thumb in thumbs:
        canvas.paste(thumb, (x, pad))
        label = p.relative_to(root).as_posix()
        if len(label) > 40:
            label = label[:18] + "..." + label[-19:]
        draw.text((x, pad + thumb_h + 4), label, fill=(230, 230, 230), font=font)
        x += thumb.width + pad

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root", help="Root directory to scan (recurses into all subdirectories)")
    ap.add_argument("--threshold", type=int, default=8,
                     help="Max Hamming distance (out of 64) to consider a candidate match (default: 8)")
    ap.add_argument("--montage-dir", default=None,
                     help="Directory to write a comparison montage image per group into "
                          "(default: <root>/_near_duplicate_montages)")
    ap.add_argument("--no-montage", action="store_true", help="Skip building montage images, report only")
    ap.add_argument("--ext", nargs="+", default=sorted(DEFAULT_EXTENSIONS),
                     help=f"File extensions to consider (default: {sorted(DEFAULT_EXTENSIONS)})")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        sys.exit(1)
    quarantine_dir = root / QUARANTINE_DIRNAME
    extensions = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in args.ext}
    montage_dir = Path(args.montage_dir) if args.montage_dir else root / "_near_duplicate_montages"

    files = list(iter_images(root, extensions, {quarantine_dir, montage_dir}))
    print(f"Scanning {len(files)} image(s) under {root} ...")

    hashes = compute_hashes(files, root)
    print(f"Hashed {len(hashes)} image(s). Comparing pairs (threshold {args.threshold}) ...")

    ordered = group_by_hash(hashes, args.threshold)

    if not ordered:
        print("No perceptual near-duplicates found at this threshold.")
        return

    print()
    print(f"Found {len(ordered)} candidate group(s). Lower avg. distance = more likely a true match.")
    print("This is fuzzy matching - review the montage images yourself before acting on anything.")
    print()

    montage_paths = []
    for gi, members in enumerate(ordered, 1):
        avg_dist = group_confidence(hashes, members)
        print(f"[group {gi}]  avg distance {avg_dist:.1f}")
        for p in members:
            try:
                size = p.stat().st_size
                with Image.open(p) as img:
                    dims = f"{img.width}x{img.height}"
            except Exception:
                size, dims = 0, "?"
            print(f"    {p.relative_to(root)}  ({dims}, {human(size)})")

        if not args.no_montage:
            out = montage_dir / f"group_{gi:03d}.png"
            build_montage(members, root, out)
            montage_paths.append(out)

    if montage_paths:
        print()
        print(f"Wrote {len(montage_paths)} comparison montage(s) to {montage_dir}")


if __name__ == "__main__":
    main()
