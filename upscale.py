"""
AI upscale support for review_gui.py's Upscale tab.

Ports the logic of ~/Projects/max-side-upscale/upscale-to-max.sh into
Python so it can be driven from the web UI: resize an image so its
longest side hits a target pixel count, preserving aspect ratio.

- Already >= target: single Lanczos3 downsample (PIL) - AI upscaling
  never helps when shrinking.
- Smaller than target: runs Real-ESRGAN (realesrgan-ncnn-vulkan, GPU) at
  the smallest integer factor (2/3/4x) that covers the gap, chaining a
  second 4x pass for extreme scale-ups, then one final Lanczos3 resample
  down to the *exact* target pixel count.

Output is always "<name>_upscaled.<ext>" next to the original - nothing
is moved, deleted, or quarantined, so this has no manifest/restore
involvement at all; re-running just overwrites the previous output for
whichever originals are still under the target.
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

from find_near_duplicates import iter_images

UPSCALE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
MIN_TARGET = 512
MAX_TARGET = 8192
DEFAULT_TARGET = 4096
MODEL = "realesrgan-x4plus-anime"
MODEL_DIR = "/usr/share/realesrgan-ncnn-vulkan/models"
OUTPUT_SUFFIX = "_upscaled"


def tool_status() -> str | None:
    """None if realesrgan-ncnn-vulkan is ready to use, otherwise a
    human-readable reason it isn't."""
    if shutil.which("realesrgan-ncnn-vulkan") is None:
        return "realesrgan-ncnn-vulkan not found on PATH (AUR: realesrgan-ncnn-vulkan)"
    if not Path(MODEL_DIR).is_dir():
        return f"model directory not found: {MODEL_DIR}"
    return None


def iter_upscale_candidates(root: Path, quarantine_dir: Path, review_dir: Path):
    """Yields (path, width, height) for every candidate image under root -
    unfiltered by target size, since the web UI filters client-side as the
    resolution slider moves rather than re-scanning per tick."""
    for p in iter_images(root, UPSCALE_EXTENSIONS, {quarantine_dir, review_dir}):
        try:
            with Image.open(p) as img:
                w, h = img.size
        except Exception as e:
            print(f"  ! could not read {p}: {e}", file=sys.stderr)
            continue
        yield p, w, h


def output_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}{OUTPUT_SUFFIX}{path.suffix}")


def pick_scale(needed: float) -> int:
    """Real-ESRGAN only supports integer scales 2/3/4 - the smallest one
    that reaches or exceeds the requested factor."""
    if needed <= 2:
        return 2
    if needed <= 3:
        return 3
    return 4


def _run_realesrgan(infile: Path, outfile: Path, scale: int):
    subprocess.run(
        ["realesrgan-ncnn-vulkan", "-i", str(infile), "-o", str(outfile),
         "-n", MODEL, "-s", str(scale), "-m", MODEL_DIR],
        check=True, capture_output=True, text=True,
    )


def upscale_one(path: Path, target: int) -> Path:
    """Processes a single image, returns the output path. Raises on
    failure (subprocess.CalledProcessError, OSError, ...) - callers
    processing a batch should catch per-file so one bad image doesn't
    abort the rest."""
    dest = output_path(path)

    with Image.open(path) as img:
        w, h = img.size
        longest = max(w, h)
        if longest >= target:
            scale = target / longest
            new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
            img.convert("RGB" if img.mode not in ("RGB", "RGBA") else img.mode) \
               .resize(new_size, Image.LANCZOS).save(dest)
            return dest

    needed = target / longest
    scale = pick_scale(needed)
    total_scale = scale

    # Chain a second 4x AI pass if a single pass still undershoots the
    # target (e.g. 200px -> 4096px needs ~20x, beyond one 4x pass).
    chain = False
    while longest * total_scale < target and total_scale < 16:
        chain = True
        total_scale *= 4

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        pass1 = tmp / f"pass1{path.suffix}"
        _run_realesrgan(path, pass1, scale)
        source_for_resample = pass1
        if chain:
            pass2 = tmp / f"pass2{path.suffix}"
            _run_realesrgan(pass1, pass2, 4)
            source_for_resample = pass2

        with Image.open(source_for_resample) as img:
            resample_scale = target / longest / total_scale
            new_size = (max(1, round(img.width * resample_scale)), max(1, round(img.height * resample_scale)))
            img.convert("RGB" if img.mode not in ("RGB", "RGBA") else img.mode) \
               .resize(new_size, Image.LANCZOS).save(dest)
    return dest


def run_upscale(root: Path, quarantine_dir: Path, review_dir: Path, target: int, on_progress=None) -> dict:
    """Recomputes the eligible list fresh (same "recompute immediately
    before applying" approach used for Identical Files/Normalisation),
    then upscales every eligible file in turn. Returns {"processed": N,
    "errors": [{"path": rel, "error": str}, ...]}."""
    candidates = [(p, w, h) for p, w, h in iter_upscale_candidates(root, quarantine_dir, review_dir)
                  if max(w, h) < target]
    total = len(candidates)
    processed = 0
    errors = []
    for i, (p, w, h) in enumerate(candidates, 1):
        rel = str(p.relative_to(root))
        if on_progress:
            on_progress(rel, i, total)
        try:
            upscale_one(p, target)
            processed += 1
        except Exception as e:
            print(f"  ! failed to upscale {rel}: {e}", file=sys.stderr)
            errors.append({"path": rel, "error": str(e)})
    return {"processed": processed, "errors": errors}
