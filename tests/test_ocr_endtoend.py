"""End-to-end OCR check against synthetic game frames.

Renders fake 1920x1080 "game" frames - a busy, moving background with white
subtitle text in the configured region - then runs the real pipeline over them:
threshold, hash, prepare, OCR. This is what catches a thresholding or cropping
mistake that unit tests on hand-built arrays would miss.

Needs a working OCR backend. Run with:  python -m tests.test_ocr_endtoend
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gamelens import capture  # noqa: E402
from gamelens.buffer import DialogueBuffer  # noqa: E402
from gamelens.config import Config  # noqa: E402
from gamelens.ocr import build_engine  # noqa: E402

W, H = 1920, 1080
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/crosextra/Carlito-Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]

LINES = [
    "I found the girl. She was in Velen, with the Baron.",
    "Then you'd best tell me everything, witcher.",
    "The ealdorman never paid. That's why I left.",
]


def load_font(size: int = 34) -> ImageFont.ImageFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def render_frame(text: str, scene_seed: int, region_px) -> np.ndarray:
    """A noisy, colourful scene with white subtitle text, as BGRA like mss gives."""
    rng = np.random.default_rng(scene_seed)
    # Blotchy background, upscaled so it looks like scenery rather than static.
    coarse = rng.integers(10, 150, size=(H // 40, W // 40, 3), dtype=np.uint8)
    background = np.asarray(
        Image.fromarray(coarse).resize((W, H), Image.BICUBIC)
    )
    img = Image.fromarray(background, mode="RGB")

    draw = ImageDraw.Draw(img)
    font = load_font()
    left, top, width, height = region_px
    cx = left + width // 2
    cy = top + height // 2
    # Witcher-style: white text with a dark outline, centred in the box.
    draw.text((cx, cy), text, font=font, fill=(245, 243, 235), anchor="mm",
              stroke_width=3, stroke_fill=(12, 12, 12))

    rgb = np.asarray(img)
    bgra = np.dstack([rgb[:, :, ::-1], np.full((H, W, 1), 255, dtype=np.uint8)])
    return bgra


def main() -> int:
    cfg = Config.load(Path(__file__).resolve().parent.parent / "config.yaml")
    engine, notes = build_engine(list(cfg.ocr["backends"]), str(cfg.ocr["language"]))
    for note in notes:
        print(f"  ocr: {note}")
    if engine.name == "none":
        print("\n  No OCR backend available here - skipping (not a failure).")
        return 0

    mon = capture.Box(0, 0, W, H)
    box = capture.resolve_region(mon, cfg.capture["region"])
    region_px = (box.left, box.top, box.width, box.height)
    print(f"  subtitle box: {box.width}x{box.height} at ({box.left},{box.top})\n")

    threshold = int(cfg.capture["text_threshold"])
    min_fraction = float(cfg.capture["min_text_fraction"])
    buf = DialogueBuffer(float(cfg.buffer["minutes"]),
                         float(cfg.buffer["dedupe_ratio"]),
                         int(cfg.buffer["min_line_chars"]))

    ocr_calls = 0
    failures = 0
    empty_frames_detected = 0

    # Each line is held for several frames over a changing background, with a
    # blank frame between lines - the real shape of a conversation.
    for index, line in enumerate(LINES):
        for repeat in range(4):
            frame = render_frame(line, scene_seed=index * 100 + repeat, region_px=region_px)
            crop = frame[box.top:box.top + box.height, box.left:box.left + box.width]
            gray = capture.to_gray(crop)
            mask = capture.text_mask(gray, threshold)
            if float(mask.mean()) < min_fraction:
                continue
            sig = capture.mask_signature(mask)
            if repeat > 0 and sig != last_sig:  # noqa: F821
                # Text identical, background different: the hash must not change.
                print(f"  FAIL  hash changed while the text stayed the same "
                      f"(line {index + 1}, frame {repeat})")
                failures += 1
            last_sig = sig  # noqa: F841
            if repeat == 0:
                read = engine.read(capture.prepare_for_ocr(gray, mask))
                ocr_calls += 1
                buf.add(read)
                print(f"  line {index + 1}")
                print(f"    rendered: {line}")
                print(f"    read:     {read or '(nothing)'}")

        blank = render_frame("", scene_seed=999 + index, region_px=region_px)
        crop = blank[box.top:box.top + box.height, box.left:box.left + box.width]
        gray = capture.to_gray(crop)
        if float(capture.text_mask(gray, threshold).mean()) < min_fraction:
            empty_frames_detected += 1
        buf.mark_gap()

    print(f"\n  OCR calls: {ocr_calls} for {len(LINES) * 4} frames "
          f"({len(LINES)} expected)")
    print(f"  blank frames correctly skipped: {empty_frames_detected}/{len(LINES)}")
    print(f"  buffer holds {len(buf)} lines\n")
    print("  transcript as the model would see it:")
    for row in buf.transcript().splitlines():
        print(f"    {row}")

    # Accuracy: how much of each rendered line survived the round trip.
    from difflib import SequenceMatcher

    stored = [line.text for line in buf.lines()]
    print()
    for rendered, got in zip(LINES, stored):
        ratio = SequenceMatcher(None, rendered.lower(), got.lower()).ratio()
        verdict = "ok  " if ratio > 0.85 else "FAIL"
        if ratio <= 0.85:
            failures += 1
        print(f"  {verdict}  {ratio * 100:5.1f}% match   {got}")

    if len(stored) != len(LINES):
        print(f"\n  FAIL  expected {len(LINES)} lines, got {len(stored)}")
        failures += 1

    print(f"\n  {'all good' if not failures else f'{failures} problem(s)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
