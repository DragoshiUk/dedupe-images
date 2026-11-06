#!/usr/bin/env bash
#
# upscale-to-max.sh - upscale image(s) so the longest side hits a target
# pixel size, preserving aspect ratio, at the highest quality achievable.
#
# Pipeline: if the image is already >= target, do a single high-quality
# Lanczos downsample (no AI step needed - AI upscaling never helps when
# shrinking). If the image is smaller, run it through Real-ESRGAN at the
# smallest integer GPU scale (2/3/4x) that overshoots the target, then
# Lanczos-resample down to the exact target size. Upscaling past the
# needed size and resampling down preserves more of the AI-added detail
# than asking Real-ESRGAN for a non-integer scale (which it can't do
# directly anyway - it only supports 2/3/4x).
#
# Usage:
#   ./upscale-to-max.sh [-s SIZE] [-m MODEL] [-o OUTDIR] INPUT...
#
#   INPUT        one or more image files, and/or directories (recursed)
#   -s SIZE      target longest-side size in px (default: 4096)
#   -m MODEL     realesrgan-x4plus-anime | realesrgan-x4plus | realesr-animevideov3
#                (default: realesrgan-x4plus-anime)
#   -o OUTDIR    output directory (default: alongside each input, "<name>_upscaled.<ext>")
#
# Requires: realesrgan-ncnn-vulkan, vips (or imagemagick as fallback)

set -euo pipefail

TARGET=4096
MODEL="realesrgan-x4plus-anime"
OUTDIR=""
MODEL_DIR="/usr/share/realesrgan-ncnn-vulkan/models"

usage() { grep -E '^#( |$)' "$0" | sed 's/^#//'; exit 1; }

while getopts "s:m:o:h" opt; do
  case "$opt" in
    s) TARGET="$OPTARG" ;;
    m) MODEL="$OPTARG" ;;
    o) OUTDIR="$OPTARG" ;;
    h) usage ;;
    *) usage ;;
  esac
done
shift $((OPTIND - 1))

if [ "$#" -eq 0 ]; then
  echo "error: no input files or directories given" >&2
  usage
fi

command -v realesrgan-ncnn-vulkan >/dev/null 2>&1 || { echo "error: realesrgan-ncnn-vulkan not found" >&2; exit 1; }
command -v vips >/dev/null 2>&1 || { echo "error: vips not found (needed for exact final resample)" >&2; exit 1; }

case "$MODEL" in
  realesrgan-x4plus-anime|realesrgan-x4plus|realesr-animevideov3) ;;
  *) echo "error: unknown model '$MODEL'" >&2; exit 1 ;;
esac

if [ -n "$OUTDIR" ]; then
  mkdir -p "$OUTDIR"
fi

# Real-ESRGAN only supports integer scales 2/3/4. Pick the smallest that
# reaches or exceeds the requested scale factor; cap at 4 (its max) and
# chain another 4x pass on top for extreme scale-ups.
pick_scale() {
  local needed="$1"
  awk -v n="$needed" 'BEGIN { if (n <= 2) print 2; else if (n <= 3) print 3; else print 4 }'
}

process_one() {
  local infile="$1"
  local base ext outfile w h longest needed scale tmpfile chain_scale total_scale

  base="$(basename "$infile")"
  ext="${base##*.}"
  base="${base%.*}"

  if [ -n "$OUTDIR" ]; then
    outfile="$OUTDIR/${base}_upscaled.${ext}"
  else
    outfile="$(dirname "$infile")/${base}_upscaled.${ext}"
  fi

  read -r w h < <(vipsheader -f width "$infile" | tr -d '\n'; echo -n ' '; vipsheader -f height "$infile")
  longest=$(( w > h ? w : h ))

  if [ "$longest" -ge "$TARGET" ]; then
    echo "[$base] ${w}x${h} already >= ${TARGET}px, downsampling with Lanczos only"
    vips resize "$infile" "$outfile" "$(awk -v t="$TARGET" -v l="$longest" 'BEGIN{print t/l}')" --kernel lanczos3
    echo "[$base] -> $outfile"
    return
  fi

  needed=$(awk -v t="$TARGET" -v l="$longest" 'BEGIN{print t/l}')
  scale=$(pick_scale "$needed")
  total_scale=$scale

  # Chain a second 4x AI pass if a single pass still undershoots the target
  # (e.g. going from 200px to 4096px needs ~20x, beyond one 4x pass).
  chain_scale=""
  while [ "$(awk -v l="$longest" -v s="$total_scale" -v t="$TARGET" 'BEGIN{print (l*s<t)?1:0}')" -eq 1 ] && [ "$total_scale" -lt 16 ]; do
    chain_scale=4
    total_scale=$(( total_scale * 4 ))
  done

  tmpfile="$(mktemp --suffix=".${ext}")"
  echo "[$base] ${w}x${h} -> AI upscale x${scale} ($MODEL)"
  realesrgan-ncnn-vulkan -i "$infile" -o "$tmpfile" -n "$MODEL" -s "$scale" -m "$MODEL_DIR"

  if [ -n "$chain_scale" ]; then
    local tmpfile2
    tmpfile2="$(mktemp --suffix=".${ext}")"
    echo "[$base] chaining second AI pass x${chain_scale} (total x${total_scale})"
    realesrgan-ncnn-vulkan -i "$tmpfile" -o "$tmpfile2" -n "$MODEL" -s "$chain_scale" -m "$MODEL_DIR"
    rm -f "$tmpfile"
    tmpfile="$tmpfile2"
  fi

  echo "[$base] final Lanczos resample to exact ${TARGET}px longest side"
  vips resize "$tmpfile" "$outfile" "$(awk -v t="$TARGET" -v l="$longest" -v s="$total_scale" 'BEGIN{print t/l/s}')" --kernel lanczos3
  rm -f "$tmpfile"
  echo "[$base] -> $outfile"
}

# Expand directories into files
FILES=()
for input in "$@"; do
  if [ -d "$input" ]; then
    while IFS= read -r -d '' f; do FILES+=("$f"); done < <(find "$input" -type f \( -iname '*.png' -o -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.webp' \) -print0)
  elif [ -f "$input" ]; then
    FILES+=("$input")
  else
    echo "warning: skipping '$input' (not a file or directory)" >&2
  fi
done

if [ "${#FILES[@]}" -eq 0 ]; then
  echo "error: no valid input images found" >&2
  exit 1
fi

for f in "${FILES[@]}"; do
  process_one "$f"
done
