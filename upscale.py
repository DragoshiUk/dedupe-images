"""
AI upscale support for review_gui.py's Upscale tab.

Resizes an image so its longest side hits a target pixel count, preserving
aspect ratio, driven from the web UI.

- Already >= target: single Lanczos3 downsample (PIL) - AI upscaling
  never helps when shrinking.
- Smaller than target: runs Real-ESRGAN (realesrgan-ncnn-vulkan, GPU) at
  the smallest integer factor (2/3/4x) that covers the gap, chaining a
  second 4x pass for extreme scale-ups, then one final Lanczos3 resample
  down to the *exact* target pixel count.

Output is a new file - by default "<name>_upscaled.<ext>" next to the
original, or, when an output directory is chosen, the source tree
structure mirrored under it. The filename affix (default "_upscaled") and
whether it's a prefix or a suffix are both configurable. Nothing is ever
moved, deleted, or quarantined, so this has no manifest/restore
involvement at all. A file whose output path already exists is skipped
and reported, not silently overwritten, unless overwrite is set.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from PIL import Image

from find_near_duplicates import iter_images

UPSCALE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
MIN_TARGET = 512
MAX_TARGET = 8192
DEFAULT_TARGET = 4096
MODEL = "realesrgan-x4plus-anime"
DEFAULT_AFFIX = "_upscaled"
AFFIX_POSITIONS = {"prefix", "suffix"}

# realesrgan-ncnn-vulkan can come from a system package (its models then
# live in the shared dir below) or from the self-contained portable build
# the Upscale tab downloads on request - unpacked under VENDOR_DIR with its
# own bundled models/ next to the binary.
SYSTEM_MODEL_DIR = Path("/usr/share/realesrgan-ncnn-vulkan/models")
VENDOR_DIR = Path.home() / ".local" / "share" / "dedupe-images" / "realesrgan-ncnn-vulkan"
VENDOR_BIN = VENDOR_DIR / "realesrgan-ncnn-vulkan"
# Portable Intel/AMD/Nvidia build: binary + models, no system deps beyond a
# Vulkan loader. Pinned deliberately - bump by hand.
RELEASE_URL = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesrgan-ncnn-vulkan-20220424-ubuntu.zip"
INSTALL_HINT = ("the 'realesrgan-ncnn-vulkan' AUR package, or use the "
                "\"Download realesrgan-ncnn-vulkan\" button on the Upscale tab")


def resolve_tool() -> tuple[Path, Path] | None:
    """(binary, models_dir) for the first usable realesrgan-ncnn-vulkan - a
    system install first, then the downloaded portable copy - or None."""
    sys_bin = shutil.which("realesrgan-ncnn-vulkan")
    if sys_bin and SYSTEM_MODEL_DIR.is_dir():
        return Path(sys_bin), SYSTEM_MODEL_DIR
    vend_models = VENDOR_DIR / "models"
    if VENDOR_BIN.is_file() and os.access(VENDOR_BIN, os.X_OK) and vend_models.is_dir():
        return VENDOR_BIN, vend_models
    return None


def tool_status() -> str | None:
    """None if realesrgan-ncnn-vulkan is ready to use, otherwise a
    human-readable reason it isn't."""
    if resolve_tool() is None:
        return f"realesrgan-ncnn-vulkan is not installed - install {INSTALL_HINT}"
    return None


def install_tool(on_log=None, on_bytes=None) -> None:
    """Downloads the pinned portable realesrgan-ncnn-vulkan release and
    unpacks just its binary + models/ under VENDOR_DIR. on_log(str) gets
    human-readable lines (also echoed to stdout); on_bytes(done, total)
    ticks during the download. Raises on any failure."""
    def log(msg):
        print(f"  [upscale-install] {msg}")
        if on_log:
            on_log(msg)

    if resolve_tool() is not None:
        log("realesrgan-ncnn-vulkan is already available - nothing to do")
        return

    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        zip_path = Path(td) / "realesrgan.zip"
        log(f"Downloading {RELEASE_URL}")
        req = urllib.request.Request(RELEASE_URL, headers={"User-Agent": "dedupe-images"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(zip_path, "wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            done = last_logged = 0
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                if on_bytes:
                    on_bytes(done, total)
                if done - last_logged >= 5 * 1024 * 1024:
                    last_logged = done
                    got = f"{done / 1024 / 1024:.0f} MB"
                    log(f"  {got}{f' / {total / 1024 / 1024:.0f} MB' if total else ''}")
        log(f"Downloaded {done / 1024 / 1024:.1f} MB, extracting")

        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            bin_entry = next(
                (n for n in names if not n.endswith("/") and Path(n).name == "realesrgan-ncnn-vulkan"),
                None,
            )
            if bin_entry is None:
                raise RuntimeError("archive contains no realesrgan-ncnn-vulkan binary")
            prefix = bin_entry[: -len("realesrgan-ncnn-vulkan")]
            model_entries = [n for n in names
                             if not n.endswith("/") and n.startswith(prefix + "models/")]
            if not model_entries:
                raise RuntimeError("archive contains no bundled models/ directory")
            vendor_root = VENDOR_DIR.resolve()
            for n in [bin_entry, *model_entries]:
                rel = n[len(prefix):]
                dest = VENDOR_DIR / rel
                # zip-slip guard: a ".." component only resolves at the OS
                # layer, so check the resolved path still lands inside
                # VENDOR_DIR before creating anything.
                if not dest.resolve().is_relative_to(vendor_root):
                    raise RuntimeError(f"archive entry {n!r} escapes the vendor directory")
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(n) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                log(f"  {rel}")

    VENDOR_BIN.chmod(0o755)

    log("Checking the binary runs")
    try:
        proc = subprocess.run([str(VENDOR_BIN)], capture_output=True, text=True, timeout=30)
        blob = (proc.stdout + proc.stderr).lower()
        if "error while loading shared libraries" in blob or "cannot execute" in blob:
            first = (proc.stderr or proc.stdout).strip().splitlines()[0]
            log(f"  ! warning: {first}")
            log("  ! files are in place, but the binary needs a Vulkan loader - "
                "install 'vulkan-icd-loader' plus your GPU's Vulkan driver")
    except FileNotFoundError:
        raise RuntimeError("the downloaded binary won't execute on this system")
    except subprocess.TimeoutExpired:
        pass  # printed usage and sat waiting for input - close enough to "runs"

    if resolve_tool() is None:
        raise RuntimeError("install finished but the tool still isn't resolving")
    log("realesrgan-ncnn-vulkan is ready to use")


def iter_upscale_candidates(root: Path, exclude_dirs: set[Path]):
    """Yields (path, width, height) for every candidate image under root -
    unfiltered by target size, since the web UI filters client-side as the
    resolution slider moves rather than re-scanning per tick."""
    for p in iter_images(root, UPSCALE_EXTENSIONS, exclude_dirs):
        try:
            with Image.open(p) as img:
                w, h = img.size
        except Exception as e:
            print(f"  ! could not read {p}: {e}", file=sys.stderr)
            continue
        yield p, w, h


def affixed_name(name: str, affix: str, affix_pos: str) -> str:
    """Adds `affix` to a filename's stem as a prefix or suffix, extension
    preserved: affixed_name("cat.jpg", "_upscaled", "suffix") -> "cat_upscaled.jpg"."""
    p = Path(name)
    stem = f"{affix}{p.stem}" if affix_pos == "prefix" else f"{p.stem}{affix}"
    return f"{stem}{p.suffix}"


def is_affixed(name: str, affix: str, affix_pos: str) -> bool:
    """True if `name`'s stem already carries `affix` in the chosen
    position - used to skip a previous run's own output when it sits
    alongside the sources (out_dir=None), so re-running can't chew on
    "<name>_upscaled.jpg" and spit out "<name>_upscaled_upscaled.jpg"."""
    if not affix:
        return False
    stem = Path(name).stem
    return stem.startswith(affix) if affix_pos == "prefix" else stem.endswith(affix)


def output_path(src: Path, root: Path, out_dir: Path | None,
                affix: str, affix_pos: str) -> Path:
    """Where the upscaled copy of `src` is written. out_dir=None keeps it
    beside the original; otherwise the source's sub-path below `root` is
    recreated under `out_dir` (so two same-named files in different
    subdirectories can't collide)."""
    name = affixed_name(src.name, affix, affix_pos)
    if out_dir is None:
        return src.with_name(name)
    return out_dir / src.relative_to(root).parent / name


def pick_scale(needed: float) -> int:
    """Real-ESRGAN only supports integer scales 2/3/4 - the smallest one
    that reaches or exceeds the requested factor."""
    if needed <= 2:
        return 2
    if needed <= 3:
        return 3
    return 4


def _run_realesrgan(infile: Path, outfile: Path, scale: int, binary: Path, models_dir: Path):
    subprocess.run(
        [str(binary), "-i", str(infile), "-o", str(outfile),
         "-n", MODEL, "-s", str(scale), "-m", str(models_dir)],
        check=True, capture_output=True, text=True,
    )


def _lanczos_resize(img: Image.Image, w: int, h: int, scale: float, dest: Path):
    """Lanczos3-resample img to (w, h) * scale and save to dest, keeping
    the source mode when it's already web-friendly, else flattening to RGB."""
    new_size = (max(1, round(w * scale)), max(1, round(h * scale)))
    mode = img.mode if img.mode in ("RGB", "RGBA") else "RGB"
    img.convert(mode).resize(new_size, Image.LANCZOS).save(dest)


class OutputExists(Exception):
    """The destination file already exists and overwrite wasn't requested -
    a skip, not a failure. Carries the destination for reporting."""
    def __init__(self, dest: Path):
        super().__init__(f"output already exists: {dest}")
        self.dest = dest


def upscale_one(path: Path, target: int, root: Path, out_dir: Path | None = None,
                affix: str = DEFAULT_AFFIX, affix_pos: str = "suffix",
                overwrite: bool = False, tool=None) -> Path:
    """Processes a single image, returns the output path. Raises on
    failure (subprocess.CalledProcessError, OSError, ...) - callers
    processing a batch should catch per-file so one bad image doesn't
    abort the rest. Raises OutputExists (a skip, not a failure) when the
    destination is already there and overwrite is False. `tool` is a
    resolve_tool() result; resolved here if omitted."""
    dest = output_path(path, root, out_dir, affix, affix_pos)
    if dest.resolve() == path.resolve():
        raise ValueError(f"output path would overwrite the original: {path}")
    if dest.exists() and not overwrite:
        raise OutputExists(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(path) as img:
        w, h = img.size
        longest = max(w, h)
        if longest >= target:
            _lanczos_resize(img, w, h, target / longest, dest)  # downscale, no AI
            return dest

    if tool is None:
        tool = resolve_tool()
    if tool is None:
        raise RuntimeError(f"realesrgan-ncnn-vulkan is not installed - install {INSTALL_HINT}")
    binary, models_dir = tool

    scale = pick_scale(target / longest)
    total_scale = scale

    # Chain a second 4x AI pass if a single pass still undershoots the
    # target (e.g. 200px -> 4096px needs ~20x, beyond one 4x pass).
    while longest * total_scale < target and total_scale < 16:
        total_scale *= 4

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        pass1 = tmp / f"pass1{path.suffix}"
        _run_realesrgan(path, pass1, scale, binary, models_dir)
        source_for_resample = pass1
        if total_scale != scale:
            pass2 = tmp / f"pass2{path.suffix}"
            _run_realesrgan(pass1, pass2, 4, binary, models_dir)
            source_for_resample = pass2

        with Image.open(source_for_resample) as img:
            _lanczos_resize(img, img.width, img.height, target / longest / total_scale, dest)
    return dest


def eligible_candidates(root: Path, exclude_dirs: set[Path], target: int, out_dir: Path | None,
                        affix: str, affix_pos: str) -> list[Path]:
    """The paths run_upscale would actually process: under target on the
    longest side, and - in alongside mode (out_dir=None) - not themselves
    an already-affixed prior output."""
    scan_excludes = set(exclude_dirs)
    if out_dir is not None:
        scan_excludes.add(out_dir)  # never feed a previous run's own output back in
    return [p for p, w, h in iter_upscale_candidates(root, scan_excludes)
            if max(w, h) < target
            and not (out_dir is None and is_affixed(p.name, affix, affix_pos))]


def run_upscale(root: Path, exclude_dirs: set[Path], target: int, out_dir: Path | None = None,
                affix: str = DEFAULT_AFFIX, affix_pos: str = "suffix",
                overwrite: bool = False, on_progress=None) -> dict:
    """Recomputes the eligible list fresh (same "recompute immediately
    before applying" approach used for Identical Files/Normalisation),
    then upscales every eligible file in turn. Returns {"processed": N,
    "skipped": [{"path", "dest"}, ...], "errors": [{"path", "error"}, ...]}
    - "skipped" is files whose output already existed and overwrite was off."""
    candidates = eligible_candidates(root, exclude_dirs, target, out_dir, affix, affix_pos)
    tool = resolve_tool()
    total = len(candidates)
    processed = 0
    skipped = []
    errors = []
    for i, p in enumerate(candidates, 1):
        rel = str(p.relative_to(root))
        if on_progress:
            on_progress(rel, i, total)
        try:
            upscale_one(p, target, root, out_dir, affix, affix_pos, overwrite=overwrite, tool=tool)
            processed += 1
        except OutputExists as e:
            print(f"  - skipped {rel} (output exists: {e.dest})", file=sys.stderr)
            skipped.append({"path": rel, "dest": str(e.dest)})
        except Exception as e:
            print(f"  ! failed to upscale {rel}: {e}", file=sys.stderr)
            errors.append({"path": rel, "error": str(e)})
    return {"processed": processed, "skipped": skipped, "errors": errors}
